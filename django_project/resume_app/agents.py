from typing import TypedDict, List, Annotated, Optional
import operator
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from langgraph.graph import StateGraph, END

from .callbacks import TokenUsageCallback
from .llm_factory import get_llm
from .parsers import (
    ScoreFeedback,
    FitCheckResult,
    parse_score_fallback as _parse_score_fallback,
    parse_fit_check_fallback as _parse_fit_check_fallback_from_parsers,
    normalize_dict_keys as _normalize_dict_keys,
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

logger = logging.getLogger(__name__)

# Chars-per-token heuristic when provider does not report usage (e.g. some Ollama setups)
_CHARS_PER_TOKEN_ESTIMATE = 4


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
    max_attempts=2,
    config=None,
    structured_schema=None,
    job_cache_key: str | None = None,
    usage_query_kind: str | None = None,
):
    from .llm_gateway import invoke_llm_messages

    return invoke_llm_messages(
        messages,
        job_cache_key=job_cache_key,
        structured_schema=structured_schema,
        config=config,
        llm_override=llm,
        max_attempts_per_model=max_attempts,
        usage_query_kind=usage_query_kind,
    )


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
            structured_schema=FitCheckResult,
            job_cache_key=job_cache_key,
            usage_query_kind=_qk,
        )
        if isinstance(result, FitCheckResult):
            return {"score": result.score, "reasoning": result.reasoning, "thoughts": result.thoughts}
        parsed = _parse_fit_check_fallback_from_parsers(str(result))
        return {"score": parsed.score, "reasoning": parsed.reasoning, "thoughts": parsed.thoughts}
    except Exception as e:
        logger.warning("fit_check structured output failed: %s", e)
        raw = _llm_invoke_with_retry(
            llm, messages, job_cache_key=job_cache_key, usage_query_kind=_qk
        )
        content = raw.content if hasattr(raw, "content") else str(raw)
        parsed = _parse_fit_check_fallback_from_parsers(content)
        return {"score": parsed.score, "reasoning": parsed.reasoning, "thoughts": parsed.thoughts}


def run_matching(
    resume_text: str,
    job_description: str,
    llm,
    prompt_template: str = None,
    prompt_system: str = None,
    prompt_user: str = None,
    prompt_legacy: str = None,
    job_cache_key: str | None = None,
    usage_query_kind: str | None = None,
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
        llm, messages, job_cache_key=job_cache_key, usage_query_kind=_qk
    )
    content = getattr(raw, "content", None)
    if content is None:
        content = str(raw) if raw is not None else ""
    if not isinstance(content, str):
        content = str(content)
    logger.info("[matching] raw response length=%s first_500=%r", len(content), (content or "")[:500])
    parsed = _parse_fit_check_fallback_from_parsers(content or "")
    return {
        "score": parsed.score,
        "reasoning": parsed.reasoning,
        "interview_probability": parsed.interview_probability,
    }


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
    writer_prompt_system: str
    writer_prompt_user: str
    writer_prompt_legacy: str
    ats_judge_prompt_system: str
    ats_judge_prompt_user: str
    ats_judge_prompt_legacy: str
    recruiter_judge_prompt_system: str
    recruiter_judge_prompt_user: str
    recruiter_judge_prompt_legacy: str

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
})


