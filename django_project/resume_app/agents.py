from typing import TypedDict, List, Annotated, Optional
import operator
import logging
import time
import re

from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END

from .callbacks import TokenUsageCallback
from .llm_factory import get_llm
from .parsers import (
    ScoreFeedback,
    FitCheckResult,
    parse_score_fallback as _parse_score_fallback,
    parse_fit_check_fallback as _parse_fit_check_fallback,
    normalize_dict_keys as _normalize_dict_keys,
)
from .prompts import (
    DEFAULT_WRITER_PROMPT,
    DEFAULT_ATS_JUDGE_PROMPT,
    DEFAULT_RECRUITER_JUDGE_PROMPT,
    DEFAULT_FIT_CHECK_PROMPT,
    DEFAULT_MATCHING_PROMPT,
)

logger = logging.getLogger(__name__)

# Chars-per-token heuristic when provider does not report usage (e.g. some Ollama setups)
_CHARS_PER_TOKEN_ESTIMATE = 4


def _normalize_token_usage(response, llm_output=None, prompt_text=None, response_content=None):
    """Extract (input_tokens, output_tokens, estimated) from LLM response. Uses provider metadata first, then estimates from text length."""
    in_tok, out_tok, estimated = 0, 0, False
    usage = None
    if response is not None:
        usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        in_tok = int(usage.get("input_tokens") or usage.get("input") or usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or usage.get("output") or usage.get("completion_tokens") or 0)
    if in_tok == 0 and out_tok == 0 and llm_output:
        tu = (llm_output or {}).get("token_usage") or (llm_output or {}).get("usage") or {}
        if isinstance(tu, dict):
            in_tok = int(tu.get("input_tokens") or tu.get("prompt_tokens") or 0)
            out_tok = int(tu.get("output_tokens") or tu.get("completion_tokens") or 0)
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
    return {"input_tokens": in_tok, "output_tokens": out_tok, "tokens_estimated": estimated}


def _format_prompt(template: str, **kwargs) -> str:
    """Format template with kwargs, but treat only known keys as placeholders. Any other { } in the
    template (e.g. JSON examples) are left literal so str.format() does not raise KeyError."""
    known = set(kwargs.keys())
    # Escape all braces, then restore only our placeholders so they get substituted
    escaped = template.replace("{", "{{").replace("}", "}}")
    for name in known:
        escaped = escaped.replace("{{" + name + "}}", "{" + name + "}")
    return escaped.format(**kwargs)


def _extract_429_retry_seconds(exc: Exception):
    """Parse 'Please retry in X seconds' or retry_delay from 429/ResourceExhausted errors. Returns seconds or None."""
    msg = str(exc)
    # "Please retry in 58.659962366s." or similar
    m = re.search(r"retry\s+in\s+([\d.]+)\s*s", msg, re.I)
    if m:
        try:
            return max(1, min(300, int(float(m.group(1)))))  # clamp 1–300s
        except (ValueError, TypeError):
            pass
    # retry_delay { seconds: 58 }
    m = re.search(r"retry_delay[\s\S]*?seconds[\"']?\s*:\s*(\d+)", msg, re.I)
    if m:
        try:
            return max(1, min(300, int(m.group(1))))
        except (ValueError, TypeError):
            pass
    return None


def _llm_invoke_with_retry(llm, messages, max_attempts=3, config=None):
    last_exc = None
    for attempt in range(max_attempts):
        try:
            if config is not None:
                return llm.invoke(messages, config=config)
            return llm.invoke(messages)
        except Exception as e:
            last_exc = e
            is_429 = "429" in str(e) or "ResourceExhausted" in type(e).__name__ or "quota" in str(e).lower()
            if is_429 and attempt < max_attempts - 1:
                delay = _extract_429_retry_seconds(e) or 60
                logger.warning("429/quota error, waiting %s seconds before retry (attempt %s): %s", delay, attempt + 1, e)
                time.sleep(delay)
            else:
                raise
    raise last_exc


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


