import json
import logging
import re
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ScoreFeedback(BaseModel):
    score: int = Field(ge=0, le=100, description="Score from 0 to 100")
    feedback: str = Field(description="Brief feedback text")


class FitCheckResult(BaseModel):
    """Candidate-job fit: score 0-100, reasoning, and thoughts on why/why not a fit."""

    score: int = Field(ge=0, le=100, description="Overall fit score 0-100")
    interview_probability: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="Probability (0-100) that the candidate would be called for an interview if they applied.",
    )
    reasoning: str = Field(
        description="Brief reasoning based on match, seniority, and likelihood of interview call"
    )
    thoughts: str = Field(
        description=(
            "Your assessment of why or why not the candidate is a fit: key strengths and gaps "
            "relative to the role"
        )
    )


def _normalize_key(k: str) -> str:
    """Strip whitespace and optional surrounding quotes so '\\r\\n  \"analysis\"' matches 'analysis'."""
    if not isinstance(k, str):
        return str(k)
    s = k.replace("\r", "").replace("\n", " ").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    return s


def _get_key(d: dict, *candidates: str):
    """Get value from d by exact key, or by normalized key."""
    if not isinstance(d, dict):
        return None
    for c in candidates:
        if c in d:
            return d[c]
    keys_norm = {_normalize_key(k): k for k in d}
    for c in candidates:
        if c in keys_norm:
            return d[keys_norm[c]]
    return None


def _normalize_dict_keys(obj):
    """Return a copy of obj (dict/list) with all dict keys normalized."""
    if isinstance(obj, dict):
        return {_normalize_key(k): _normalize_dict_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_dict_keys(v) for v in obj]
    return obj


def _extract_json_object(content: str) -> Optional[dict]:
    """Extract a single JSON object from LLM output."""
    logger.debug("[ATS/Recruiter] _extract_json_object input length=%s", len(content) if content else 0)
    if not content or not isinstance(content, str):
        logger.debug("[ATS/Recruiter] _extract_json_object: no content or not str")
        return None
    text = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    logger.debug(
        "[ATS/Recruiter] after normalize: len=%s, first 250 repr=%s",
        len(text),
        repr(text[:250]),
    )
    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if code_block:
        text = code_block.group(1).replace("\r\n", "\n").replace("\r", "\n").strip()
        logger.debug(
            "[ATS/Recruiter] after code block strip: len=%s",
            len(text),
        )
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            logger.debug("[ATS/Recruiter] no more '{' at pos>=%s", pos)
            break
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            keys = list(obj.keys())[:15]
                            logger.debug(
                                "[ATS/Recruiter] parsed obj at start=%s, keys=%s",
                                start,
                                keys,
                            )
                            norm_keys = {_normalize_key(k) for k in obj}
                            root_keys = {
                                "ats_match_score",
                                "score",
                                "recruiter_score",
                                "analysis",
                                "feedback",
                                "missing_keywords",
                                "actionable_feedback",
                            }
                            if norm_keys & root_keys:
                                return obj
                    except json.JSONDecodeError as je:
                        logger.debug(
                            "[ATS/Recruiter] JSONDecodeError at start=%s: %s",
                            start,
                            je,
                        )
                        break
        pos = start + 1
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        logger.debug(
                            "[ATS/Recruiter] fallback parsed obj, keys=%s",
                            list(obj.keys())[:15] if isinstance(obj, dict) else type(obj),
                        )
                        return obj
                    except json.JSONDecodeError as je:
                        logger.debug(
                            "[ATS/Recruiter] fallback JSONDecodeError: %s",
                            je,
                        )
                        break
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            obj = json.loads(match.group())
            logger.debug(
                "[ATS/Recruiter] regex fallback parsed, keys=%s",
                list(obj.keys())[:15] if isinstance(obj, dict) else type(obj),
            )
            return obj
        except json.JSONDecodeError as je:
            logger.debug("[ATS/Recruiter] regex fallback JSONDecodeError: %s", je)
    logger.warning("[ATS/Recruiter] _extract_json_object: no valid object found")
    return None


# Match score lines like "Resume Match Score (0-100): 78" or markdown
# "**Resume Match Score (0-100):** **78**" (models often bold the label and number).
# Value may be markdown-wrapped multiple times, e.g. "(0-100):** **78**"
_PLAIN_SCORE_LINE_RE = re.compile(
    r"(?:\*+\s*)?"
    r"(?:Resume\s+Match\s+Score|ATS\s+Match\s+Score|Recruiter\s+Score|Match\s+Score|Fit\s+Score|Score)"
    r"\s*(?:\([^)]*\))?"
    r"\s*(?:\*+\s*)?"
    r":\s*(?:\*+\s*)*(\d{1,3})\b",
    re.IGNORECASE,
)


def _extract_plain_text_score(content: str) -> Optional[int]:
    """Best-effort 0–100 score from prose/markdown when JSON is absent."""
    if not content or not isinstance(content, str):
        return None
    m = _PLAIN_SCORE_LINE_RE.search(content)
    if m:
        try:
            return max(0, min(100, int(m.group(1))))
        except (ValueError, TypeError):
            pass
    m2 = re.search(r"\bscore\s*[=:]\s*(?:\*+\s*)*(\d{1,3})\b", content, re.IGNORECASE)
    if m2:
        try:
            return max(0, min(100, int(m2.group(1))))
        except (ValueError, TypeError):
            pass
    return None