def _state_get(state: dict, key: str, default=None):
    """Get from state only if key is in _STATE_KEYS; otherwise return default. Avoids KeyError on stray keys."""
    if key not in _STATE_KEYS:
        return default
    try:
        return state.get(key, default) if isinstance(state, dict) else getattr(state, key, default)
    except KeyError:
        return default


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
    fmt = dict(
        resume_text=resume_body_this_step,
        job_description=_state_get(state, "job_description") or "",
        feedback=", ".join(_state_get(state, "feedback") or []),
        optimized_resume=prior,
        source_resume_text=src,
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
    dbg_prompt = "\n\n---\n\n".join(
        f"{m.__class__.__name__}:\n{getattr(m, 'content', '')}" for m in messages
    )
    logger.warning(
        "[writer] prompts sent to LLM (%s message(s), total_chars=%s):\n%s",
        len(messages),
        len(dbg_prompt),
        dbg_prompt[:8000] + ("..." if len(dbg_prompt) > 8000 else ""),
    )
    if _state_get(state, "debug"):
        dm = []
        for m in messages:
            role = "user"
            if isinstance(m, SystemMessage):
                role = "system"
            dm.append({"role": role, "content": getattr(m, "content", "")})
        out = {"debug_prompt": dbg_prompt, "debug_messages": dm}
    else:
        out = {}
    from .llm_gateway import USAGE_QUERY_OPTIMIZER_WRITER

    response = _llm_invoke_with_retry(
        llm,
        messages,
        job_cache_key=_state_get(state, "job_cache_key"),
        usage_query_kind=USAGE_QUERY_OPTIMIZER_WRITER,
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
    return out


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
):
    llm = _state_get(state, "llm")
    draft = (_state_get(state, "optimized_resume") or "").strip() or (_state_get(state, "resume_text") or "").strip()
    fmt = dict(
        optimized_resume=draft,
        job_description=_state_get(state, "job_description") or "",
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
    dbg_prompt = "\n\n---\n\n".join(
        f"{m.__class__.__name__}:\n{getattr(m, 'content', '')}" for m in messages
    )
    logger.warning(
        "[%s] prompts sent to LLM (%s message(s), chars=%s):\n%s",
        label,
        len(messages),
        len(dbg_prompt),
        dbg_prompt[:8000] + ("..." if len(dbg_prompt) > 8000 else ""),
    )
    if _state_get(state, "debug"):
        dm = []
        for m in messages:
            role = "user"
            if isinstance(m, SystemMessage):
                role = "system"
            dm.append({"role": role, "content": getattr(m, "content", "")})
        out = {"debug_prompt": dbg_prompt, "debug_messages": dm}
    else:
        out = {}
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
    usage_callback = TokenUsageCallback()
    try:
        result = _llm_invoke_with_retry(
            llm,
            messages,
            config={"callbacks": [usage_callback]},
            structured_schema=ScoreFeedback,
            job_cache_key=_state_get(state, "job_cache_key"),
            usage_query_kind=_usage_qk,
        )
        if isinstance(result, ScoreFeedback):
            data = result
        else:
            content = str(result)
            logger.warning("[%s] raw LLM output (structured returned non-ScoreFeedback), length=%s:\n---\n%s\n---", label, len(content), content)
            data, last_json = _parse_score_fallback(content, label)
    except Exception as e:
        logger.warning("%s structured output failed: %s", label, e)
        raw = _llm_invoke_with_retry(
            llm,
            messages,
            job_cache_key=_state_get(state, "job_cache_key"),
            usage_query_kind=_usage_qk,
        )
        content = raw.content if hasattr(raw, "content") else str(raw)
        logger.warning("[%s] raw LLM output (before parse), length=%s:\n---\n%s\n---", label, len(content), content)
        data, last_json = _parse_score_fallback(content, label)

    out.update({
        score_key: data.score,
        "feedback": [f"{feedback_prefix}{data.feedback}"],
    })
    if last_json is not None:
        out[last_json_state_key] = _normalize_dict_keys(last_json)
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
        usage = _normalize_token_usage(None, None, dbg_prompt, str(data.feedback) if data else None)
        out["input_tokens"] = usage["input_tokens"]
        out["output_tokens"] = usage["output_tokens"]
        if usage.get("tokens_estimated"):
            out["tokens_estimated"] = True
    return out


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
    )

# --- Graph Logic ---

VALID_STEP_IDS = frozenset({"writer", "ats_judge", "recruiter_judge"})


def create_workflow_from_steps(steps: list, max_iterations: int = 3, loop_to: Optional[str] = None):
    """
    Build a compiled StateGraph from an ordered list of step ids. No recursion:
    the flow runs once from first step to last, then END.
    steps: e.g. ["recruiter_judge", "writer", "ats_judge", "writer", "recruiter_judge"]
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