def _parse_score_fallback(content: str, node_name: str) -> tuple["ScoreFeedback", Optional[dict]]:
    """Fallback when structured output is not available or fails. Supports both simple
    {score, feedback} and extended ATS schemas (e.g. ats_match_score, analysis, missing_keywords, actionable_feedback).
    Builds a structured feedback string for the next step. Returns (ScoreFeedback, parsed_dict or None)."""
    logger.debug("[%s] _parse_score_fallback content len=%s", node_name, len(content) if content else 0)
    data = _extract_json_object(content)
    if data is not None:
        logger.warning("[%s] _parse_score_fallback extracted data keys=%s", node_name, list(data.keys()))
        try:
            # Score: support ats_match_score (custom ATS prompts) or score (use key-normalized lookup)
            raw_score = _get_key(data, "ats_match_score", "score")
            score = int(raw_score) if raw_score is not None else 70
            score = max(0, min(100, score))
            # Build structured feedback for the next step (clearly labeled so writer can use it)
            feedback_sections = []
            feedback_val = _get_key(data, "feedback")
            if feedback_val:
                feedback_sections.append(f"Summary: {feedback_val}")
            analysis = _get_key(data, "analysis")
            # If parser returned only the inner "analysis" object (no root), treat data as analysis
            if not isinstance(analysis, dict) and (
                _get_key(data, "dealbreakers_rationale") is not None
                or _get_key(data, "contextual_evidence_quality") is not None
                or _get_key(data, "dealbreakers_met") is not None
            ):
                analysis = data
            if isinstance(analysis, dict):
                dr = _get_key(analysis, "dealbreakers_rationale")
                ce = _get_key(analysis, "contextual_evidence_quality")
                if dr:
                    feedback_sections.append(f"Dealbreakers: {dr}")
                if ce:
                    feedback_sections.append(f"Contextual evidence: {ce}")
            missing = _get_key(data, "missing_keywords")
            if isinstance(missing, list) and missing:
                feedback_sections.append("Missing keywords: " + ", ".join(str(k) for k in missing))
            elif missing and not isinstance(missing, (dict, list)):
                feedback_sections.append(f"Missing keywords: {missing}")
            action = _get_key(data, "actionable_feedback")
            if isinstance(action, list) and action:
                feedback_sections.append("Actionable feedback:\n- " + "\n- ".join(str(a) for a in action))
            elif action and not isinstance(action, (dict, list)):
                feedback_sections.append(f"Actionable feedback: {action}")
            feedback = "\n".join(feedback_sections) if feedback_sections else "No feedback parsed."
            return ScoreFeedback(score=score, feedback=feedback), data
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("[%s] could not use parsed JSON: %s", node_name, e)
            logger.debug("[%s] parsed data repr (first 500 chars)=%s", node_name, repr(str(data)[:500]))
    # No JSON: try to extract score from plain text (e.g. "ATS Match Score: 65", "Resume Match Score (0-100): 88")
    if content and isinstance(content, str):
        score_match = re.search(
            r"(?:Resume\s+Match\s+Score|ATS\s+Match\s+Score|Recruiter\s+Score|Match\s+Score|Score)"
            r"\s*(?:\([^)]*\))?\s*:\s*(\d{1,3})",
            content,
            re.IGNORECASE,
        )
        if not score_match:
            score_match = re.search(r"\bscore\s*[=:]\s*(\d{1,3})\b", content, re.IGNORECASE)
        if score_match:
            try:
                score = max(0, min(100, int(score_match.group(1))))
                feedback = content.strip()[:2000] + ("..." if len(content) > 2000 else "")
                logger.warning("[%s] _parse_score_fallback extracted score from plain text: %s", node_name, score)
                return ScoreFeedback(score=score, feedback=feedback), None
            except (ValueError, TypeError):
                pass
    logger.warning("[%s] _parse_score_fallback returning default (no data or exception)", node_name)
    return ScoreFeedback(score=70, feedback="Could not parse score. Defaulting."), None


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