def parse_score_fallback(content: str, node_name: str) -> tuple[ScoreFeedback, Optional[dict]]:
    """Fallback when structured output is not available or fails."""
    logger.debug("[%s] parse_score_fallback content len=%s", node_name, len(content) if content else 0)
    data = _extract_json_object(content)
    if data is not None:
        logger.warning("[%s] parse_score_fallback extracted data keys=%s", node_name, list(data.keys()))
        try:
            raw_score = _get_key(data, "ats_match_score", "score", "recruiter_score")
            score = int(raw_score) if raw_score is not None else 70
            score = max(0, min(100, score))
            feedback_sections = []
            feedback_val = _get_key(data, "feedback")
            if feedback_val:
                feedback_sections.append(f"Summary: {feedback_val}")
            analysis = _get_key(data, "analysis")
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
                feedback_sections.append(
                    "Missing keywords: " + ", ".join(str(k) for k in missing)
                )
            elif missing and not isinstance(missing, (dict, list)):
                feedback_sections.append(f"Missing keywords: {missing}")
            action = _get_key(data, "actionable_feedback")
            if isinstance(action, list) and action:
                feedback_sections.append(
                    "Actionable feedback:\n- " + "\n- ".join(str(a) for a in action)
                )
            elif action and not isinstance(action, (dict, list)):
                feedback_sections.append(f"Actionable feedback: {action}")
            feedback = "\n".join(feedback_sections) if feedback_sections else "No feedback parsed."
            return ScoreFeedback(score=score, feedback=feedback), data
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("[%s] could not use parsed JSON: %s", node_name, e)
            logger.debug(
                "[%s] parsed data repr (first 500 chars)=%s",
                node_name,
                repr(str(data)[:500]),
            )
    if content and isinstance(content, str):
        score = _extract_plain_text_score(content)
        if score is not None:
            feedback = content.strip()[:2000] + ("..." if len(content) > 2000 else "")
            logger.warning(
                "[%s] parse_score_fallback extracted score from plain text: %s",
                node_name,
                score,
            )
            return ScoreFeedback(score=score, feedback=feedback), None
    logger.warning("[%s] parse_score_fallback returning default (no data or exception)", node_name)
    return ScoreFeedback(score=70, feedback="Could not parse score. Defaulting."), None


def parse_fit_check_fallback(content: str) -> FitCheckResult:
    text = (content or "").strip()
    if not text:
        return FitCheckResult(
            score=50,
            interview_probability=None,
            reasoning="Could not parse fit check. Defaulting.",
            thoughts="",
        )

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
        raw_ip = _get_key(
            data,
            "interview_probability",
            "interviewProbability",
            "interview_likelihood",
            "interviewLikelihood",
            "interview_prob",
            "interviewProb",
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
            ip = None
            if raw_ip is not None:
                try:
                    if isinstance(raw_ip, str):
                        # Handle common formats like "72%", "Interview: 72/100", etc.
                        m = re.search(r"(\d{1,3})", raw_ip)
                        if m:
                            ip_val = int(m.group(1))
                            ip = max(0, min(100, ip_val))
                    else:
                        ip_val = int(raw_ip)
                        ip = max(0, min(100, ip_val))
                except (TypeError, ValueError):
                    ip = None
            # Fallback: sometimes the model includes interview probability in the
            # reasoning string but uses a non-matching key name in JSON.
            if ip is None:
                ip_text_match = re.search(
                    r"interview[^0-9]{0,30}(\d{1,3})\s*%?",
                    text,
                    re.IGNORECASE,
                )
                if ip_text_match:
                    try:
                        ip_val = int(ip_text_match.group(1))
                        ip = max(0, min(100, ip_val))
                    except (TypeError, ValueError):
                        ip = None
            return FitCheckResult(
                score=score,
                interview_probability=ip,
                reasoning=str(reasoning or thoughts or "Parsed from JSON fallback."),
                thoughts=str(thoughts or reasoning or ""),
            )
        except (TypeError, ValueError):
            pass

    score_val = _extract_plain_text_score(text)

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

    if score_val is not None:
        interview_probability = None
        ip_match = re.search(
            r"(?:Interview\s+(?:probability|likelihood))\s*[:=-]?\s*(\d{1,3})\s*(?:%|/100)?",
            text,
            re.IGNORECASE,
        )
        if ip_match:
            try:
                interview_probability = max(0, min(100, int(ip_match.group(1))))
            except (TypeError, ValueError):
                interview_probability = None
        return FitCheckResult(
            score=score_val,
            interview_probability=interview_probability,
            reasoning=reasoning or "Parsed score from plain-text response.",
            thoughts=thoughts or reasoning or "",
        )

    return FitCheckResult(
        score=50,
        interview_probability=None,
        reasoning=(reasoning or text[:500] or "Could not parse fit check. Defaulting."),
        thoughts=thoughts or "",
    )


def normalize_dict_keys(obj):
    return _normalize_dict_keys(obj)

