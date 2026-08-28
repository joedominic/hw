from typing import TypedDict, List, Annotated, Optional, Any
import operator
import logging
import re
import threading

from django.conf import settings
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from langgraph.graph import StateGraph, END

from .callbacks import TokenUsageCallback
from .llm_factory import get_llm
from .parsers import (
    AtsJudgeResult,
    ScoreFeedback,
    FitCheckResult,
    coerce_structured_judge_result as _coerce_structured_judge_result,
    is_default_judge_fallback as _is_default_judge_fallback,
    llm_message_content_to_text as _llm_message_content_to_text,
    parse_ats_judge_fallback as _parse_ats_judge_fallback,
    parse_score_fallback as _parse_score_fallback,
    parse_fit_check_fallback as _parse_fit_check_fallback_from_parsers,
    normalize_dict_keys as _normalize_dict_keys,
    serialize_llm_result_for_log as _serialize_llm_result_for_log,
)
from .prompts import (
    DEFAULT_WRITER_PROMPT,
    DEFAULT_WRITER_SYSTEM,
    DEFAULT_WRITER_USER,
    DEFAULT_ATS_JUDGE_PROMPT,
    DEFAULT_ATS_JUDGE_SYSTEM,
    DEFAULT_ATS_JUDGE_USER,
    DEFAULT_RECRUITER_JUDGE_PROMPT,
    DEFAULT_RECRUITER_JUDGE_SYSTEM,
    DEFAULT_RECRUITER_JUDGE_USER,
    DEFAULT_FIT_CHECK_PROMPT,
    DEFAULT_FIT_CHECK_SYSTEM,
    DEFAULT_FIT_CHECK_USER,
    DEFAULT_MATCHING_PROMPT,
    DEFAULT_MATCHING_SYSTEM,
    DEFAULT_MATCHING_USER,
)
from .optimizer_budget import (
    omitted_duplicate_field_note,
    should_include_full_job_description,
    should_include_source_resume,
    truncate_judge_job_description,
    truncate_judge_resume,
)

logger = logging.getLogger(__name__)

# LangGraph stream updates often omit non-channel / ephemeral keys. Persist per-node
# LLM debug payloads here (FIFO per run key) so AgentLog always gets prompts + tokens.
_NODE_LLM_DEBUG_LOCK = threading.Lock()
_NODE_LLM_DEBUG_QUEUES: dict[str, list[dict]] = {}


def push_node_llm_debug(run_key: str | None, payload: dict) -> None:
    """Append debug payload for the next AgentLog of this optimization run."""
    key = (run_key or "").strip()
    if not key or not payload:
        return
    with _NODE_LLM_DEBUG_LOCK:
        _NODE_LLM_DEBUG_QUEUES.setdefault(key, []).append(dict(payload))


def pop_node_llm_debug(run_key: str | None) -> dict:
    """Pop the next queued debug payload (ordered by node completion)."""
    key = (run_key or "").strip()
    if not key:
        return {}
    with _NODE_LLM_DEBUG_LOCK:
        q = _NODE_LLM_DEBUG_QUEUES.get(key) or []
        if not q:
            return {}
        item = q.pop(0)
        if not q:
            _NODE_LLM_DEBUG_QUEUES.pop(key, None)
        return item


def clear_node_llm_debug(run_key: str | None) -> None:
    key = (run_key or "").strip()
    if not key:
        return
    with _NODE_LLM_DEBUG_LOCK:
        _NODE_LLM_DEBUG_QUEUES.pop(key, None)