def run_fit_check(resume_text: str, job_description: str, llm, prompt_template: str = None) -> dict:
    """Returns { score: int, reasoning: str, thoughts: str }. Score 0-100; if < 50 caller may ask user to confirm."""
    template = prompt_template or DEFAULT_FIT_CHECK_PROMPT
    prompt = _format_prompt(template, resume_text=resume_text, job_description=job_description)
    try:
        structured_llm = llm.with_structured_output(FitCheckResult)
        result = _llm_invoke_with_retry(structured_llm, [HumanMessage(content=prompt)])
        if isinstance(result, FitCheckResult):
            return {"score": result.score, "reasoning": result.reasoning, "thoughts": result.thoughts}
        parsed = _parse_fit_check_fallback(str(result))
        return {"score": parsed.score, "reasoning": parsed.reasoning, "thoughts": parsed.thoughts}
    except Exception as e:
        logger.warning("fit_check structured output failed: %s", e)
        raw = _llm_invoke_with_retry(llm, [HumanMessage(content=prompt)])
        content = raw.content if hasattr(raw, "content") else str(raw)
        parsed = _parse_fit_check_fallback(content)
        return {"score": parsed.score, "reasoning": parsed.reasoning, "thoughts": parsed.thoughts}
DEFAULT_WRITER_PROMPT = """You are an expert Resume Writer. Your task is to tailor the following resume to the job description provided.
Ensure you highlight relevant skills and experiences without hallucinating any information.
Format your output using simple markdown so it can be exported to Word and PDF with proper formatting:
- Use ## for section headings (e.g. ## EXPERIENCE, ## EDUCATION).
- Use **bold** for emphasis on key terms or job titles.
- Use a single - or * at the start of a line for bullet points.
- Use blank lines between paragraphs and sections.

Resume:
{resume_text}

Job Description:
{job_description}

Previous Feedback:
{feedback}

Optimized Resume:
"""

DEFAULT_ATS_JUDGE_PROMPT = """You are an ATS (Applicant Tracking System) Judge. Score the following tailored resume against the job description.
Focus on keywords and parseability. Return a score 0-100 and brief feedback.

Tailored Resume:
{optimized_resume}

Job Description:
{job_description}
"""

DEFAULT_RECRUITER_JUDGE_PROMPT = """You are a Senior Recruiter. Score the following tailored resume against the job description.
Focus on metrics, impact, and action verbs. Return a score 0-100 and brief feedback.

Tailored Resume:
{optimized_resume}

Job Description:
{job_description}
"""

# --- Candidate-job fit check (run before optimization) ---
DEFAULT_FIT_CHECK_PROMPT = """You are an expert recruiter. Assess whether this candidate is a reasonable fit for the job.

Consider:
1. **Match**: How well do the candidate's skills, experience, and background align with the role requirements?
2. **Seniority**: Is the candidate's level (e.g. years of experience, scope) appropriate—not overqualified to the point of rejection, not underqualified?
3. **Interview likelihood**: Based on typical hiring behavior, what is the probability (roughly 0-100%) that this candidate would be called in for an interview if they applied?

Provide:
- A single overall fit score from 0 to 100.
- Brief reasoning (2-3 sentences) covering match, seniority, and interview likelihood.
- Your thoughts on why or why not the candidate is a fit: call out key strengths that align with the role and any gaps or concerns. Be specific and constructive.

Resume:
{resume_text}

Job Description:
{job_description}
"""

# --- Matching (resume vs job → single score for search results) ---
DEFAULT_MATCHING_PROMPT = """You are an expert recruiter and ATS specialist. Analyze how well the candidate's resume matches the job description. Be objective and strict. Do not inflate scores.

Consider: hard requirements (years of experience, mandatory skills), keyword and semantic fit, evidence in experience bullets (not just skills list), and seniority alignment.

Return a single match score from 0 to 100. 90+ = strong fit, 70–89 = good but gaps, <70 = significant gaps.

Resume:
{resume_text}

Job Description:
{job_description}
"""


def run_matching(resume_text: str, job_description: str, llm, prompt_template: str = None) -> dict:
    """Returns { score: int, reasoning: str }. Score 0-100. Used after job search for independent LLM match score.
    Uses raw LLM call + parser (no structured output) so all providers return parseable text."""
    template = prompt_template or DEFAULT_MATCHING_PROMPT
    prompt = _format_prompt(template, resume_text=resume_text, job_description=job_description)
    logger.info("[matching] prompt length=%s", len(prompt))
    raw = _llm_invoke_with_retry(llm, [HumanMessage(content=prompt)])
    content = getattr(raw, "content", None)
    if content is None:
        content = str(raw) if raw is not None else ""
    if not isinstance(content, str):
        content = str(content)
    logger.info("[matching] raw response length=%s first_500=%r", len(content), (content or "")[:500])
    parsed = _parse_fit_check_fallback(content or "")
    return {"score": parsed.score, "reasoning": parsed.reasoning}


# --- State Definition ---
class _AgentStateBase(TypedDict):
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

# --- Agent Nodes ---

# Known state keys so we never trigger KeyError on stray keys (e.g. from merged JSON/session).
_STATE_KEYS = frozenset({
    "resume_text", "job_description", "optimized_resume", "ats_score", "recruiter_score",
    "feedback", "iteration_count", "llm", "writer_prompt_template", "ats_judge_prompt_template",
    "recruiter_judge_prompt_template", "debug", "max_iterations", "score_threshold",
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
    llm = _state_get(state, "llm")
    template = _state_get(state, "writer_prompt_template") or DEFAULT_WRITER_PROMPT
    prompt = _format_prompt(
        template,
        resume_text=_state_get(state, "resume_text") or "",
        job_description=_state_get(state, "job_description") or "",
        feedback=", ".join(_state_get(state, "feedback") or []),
    )
    logger.warning("[writer] prompts sent to LLM: no system prompt; single user message (length=%s):\n--- USER PROMPT ---\n%s\n--- END USER PROMPT ---", len(prompt), prompt)
    if _state_get(state, "debug"):
        out = {"debug_prompt": prompt, "debug_messages": [{"role": "user", "content": prompt}]}
    else:
        out = {}
    response = _llm_invoke_with_retry(llm, [HumanMessage(content=prompt)])
    out.update({
        "optimized_resume": response.content,
        "iteration_count": (_state_get(state, "iteration_count") or 0) + 1
    })
    usage = _normalize_token_usage(response, None, prompt)
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
    default_template: str,
    score_key: str,
    feedback_prefix: str,
    last_json_state_key: str,
):
    llm = _state_get(state, "llm")
    template = _state_get(state, template_state_key) or default_template
    draft = (_state_get(state, "optimized_resume") or "").strip() or (_state_get(state, "resume_text") or "").strip()
    prompt = _format_prompt(
        template,
        optimized_resume=draft,
        job_description=_state_get(state, "job_description") or "",
    )
    logger.warning("[%s] prompts sent to LLM: no system prompt; single user message (length=%s):\n--- USER PROMPT ---\n%s\n--- END USER PROMPT ---", label, len(prompt), prompt)
    if _state_get(state, "debug"):
        out = {"debug_prompt": prompt, "debug_messages": [{"role": "user", "content": prompt}]}
    else:
        out = {}
    last_json = None
    raw = None
    usage_callback = TokenUsageCallback()
    try:
        structured_llm = llm.with_structured_output(ScoreFeedback)
        result = _llm_invoke_with_retry(
            structured_llm, [HumanMessage(content=prompt)], config={"callbacks": [usage_callback]}
        )
        if isinstance(result, ScoreFeedback):
            data = result
        else:
            content = str(result)
            logger.warning("[%s] raw LLM output (structured returned non-ScoreFeedback), length=%s:\n---\n%s\n---", label, len(content), content)
            data, last_json = _parse_score_fallback(content, label)
    except Exception as e:
        logger.warning("%s structured output failed: %s", label, e)
        raw = _llm_invoke_with_retry(llm, [HumanMessage(content=prompt)])
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
        usage = _normalize_token_usage(raw, getattr(raw, "llm_output", None), prompt)
        out["input_tokens"] = usage["input_tokens"]
        out["output_tokens"] = usage["output_tokens"]
        if usage.get("tokens_estimated"):
            out["tokens_estimated"] = True
    elif usage_callback.total_input_tokens or usage_callback.total_output_tokens:
        out["input_tokens"] = usage_callback.total_input_tokens
        out["output_tokens"] = usage_callback.total_output_tokens
    else:
        usage = _normalize_token_usage(None, None, prompt, str(data.feedback) if data else None)
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
        default_template=DEFAULT_ATS_JUDGE_PROMPT,
        score_key="ats_score",
        feedback_prefix="ATS: ",
        last_json_state_key="last_ats_json",
    )


def recruiter_judge_node(state: AgentState):
    return _judge_node(
        state,
        label="recruiter_judge",
        template_state_key="recruiter_judge_prompt_template",
        default_template=DEFAULT_RECRUITER_JUDGE_PROMPT,
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