def _debug_payload_from_node_out(
    *,
    step: str,
    messages_debug: list[dict],
    debug_prompt: str,
    format_summary: dict,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    tokens_estimated: bool = False,
    raw_llm_response: str | None = None,
    raw_llm_response_retry: str | None = None,
    parse_info: dict | None = None,
    feedback=None,
    optimized_resume: str | None = None,
) -> dict:
    """Fields stored on AgentLog.thought for admin debug UI."""
    payload: dict[str, Any] = {
        "step": step,
        "debug_messages": messages_debug,
        "debug_prompt": debug_prompt,
        "debug_format_summary": format_summary,
    }
    if input_tokens is not None:
        payload["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        payload["output_tokens"] = int(output_tokens)
    if tokens_estimated:
        payload["tokens_estimated"] = True
    if raw_llm_response:
        payload["raw_llm_response"] = raw_llm_response
    if raw_llm_response_retry:
        payload["raw_llm_response_retry"] = raw_llm_response_retry
    if parse_info is not None:
        payload["parse_info"] = parse_info
    if feedback is not None:
        payload["feedback"] = feedback
    if optimized_resume:
        payload["optimized_resume"] = optimized_resume
    return payload

# Chars-per-token heuristic when provider does not report usage (e.g. some Ollama setups)
_CHARS_PER_TOKEN_ESTIMATE = 4


def _judges_prefer_local() -> bool:
    return bool(getattr(settings, "OPTIMIZER_JUDGES_PREFER_LOCAL", False))


def _normalize_token_usage(response, llm_output=None, prompt_text=None, response_content=None):
    """Extract (input_tokens, output_tokens, estimated, cached_tokens) from LLM response."""
    in_tok, out_tok, estimated = 0, 0, False
    cached_tok = 0
    usage = None
    if response is not None:
        usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        in_tok = int(usage.get("input_tokens") or usage.get("input") or usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or usage.get("output") or usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            try:
                cached_tok = int(details.get("cached_tokens") or 0)
            except (TypeError, ValueError):
                cached_tok = 0
    if in_tok == 0 and out_tok == 0 and llm_output:
        tu = (llm_output or {}).get("token_usage") or (llm_output or {}).get("usage") or {}
        if isinstance(tu, dict):
            in_tok = int(tu.get("input_tokens") or tu.get("prompt_tokens") or 0)
            out_tok = int(tu.get("output_tokens") or tu.get("completion_tokens") or 0)
            ptd = tu.get("prompt_tokens_details")
            if isinstance(ptd, dict) and cached_tok == 0:
                try:
                    cached_tok = int(ptd.get("cached_tokens") or 0)
                except (TypeError, ValueError):
                    pass
    if in_tok == 0 and out_tok == 0 and (prompt_text is not None or response is not None or response_content is not None):
        content = response_content
        if content is None and response is not None:
            content = getattr(response, "content", None) or str(response)
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        content = content or ""
        in_tok = max(0, len(str(prompt_text or "")) // _CHARS_PER_TOKEN_ESTIMATE)
        out_tok = max(0, len(content) // _CHARS_PER_TOKEN_ESTIMATE)
        estimated = True
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "tokens_estimated": estimated,
        "cached_tokens": cached_tok,
    }


def _format_prompt(template: str, **kwargs) -> str:
    """Format template with kwargs, but treat only known keys as placeholders. Any other { } in the
    template (e.g. JSON examples) are left literal so str.format() does not raise KeyError."""
    known = set(kwargs.keys())
    # Escape all braces, then restore only our placeholders so they get substituted
    escaped = template.replace("{", "{{").replace("}", "}}")
    for name in known:
        escaped = escaped.replace("{{" + name + "}}", "{" + name + "}")
    return escaped.format(**kwargs)


def build_llm_messages_for_prompt(
    *,
    legacy_combined: str | None,
    system_template: str | None,
    user_template: str | None,
    format_kwargs: dict,
) -> list[BaseMessage]:
    """System + user messages (cache-friendly prefix), or one HumanMessage for legacy templates."""
    leg = (legacy_combined or "").strip()
    if leg:
        return [HumanMessage(content=_format_prompt(leg, **format_kwargs))]
    sys_t = (system_template or "").strip()
    usr_t = (user_template or "").strip()
    out: list[BaseMessage] = []
    if sys_t:
        out.append(SystemMessage(content=_format_prompt(sys_t, **format_kwargs)))
    if usr_t:
        out.append(HumanMessage(content=_format_prompt(usr_t, **format_kwargs)))
    return out


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc)
    return "429" in msg or "ResourceExhausted" in type(exc).__name__ or "quota" in msg.lower()


def _llm_invoke_with_retry(
    llm,
    messages,
    *,
    user,
    max_attempts=2,
    config=None,
    structured_schema=None,
    job_cache_key: str | None = None,
    usage_query_kind: str | None = None,
    prefer_local: bool = True,
    only_local: bool = False,
    allow_local: bool = True,
):
    """
    Invoke via the gateway.

    Defaults favor Ollama Local for non-optimizer product work. Resume-optimizer
    nodes pass prefer_local=False and allow_local=False for cloud-only routing.
    """
    from .llm_gateway import call_invoke_llm_messages

    if user is None:
        user = getattr(llm, "_resume_user", None)
    if user is None:
        raise ValueError("user is required for LLM gateway invoke")

    return call_invoke_llm_messages(
        messages,
        user=user,
        job_cache_key=job_cache_key,
        structured_schema=structured_schema,
        config=config,
        # None means gateway selection; do not force a caller-built local client.
        llm_override=llm if llm is not None else None,
        max_attempts_per_model=max_attempts,
        usage_query_kind=usage_query_kind,
        prefer_local=prefer_local,
        only_local=only_local,
        allow_local=allow_local,
    )


def _state_user(state: dict):
    """Resolve owning User from optimizer workflow state (user object, user_id, or llm._resume_user)."""
    if not state:
        return None
    user_obj = _state_get(state, "user")
    if user_obj is not None:
        return user_obj
    uid = _state_get(state, "user_id")
    if uid:
        try:
            from django.contrib.auth import get_user_model

            found = get_user_model().objects.filter(pk=int(uid)).first()
            if found:
                return found
        except Exception:
            pass
    llm = _state_get(state, "llm")
    if llm is not None:
        u_llm = getattr(llm, "_resume_user", None)
        if u_llm is not None:
            return u_llm
    return None


def _extract_json_object(content: str) -> Optional[dict]:
    """Backward-compatible shim delegating to parser module."""
    from .parsers import _extract_json_object as _inner

    return _inner(content)


def _normalize_key(k: str) -> str:
    from .parsers import _normalize_key as _inner

    return _inner(k)


def _get_key(d: dict, *candidates: str):
    from .parsers import _get_key as _inner

    return _inner(d, *candidates)


def _normalize_dict_keys(obj):
    from .parsers import normalize_dict_keys as _inner

    return _inner(obj)


def _parse_fit_check_fallback(content: str) -> "FitCheckResult":
    text = (content or "").strip()
    if not text:
        return FitCheckResult(score=50, reasoning="Could not parse fit check. Defaulting.", thoughts="")

    data = _extract_json_object(text)
    if isinstance(data, dict):
        data = _normalize_dict_keys(data)
        raw_score = _get_key(
            data,
            "score",
            "match_score",
            "resume_match_score",
            "ats_match_score",
            "fit_score",
        )
        reasoning = _get_key(
            data,
            "reasoning",
            "overall_strategic_assessment",
            "feedback",
            "summary",
        )
        thoughts = _get_key(data, "thoughts", "feedback", "reasoning")
        try:
            score = int(raw_score) if raw_score is not None else 50
            score = max(0, min(100, score))
            return FitCheckResult(
                score=score,
                reasoning=str(reasoning or thoughts or "Parsed from JSON fallback."),
                thoughts=str(thoughts or reasoning or ""),
            )
        except (TypeError, ValueError):
            pass

    score_match = re.search(
        r"(?:Resume\s+Match\s+Score|ATS\s+Match\s+Score|Recruiter\s+Score|Match\s+Score|Fit\s+Score|Score)"
        r"\s*(?:\([^)]*\))?\s*[:=-]?\s*(\d{1,3})(?:\s*/\s*100)?",
        text,
        re.IGNORECASE,
    )
    if not score_match:
        score_match = re.search(r"\bscore\s*[=:]\s*(\d{1,3})(?:\s*/\s*100)?\b", text, re.IGNORECASE)

    reasoning = ""
    thoughts = ""

    reasoning_match = re.search(
        r"(?:Reasoning|Overall Strategic Assessment|Summary|Assessment)\s*:\s*(.+?)(?:\n[A-Z][^\n]{0,60}:|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if reasoning_match:
        reasoning = reasoning_match.group(1).strip()

    thoughts_match = re.search(
        r"(?:Thoughts|Feedback|Why|Analysis)\s*:\s*(.+?)(?:\n[A-Z][^\n]{0,60}:|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if thoughts_match:
        thoughts = thoughts_match.group(1).strip()

    if not reasoning:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        filtered = []
        for line in lines:
            if re.search(
                r"(?:Resume\s+Match\s+Score|ATS\s+Match\s+Score|Recruiter\s+Score|Match\s+Score|Fit\s+Score|Score)\s*(?:\([^)]*\))?\s*[:=-]?\s*\d{1,3}",
                line,
                re.IGNORECASE,
            ):
                continue
            filtered.append(line)
        if filtered:
            reasoning = filtered[0][:500]
            if len(filtered) > 1:
                thoughts = thoughts or "\n".join(filtered[1:])[:1000]

    if score_match:
        try:
            score = max(0, min(100, int(score_match.group(1))))
            return FitCheckResult(
                score=score,
                reasoning=reasoning or "Parsed score from plain-text response.",
                thoughts=thoughts or reasoning or "",
            )
        except (TypeError, ValueError):
            pass

    return FitCheckResult(
        score=50,
        reasoning=(reasoning or text[:500] or "Could not parse fit check. Defaulting."),
        thoughts=thoughts or "",
    )


def run_fit_check(
    resume_text: str,
    job_description: str,
    llm,
    *,
    user,
    prompt_template: str = None,
    prompt_system: str = None,
    prompt_user: str = None,
    prompt_legacy: str = None,
    job_cache_key: str | None = None,
    usage_query_kind: str | None = None,
) -> dict:
    """Returns { score: int, reasoning: str, thoughts: str }. Score 0-100; if < 50 caller may ask user to confirm."""
    fmt = dict(resume_text=resume_text, job_description=job_description)
    if prompt_template and str(prompt_template).strip():
        messages = [HumanMessage(content=_format_prompt(str(prompt_template).strip(), **fmt))]
    else:
        leg = (prompt_legacy or "").strip()
        st = (prompt_system or "").strip()
        ut = (prompt_user or "").strip()
        if not leg and not st and not ut:
            messages = build_llm_messages_for_prompt(
                legacy_combined=None,
                system_template=DEFAULT_FIT_CHECK_SYSTEM,
                user_template=DEFAULT_FIT_CHECK_USER,
                format_kwargs=fmt,
            )
        else:
            messages = build_llm_messages_for_prompt(
                legacy_combined=leg or None,
                system_template=st or None,
                user_template=ut or None,
                format_kwargs=fmt,
            )
    from .llm_gateway import USAGE_QUERY_FIT_CHECK

    _qk = usage_query_kind or USAGE_QUERY_FIT_CHECK
    dbg = "\n\n---\n\n".join(f"{type(m).__name__}:{getattr(m, 'content', '')}" for m in messages)
    try:
        result = _llm_invoke_with_retry(
            llm,
            messages,
            user=user,
            structured_schema=FitCheckResult,
            job_cache_key=job_cache_key,
            usage_query_kind=_qk,
            prefer_local=True,
            only_local=False,
            allow_local=True,
        )
        if isinstance(result, FitCheckResult):
            return {"score": result.score, "reasoning": result.reasoning, "thoughts": result.thoughts}
        parsed = _parse_fit_check_fallback_from_parsers(str(result))
        return {"score": parsed.score, "reasoning": parsed.reasoning, "thoughts": parsed.thoughts}
    except Exception as e:
        logger.warning("fit_check structured output failed: %s", e)
        raw = _llm_invoke_with_retry(
            llm,
            messages,
            user=user,
            job_cache_key=job_cache_key,
            usage_query_kind=_qk,
            prefer_local=True,
            only_local=False,
            allow_local=True,
        )
        content = raw.content if hasattr(raw, "content") else str(raw)
        parsed = _parse_fit_check_fallback_from_parsers(content)
        return {"score": parsed.score, "reasoning": parsed.reasoning, "thoughts": parsed.thoughts}


def run_matching(
    resume_text: str,
    job_description: str,
    llm,
    *,
    user,
    prompt_template: str = None,
    prompt_system: str = None,
    prompt_user: str = None,
    prompt_legacy: str = None,
    job_cache_key: str | None = None,
    usage_query_kind: str | None = None,
    return_debug: bool = False,
) -> dict:
    """Returns { score: int, reasoning: str, interview_probability: int|None }. Score 0-100.
    Used after job search for independent LLM match score.
    Uses raw LLM call + parser (no structured output) so all providers return parseable text."""
    fmt = dict(resume_text=resume_text, job_description=job_description)
    if prompt_template and str(prompt_template).strip():
        messages = [HumanMessage(content=_format_prompt(str(prompt_template).strip(), **fmt))]
    else:
        leg = (prompt_legacy or "").strip()
        st = (prompt_system or "").strip()
        ut = (prompt_user or "").strip()
        if not leg and not st and not ut:
            messages = build_llm_messages_for_prompt(
                legacy_combined=None,
                system_template=DEFAULT_MATCHING_SYSTEM,
                user_template=DEFAULT_MATCHING_USER,
                format_kwargs=fmt,
            )
        else:
            messages = build_llm_messages_for_prompt(
                legacy_combined=leg or None,
                system_template=st or None,
                user_template=ut or None,
                format_kwargs=fmt,
            )
    from .llm_gateway import USAGE_QUERY_MATCHING

    _qk = usage_query_kind or USAGE_QUERY_MATCHING
    dbg = "\n\n---\n\n".join(f"{type(m).__name__}:{getattr(m, 'content', '')}" for m in messages)
    logger.info("[matching] messages=%s total_chars=%s", len(messages), len(dbg))
    raw = _llm_invoke_with_retry(
        llm,
        messages,
        user=user,
        job_cache_key=job_cache_key,
        usage_query_kind=_qk,
        prefer_local=True,
        only_local=True,
        allow_local=True,
    )
    content = getattr(raw, "content", None)
    if content is None:
        content = str(raw) if raw is not None else ""
    if not isinstance(content, str):
        content = str(content)
    logger.info("[matching] raw response length=%s first_500=%r", len(content), (content or "")[:500])
    parsed = _parse_fit_check_fallback_from_parsers(content or "")
    out = {
        "score": parsed.score,
        "reasoning": parsed.reasoning,
        "interview_probability": parsed.interview_probability,
    }
    if return_debug:
        out["raw_llm_content"] = content or ""
    return out


# --- State Definition ---
class _AgentStateBase(TypedDict):
    # Parsed PDF at workflow start; after each Writer completes, updated to that Writer's output (canonical body for later steps).
    resume_text: str
    job_description: str
    optimized_resume: str
    ats_score: int
    recruiter_score: int
    feedback: Annotated[List[str], operator.add]
    iteration_count: int
    llm: any
    writer_prompt_template: str
    ats_judge_prompt_template: str
    recruiter_judge_prompt_template: str
    max_iterations: int


class AgentState(_AgentStateBase, total=False):
    last_ats_json: dict  # Parsed ATS judge JSON when using custom prompts
    last_recruiter_json: dict  # Parsed recruiter judge JSON when available
    score_threshold: int  # Exit when avg(ats_score, recruiter_score) >= this (default 85)
    job_cache_key: str  # Stable id for LLM gateway pinning (e.g. optimized resume id)
    source_resume_text: str  # PDF extraction; immutable fact anchor for Writer across steps
    writer_job_description: str  # Role-focused JD excerpt for Writer (optional; else full JD)
    judge_job_description: str  # JD excerpt for ATS/Recruiter judges
    job_title: str  # Optional title for JD cleanse prompt
    optimization_notes: str
    pipeline_skills_json: str
    job_highlights: str
    retrieval_context: str
    optimizer_context_budget: dict  # Char counts / retrieval debug for UI
    user_id: int  # Owner for tenant-scoped LLM gateway calls
    writer_prompt_system: str
    writer_prompt_user: str
    writer_prompt_legacy: str
    ats_judge_prompt_system: str
    ats_judge_prompt_user: str
    ats_judge_prompt_legacy: str
    recruiter_judge_prompt_system: str
    recruiter_judge_prompt_user: str
    recruiter_judge_prompt_legacy: str
    # Per-node debug / metering (must be in schema or LangGraph drops them from stream updates)
    debug: bool
    input_tokens: int
    output_tokens: int
    tokens_estimated: bool
    debug_prompt: str
    debug_messages: list
    debug_format_summary: dict
    raw_llm_response: str
    raw_llm_response_retry: str
    raw_llm_result_type: str
    raw_llm_result_type_retry: str
    parse_info: dict
    jd_cleansed: bool  # True when jd_cleanse_node successfully shrank the JD

# --- Agent Nodes ---

# Known state keys so we never trigger KeyError on stray keys (e.g. from merged JSON/session).
_STATE_KEYS = frozenset({
    "resume_text", "job_description", "optimized_resume", "source_resume_text", "ats_score", "recruiter_score",
    "feedback", "iteration_count", "llm", "writer_prompt_template", "ats_judge_prompt_template",
    "recruiter_judge_prompt_template",
    "writer_prompt_system", "writer_prompt_user", "writer_prompt_legacy",
    "ats_judge_prompt_system", "ats_judge_prompt_user", "ats_judge_prompt_legacy",
    "recruiter_judge_prompt_system", "recruiter_judge_prompt_user", "recruiter_judge_prompt_legacy",
    "debug", "max_iterations", "score_threshold", "job_cache_key",
    "writer_job_description", "optimization_notes", "pipeline_skills_json", "job_highlights",
    "retrieval_context", "optimizer_context_budget", "user_id",
    "judge_job_description", "job_title", "jd_cleansed",
    "input_tokens", "output_tokens", "tokens_estimated",
    "debug_prompt", "debug_messages", "debug_format_summary",
    "raw_llm_response", "raw_llm_response_retry", "raw_llm_result_type", "raw_llm_result_type_retry",
    "parse_info", "last_ats_json", "last_recruiter_json",
})


def _messages_to_debug_payload(messages) -> tuple[str, list[dict]]:
    """Build (debug_prompt text, debug_messages list) for AgentLog / UI."""
    dm: list[dict] = []
    parts: list[str] = []
    for m in messages or []:
        role = "user"
        if isinstance(m, SystemMessage):
            role = "system"
        content = getattr(m, "content", "") or ""
        dm.append({"role": role, "content": content})
        parts.append(f"{m.__class__.__name__}:\n{content}")
    return "\n\n---\n\n".join(parts), dm


def _format_field_summary(fmt: dict) -> dict:
    """Compact map of template format kwargs: char length + whether empty (for debug UI)."""
    out: dict = {}
    for key, val in (fmt or {}).items():
        s = "" if val is None else str(val)
        out[key] = {"chars": len(s), "empty": not s.strip(), "preview": (s[:240] + ("…" if len(s) > 240 else ""))}
    return out


def _state_get(state: dict, key: str, default=None):
    """Get from state only if key is in _STATE_KEYS; otherwise return default. Avoids KeyError on stray keys."""
    if key not in _STATE_KEYS:
        return default
    try:
        return state.get(key, default) if isinstance(state, dict) else getattr(state, key, default)
    except KeyError:
        return default


def _judge_job_description_from_state(state: dict) -> str:
    return (
        (_state_get(state, "judge_job_description") or "").strip()
        or (_state_get(state, "writer_job_description") or "").strip()
        or (_state_get(state, "job_description") or "").strip()
    )


def writer_node(state: AgentState):
    """
    Writer prompt `{resume_text}` = document to edit this invocation: non-empty `optimized_resume` first
    (e.g. step-mode revision with draft + PDF), else state `resume_text` (PDF text at workflow start, then the
    last Writer output after each Writer step — LangGraph merges `resume_text` from our return dict).
    `{source_resume_text}` stays the original PDF text for factual grounding.
    """
    llm = _state_get(state, "llm")
    prior = (_state_get(state, "optimized_resume") or "").strip()
    base = (_state_get(state, "resume_text") or "").strip()
    src = (_state_get(state, "source_resume_text") or "").strip()
    if not src:
        src = base
    # Same as state.resume_text whenever prior is empty; when prior is set without base synced (rare), prefer prior.
    resume_body_this_step = prior if prior else base
    jd_full = (_state_get(state, "job_description") or "").strip()
    jd_writer = (_state_get(state, "writer_job_description") or "").strip()
    if not jd_writer:
        jd_writer = jd_full
    source_for_prompt = (
        src
        if should_include_source_resume(src, resume_body_this_step)
        else omitted_duplicate_field_note()
    )
    full_jd_for_prompt = (
        jd_full
        if should_include_full_job_description(jd_writer, jd_full)
        else omitted_duplicate_field_note()
    )
    fmt = dict(
        resume_text=resume_body_this_step,
        job_description=jd_writer,
        full_job_description=full_jd_for_prompt,
        feedback=", ".join(_state_get(state, "feedback") or []),
        optimized_resume=prior,
        source_resume_text=source_for_prompt,
        optimization_notes=_state_get(state, "optimization_notes") or "(none)",
        pipeline_skills_json=_state_get(state, "pipeline_skills_json") or "(none)",
        job_highlights=_state_get(state, "job_highlights") or "(none)",
        retrieval_context=_state_get(state, "retrieval_context") or "(none)",
    )
    legacy = (_state_get(state, "writer_prompt_legacy") or "").strip()
    sys_t = (_state_get(state, "writer_prompt_system") or "").strip()
    usr_t = (_state_get(state, "writer_prompt_user") or "").strip()
    if not legacy and not sys_t and not usr_t:
        legacy = (_state_get(state, "writer_prompt_template") or DEFAULT_WRITER_PROMPT).strip()

    messages = build_llm_messages_for_prompt(
        legacy_combined=legacy or None,
        system_template=sys_t or None,
        user_template=usr_t or None,
        format_kwargs=fmt,
    )
    dbg_prompt, dm = _messages_to_debug_payload(messages)
    logger.warning(
        "[writer] prompts sent to LLM (%s message(s), total_chars=%s):\n%s",
        len(messages),
        len(dbg_prompt),
        dbg_prompt[:8000] + ("..." if len(dbg_prompt) > 8000 else ""),
    )
    # Always persist prompt + format summary on AgentLog (schema fields survive LangGraph stream).
    out = {
        "debug_prompt": dbg_prompt,
        "debug_messages": dm,
        "debug_format_summary": _format_field_summary(fmt),
    }
    from .llm_gateway import USAGE_QUERY_OPTIMIZER_WRITER

    response = _llm_invoke_with_retry(
        llm,
        messages,
        user=_state_user(state),
        job_cache_key=_state_get(state, "job_cache_key"),
        usage_query_kind=USAGE_QUERY_OPTIMIZER_WRITER,
        prefer_local=False,
        allow_local=False,
    )
    out.update({
        "optimized_resume": response.content,
        "resume_text": response.content,
        "iteration_count": (_state_get(state, "iteration_count") or 0) + 1
    })
    usage = _normalize_token_usage(response, None, dbg_prompt)
    out["input_tokens"] = usage["input_tokens"]
    out["output_tokens"] = usage["output_tokens"]
    if usage.get("tokens_estimated"):
        out["tokens_estimated"] = True
    push_node_llm_debug(
        _state_get(state, "job_cache_key"),
        _debug_payload_from_node_out(
            step="writer",
            messages_debug=dm,
            debug_prompt=dbg_prompt,
            format_summary=out["debug_format_summary"],
            input_tokens=out.get("input_tokens"),
            output_tokens=out.get("output_tokens"),
            tokens_estimated=bool(out.get("tokens_estimated")),
            raw_llm_response=_serialize_llm_result_for_log(response),
            optimized_resume=str(response.content or "")[:4000],
        ),
    )
    return out


def _unstructured_judge_invoke(
    llm,
    messages,
    *,
    user,
    label: str,
    structured_schema: type,
    parse_fallback,
    job_cache_key: str | None,
    usage_query_kind: str,
    config: dict | None = None,
    prefer_local: bool = False,
    allow_local: bool = False,
) -> tuple[ScoreFeedback | AtsJudgeResult, Optional[dict], Any, dict]:
    """
    Raw LLM invoke + text/JSON parse (no with_structured_output).
    Some providers return None from structured output; this path matches run_matching.
    """
    raw = _llm_invoke_with_retry(
        llm,
        messages,
        user=user,
        config=config,
        structured_schema=None,
        job_cache_key=job_cache_key,
        usage_query_kind=usage_query_kind,
        prefer_local=prefer_local,
        allow_local=allow_local,
    )
    text = _llm_message_content_to_text(getattr(raw, "content", None) if raw is not None else None)
    if not text.strip() and raw is not None:
        text = _llm_message_content_to_text(raw)
    logger.info(
        "[%s] unstructured response length=%s first_300=%r",
        label,
        len(text),
        (text or "")[:300],
    )
    data, last_json = parse_fallback(text or "", label)
    return data, last_json, raw, {"path": "unstructured"}


def _resolve_judge_scores(
    data: ScoreFeedback | AtsJudgeResult,
    last_json: Optional[dict],
) -> tuple[int, str, Optional[dict]]:
    """Map structured or fallback judge output to score, feedback text, and optional JSON blob."""
    if isinstance(data, AtsJudgeResult):
        json_out = last_json if last_json is not None else data.model_dump()
        return data.ats_match_score, data.feedback_text(), json_out
    if isinstance(data, ScoreFeedback):
        return data.score, data.feedback, last_json
    raise TypeError(f"Unexpected judge result type: {type(data)!r}")


def _judge_node(
    state: AgentState,
    *,
    label: str,
    template_state_key: str,
    system_state_key: str,
    user_state_key: str,
    legacy_state_key: str,
    default_template: str,
    default_system: str,
    default_user: str,
    score_key: str,
    feedback_prefix: str,
    last_json_state_key: str,
    structured_schema: type = ScoreFeedback,
    parse_fallback=_parse_score_fallback,
    use_structured_output: bool = True,
    prefer_local: bool | None = None,
):
    llm = _state_get(state, "llm")
    draft = (_state_get(state, "optimized_resume") or "").strip() or (_state_get(state, "resume_text") or "").strip()
    draft = truncate_judge_resume(draft)
    judge_jd = truncate_judge_job_description(_judge_job_description_from_state(state))
    if prefer_local is None:
        prefer_local = _judges_prefer_local()
    fmt = dict(
        optimized_resume=draft,
        job_description=judge_jd,
    )
    legacy = (_state_get(state, legacy_state_key) or "").strip()
    sys_t = (_state_get(state, system_state_key) or "").strip()
    usr_t = (_state_get(state, user_state_key) or "").strip()
    if not legacy and not sys_t and not usr_t:
        legacy = (_state_get(state, template_state_key) or default_template).strip()

    messages = build_llm_messages_for_prompt(
        legacy_combined=legacy or None,
        system_template=sys_t or None,
        user_template=usr_t or None,
        format_kwargs=fmt,
    )
    dbg_prompt, dm = _messages_to_debug_payload(messages)
    logger.warning(
        "[%s] prompts sent to LLM (%s message(s), chars=%s, prefer_local=%s):\n%s",
        label,
        len(messages),
        len(dbg_prompt),
        prefer_local,
        dbg_prompt[:8000] + ("..." if len(dbg_prompt) > 8000 else ""),
    )
    out = {
        "debug_prompt": dbg_prompt,
        "debug_messages": dm,
        "debug_format_summary": _format_field_summary(fmt),
    }
    from .llm_gateway import (
        USAGE_QUERY_OPTIMIZER_ATS_JUDGE,
        USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE,
    )

    _usage_qk = (
        USAGE_QUERY_OPTIMIZER_ATS_JUDGE
        if label == "ats_judge"
        else USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE
    )
    last_json = None
    raw = None
    parse_info: dict = {"path": "unstructured" if not use_structured_output else "structured"}
    usage_callback = TokenUsageCallback()
    invoke_config = {"callbacks": [usage_callback]}
    data: ScoreFeedback | AtsJudgeResult
    owner_user = _state_user(state)

    if not use_structured_output:
        data, last_json, raw, parse_info = _unstructured_judge_invoke(
            llm,
            messages,
            user=owner_user,
            label=label,
            structured_schema=structured_schema,
            parse_fallback=parse_fallback,
            job_cache_key=_state_get(state, "job_cache_key"),
            usage_query_kind=_usage_qk,
            config=invoke_config,
            prefer_local=prefer_local,
            allow_local=False,
        )
        out["raw_llm_response"] = _serialize_llm_result_for_log(raw)
        out["raw_llm_result_type"] = type(raw).__name__ if raw is not None else "NoneType"
    else:
        try:
            structured_result = _llm_invoke_with_retry(
                llm,
                messages,
                user=owner_user,
                config=invoke_config,
                structured_schema=structured_schema,
                job_cache_key=_state_get(state, "job_cache_key"),
                usage_query_kind=_usage_qk,
                prefer_local=prefer_local,
                allow_local=False,
            )
            out["raw_llm_response"] = _serialize_llm_result_for_log(structured_result)
            out["raw_llm_result_type"] = (
                type(structured_result).__name__
                if structured_result is not None
                else "NoneType"
            )

            if structured_result is None:
                parse_info = {"path": "structured_null"}
                logger.warning(
                    "[%s] structured output returned None; using unstructured invoke",
                    label,
                )
                data, last_json, raw, parse_info = _unstructured_judge_invoke(
                    llm,
                    messages,
                    user=owner_user,
                    label=label,
                    structured_schema=structured_schema,
                    parse_fallback=parse_fallback,
                    job_cache_key=_state_get(state, "job_cache_key"),
                    usage_query_kind=_usage_qk,
                    config=invoke_config,
                    prefer_local=prefer_local,
                    allow_local=False,
                )
                out["raw_llm_response_retry"] = _serialize_llm_result_for_log(raw)
            else:
                data, last_json = _coerce_structured_judge_result(
                    structured_result, structured_schema, parse_fallback, label
                )
                if _is_default_judge_fallback(data):
                    parse_info = {"path": "structured_default_fallback"}
                    logger.warning(
                        "[%s] structured parse failed; retrying unstructured",
                        label,
                    )
                    data, last_json, raw, parse_info = _unstructured_judge_invoke(
                        llm,
                        messages,
                        user=owner_user,
                        label=label,
                        structured_schema=structured_schema,
                        parse_fallback=parse_fallback,
                        job_cache_key=_state_get(state, "job_cache_key"),
                        usage_query_kind=_usage_qk,
                        config=invoke_config,
                        prefer_local=prefer_local,
                        allow_local=False,
                    )
                    out["raw_llm_response_retry"] = _serialize_llm_result_for_log(raw)
                    if _is_default_judge_fallback(data):
                        parse_info["path"] = "unstructured_retry_default"
                    else:
                        parse_info["path"] = "unstructured_retry"
        except Exception as e:
            parse_info = {"path": "structured_exception", "error": str(e)}
            logger.warning("%s structured output failed: %s", label, e)
            data, last_json, raw, parse_info = _unstructured_judge_invoke(
                llm,
                messages,
                user=owner_user,
                label=label,
                structured_schema=structured_schema,
                parse_fallback=parse_fallback,
                job_cache_key=_state_get(state, "job_cache_key"),
                usage_query_kind=_usage_qk,
                config=invoke_config,
                prefer_local=prefer_local,
                allow_local=False,
            )
            out["raw_llm_response"] = _serialize_llm_result_for_log(raw)
            out["raw_llm_result_type"] = type(raw).__name__ if raw is not None else "NoneType"
            if _is_default_judge_fallback(data):
                parse_info["path"] = "exception_retry_default"
            else:
                parse_info["path"] = "exception_retry_ok"

    out["parse_info"] = parse_info

    score, feedback_text, json_out = _resolve_judge_scores(data, last_json)
    out.update({
        score_key: score,
        "feedback": [f"{feedback_prefix}{feedback_text}"],
    })
    if json_out is not None:
        out[last_json_state_key] = _normalize_dict_keys(json_out)
    if raw is not None:
        usage = _normalize_token_usage(raw, getattr(raw, "llm_output", None), dbg_prompt)
        out["input_tokens"] = usage["input_tokens"]
        out["output_tokens"] = usage["output_tokens"]
        if usage.get("tokens_estimated"):
            out["tokens_estimated"] = True
    elif usage_callback.total_input_tokens or usage_callback.total_output_tokens:
        out["input_tokens"] = usage_callback.total_input_tokens
        out["output_tokens"] = usage_callback.total_output_tokens
    else:
        usage = _normalize_token_usage(None, None, dbg_prompt, feedback_text if data else None)
        out["input_tokens"] = usage["input_tokens"]
        out["output_tokens"] = usage["output_tokens"]
        if usage.get("tokens_estimated"):
            out["tokens_estimated"] = True
    push_node_llm_debug(
        _state_get(state, "job_cache_key"),
        _debug_payload_from_node_out(
            step=label,
            messages_debug=dm,
            debug_prompt=dbg_prompt,
            format_summary=out.get("debug_format_summary") or {},
            input_tokens=out.get("input_tokens"),
            output_tokens=out.get("output_tokens"),
            tokens_estimated=bool(out.get("tokens_estimated")),
            raw_llm_response=out.get("raw_llm_response"),
            raw_llm_response_retry=out.get("raw_llm_response_retry"),
            parse_info=out.get("parse_info"),
            feedback=out.get("feedback"),
        ),
    )
    return out


_PROMPT_DEBUG_KEYS = frozenset({
    "debug_prompt",
    "debug_messages",
    "debug_format_summary",
    "raw_llm_response",
    "raw_llm_response_retry",
    "raw_llm_result_type",
    "raw_llm_result_type_retry",
    "parse_info",
})


def can_view_optimizer_llm_debug(request) -> bool:
    """
    Full LLM prompt debug (system/user/raw) is staff-only — product secret sauce.

    True when the *real* signed-in user is staff/superuser, including while
    django-hijack impersonating another account.
    """
    from .tenancy import get_real_user

    real = get_real_user(request) if request is not None else None
    if real is None:
        return False
    return bool(getattr(real, "is_staff", False) or getattr(real, "is_superuser", False))


def redact_agent_log_thought(thought: dict | None, *, include_prompt_debug: bool) -> dict:
    """Return a copy of thought safe for the current viewer."""
    if not isinstance(thought, dict):
        return {}
    if include_prompt_debug:
        return dict(thought)
    public = {k: v for k, v in thought.items() if k not in _PROMPT_DEBUG_KEYS}
    public.pop("optimized_resume", None)
    public.pop("resume_text", None)
    return public


def _split_system_user_from_messages(messages) -> tuple[str, str]:
    system_parts: list[str] = []
    user_parts: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if (m.get("role") or "").lower() == "system":
            system_parts.append(content)
        else:
            user_parts.append(content)
    return "\n\n".join(system_parts), "\n\n".join(user_parts)


def format_agent_log_thought(thought, *, include_prompt_debug: bool = True) -> str:
    """Human-readable agent log: System Prompt / User Prompt / Raw Output (+ tokens)."""
    if thought is None:
        return ""
    if isinstance(thought, str):
        return thought
    if not isinstance(thought, dict):
        return str(thought)

    thought = redact_agent_log_thought(thought, include_prompt_debug=include_prompt_debug)
    import json as _json

    parts: list[str] = []

    if include_prompt_debug:
        system_text, user_text = _split_system_user_from_messages(thought.get("debug_messages"))
        if not system_text and not user_text:
            flat = (thought.get("debug_prompt") or "").strip()
            if flat:
                user_text = flat
        parts.append("--- System Prompt ---\n" + (system_text or "(none — combined/legacy user message only)"))
        parts.append("--- User Prompt ---\n" + (user_text or "(empty)"))

    in_tok = thought.get("input_tokens")
    out_tok = thought.get("output_tokens")
    if in_tok is not None or out_tok is not None:
        est = " (estimated)" if thought.get("tokens_estimated") else ""
        parts.append(
            f"--- Tokens ---\n"
            f"input={in_tok if in_tok is not None else '—'}  "
            f"output={out_tok if out_tok is not None else '—'}{est}"
        )

    if include_prompt_debug:
        raw = (thought.get("raw_llm_response") or "").strip()
        raw_retry = (thought.get("raw_llm_response_retry") or "").strip()
        if raw or raw_retry:
            body = raw or "(empty)"
            if raw_retry:
                body = body + "\n\n--- Raw Output (retry) ---\n" + raw_retry
            parts.append("--- Raw Output ---\n" + body)
        else:
            resume_out = thought.get("optimized_resume")
            if isinstance(resume_out, str) and resume_out.strip():
                s = resume_out.strip()
                parts.append("--- Raw Output ---\n" + s[:8000] + ("…" if len(s) > 8000 else ""))
            else:
                fb = thought.get("feedback")
                if fb:
                    parts.append("--- Raw Output ---\n" + (fb if isinstance(fb, str) else str(fb)))

        parse_info = thought.get("parse_info")
        if parse_info:
            try:
                parts.append(
                    "--- Parse info ---\n"
                    + _json.dumps(parse_info, indent=2, ensure_ascii=False, default=str)
                )
            except Exception:
                parts.append("--- Parse info ---\n" + str(parse_info))
    else:
        for key in ("ats_score", "recruiter_score"):
            if key in thought and thought[key] is not None:
                parts.append(f"{key}: {thought[key]}")
        fb = thought.get("feedback")
        if fb:
            parts.append(str(fb) if not isinstance(fb, str) else fb)

    if parts:
        return "\n\n".join(parts)
    return _json.dumps(thought, indent=2, ensure_ascii=False, default=str)


def ats_judge_node(state: AgentState):
    return _judge_node(
        state,
        label="ats_judge",
        template_state_key="ats_judge_prompt_template",
        system_state_key="ats_judge_prompt_system",
        user_state_key="ats_judge_prompt_user",
        legacy_state_key="ats_judge_prompt_legacy",
        default_template=DEFAULT_ATS_JUDGE_PROMPT,
        default_system=DEFAULT_ATS_JUDGE_SYSTEM,
        default_user=DEFAULT_ATS_JUDGE_USER,
        score_key="ats_score",
        feedback_prefix="ATS: ",
        last_json_state_key="last_ats_json",
        structured_schema=AtsJudgeResult,
        parse_fallback=_parse_ats_judge_fallback,
        use_structured_output=False,
    )


def recruiter_judge_node(state: AgentState):
    return _judge_node(
        state,
        label="recruiter_judge",
        template_state_key="recruiter_judge_prompt_template",
        system_state_key="recruiter_judge_prompt_system",
        user_state_key="recruiter_judge_prompt_user",
        legacy_state_key="recruiter_judge_prompt_legacy",
        default_template=DEFAULT_RECRUITER_JUDGE_PROMPT,
        default_system=DEFAULT_RECRUITER_JUDGE_SYSTEM,
        default_user=DEFAULT_RECRUITER_JUDGE_USER,
        score_key="recruiter_score",
        feedback_prefix="Recruiter: ",
        last_json_state_key="last_recruiter_json",
        use_structured_output=False,
    )


def jd_cleanse_node(state: AgentState):
    """
    Shrink a noisy job posting to core requirements via the JD Cleanse Prompt Library templates.

    Overwrites ``job_description``, ``writer_job_description``, and ``judge_job_description`` so
    every later workflow step consumes the cleansed text.
    """
    from .jd_cleanser import JDCleanserService
    from .llm_gateway import USAGE_QUERY_JD_CLEANSE
    from .prompt_store import build_jd_cleanse_llm_messages

    full_jd = (_state_get(state, "job_description") or "").strip()
    title = (_state_get(state, "job_title") or "").strip()
    if not full_jd:
        return {
            "job_description": "",
            "writer_job_description": "",
            "judge_job_description": "",
            "jd_cleansed": False,
            "debug_prompt": "",
            "debug_messages": [],
            "debug_format_summary": _format_field_summary(
                {"title": title, "job_description": ""}
            ),
        }

    jd_for_prompt = full_jd[:8000]
    fmt = {"title": title, "job_description": jd_for_prompt}
    messages = build_jd_cleanse_llm_messages(
        None,
        title=title,
        job_description=jd_for_prompt,
    )
    dbg_prompt, dm = _messages_to_debug_payload(messages)
    logger.warning(
        "[jd_cleanse] prompts sent to LLM (%s message(s), chars=%s):\n%s",
        len(messages),
        len(dbg_prompt),
        dbg_prompt[:8000] + ("..." if len(dbg_prompt) > 8000 else ""),
    )
    out: dict = {
        "debug_prompt": dbg_prompt,
        "debug_messages": dm,
        "debug_format_summary": _format_field_summary(fmt),
    }

    llm = _state_get(state, "llm")
    owner_user = _state_user(state)
    cleansed = ""
    raw = None
    try:
        raw = _llm_invoke_with_retry(
            llm,
            messages,
            user=owner_user,
            job_cache_key=_state_get(state, "job_cache_key"),
            usage_query_kind=USAGE_QUERY_JD_CLEANSE,
            # Optimizer JD cleanse follows remote-first policy (same as Writer/judges).
            prefer_local=False,
            only_local=False,
            allow_local=False,
        )
        cleansed = (_llm_message_content_to_text(getattr(raw, "content", None) if raw is not None else None) or "").strip()
        if not cleansed and raw is not None:
            cleansed = (_llm_message_content_to_text(raw) or "").strip()
    except Exception as e:
        logger.warning("[jd_cleanse] LLM cleanse failed, using heuristic: %s", e)

    used_heuristic = False
    if len(cleansed) < 50:
        cleansed = JDCleanserService.cleanse_heuristically(full_jd, title=title, max_chars=5000)
        used_heuristic = True

    cleansed = (cleansed or "").strip()
    out.update(
        {
            "job_description": cleansed,
            "writer_job_description": cleansed,
            "judge_job_description": cleansed,
            "jd_cleansed": bool(cleansed) and not used_heuristic,
            "raw_llm_response": _serialize_llm_result_for_log(raw) if raw is not None else "",
            "parse_info": {
                "path": "heuristic_fallback" if used_heuristic else "llm",
                "input_chars": len(full_jd),
                "output_chars": len(cleansed),
            },
        }
    )
    if raw is not None:
        usage = _normalize_token_usage(raw, None, dbg_prompt)
        out["input_tokens"] = usage["input_tokens"]
        out["output_tokens"] = usage["output_tokens"]
        if usage.get("tokens_estimated"):
            out["tokens_estimated"] = True

    push_node_llm_debug(
        _state_get(state, "job_cache_key"),
        _debug_payload_from_node_out(
            step="jd_cleanse",
            messages_debug=dm,
            debug_prompt=dbg_prompt,
            format_summary=out["debug_format_summary"],
            input_tokens=out.get("input_tokens"),
            output_tokens=out.get("output_tokens"),
            tokens_estimated=bool(out.get("tokens_estimated")),
            raw_llm_response=out.get("raw_llm_response"),
            optimized_resume=cleansed[:4000],
        ),
    )
    return out


# --- Graph Logic ---

VALID_STEP_IDS = frozenset({"writer", "ats_judge", "recruiter_judge", "jd_cleanse"})


def create_workflow_from_steps(steps: list, max_iterations: int = 3, loop_to: Optional[str] = None):
    """
    Build a compiled StateGraph from an ordered list of step ids. No recursion:
    the flow runs once from first step to last, then END.
    steps: e.g. ["jd_cleanse", "writer", "ats_judge", "recruiter_judge"]
    Each position in the list gets its own graph node so repeated step types run in sequence
    without overwriting edges (one node per invocation, not per step type).
    loop_to: ignored (kept for API compatibility).
    """
    if not steps:
        raise ValueError("workflow_steps must not be empty")
    invalid = [s for s in steps if s not in VALID_STEP_IDS]
    if invalid:
        raise ValueError(f"Invalid step id(s): {invalid}. Allowed: {sorted(VALID_STEP_IDS)}")
    nodes_map = {
        "writer": writer_node,
        "ats_judge": ats_judge_node,
        "recruiter_judge": recruiter_judge_node,
        "jd_cleanse": jd_cleanse_node,
    }
    workflow = StateGraph(AgentState)
    for i, step_id in enumerate(steps):
        node_name = f"step_{i}"
        handler = nodes_map[step_id]
        workflow.add_node(node_name, handler)
    workflow.set_entry_point("step_0")
    for i in range(len(steps) - 1):
        workflow.add_edge(f"step_{i}", f"step_{i + 1}")
    workflow.add_edge(f"step_{len(steps) - 1}", END)
    return workflow.compile()


def create_workflow():
    """Default workflow: writer -> ats_judge -> recruiter_judge -> END (single pass)."""
    return create_workflow_from_steps(["writer", "ats_judge", "recruiter_judge"])
