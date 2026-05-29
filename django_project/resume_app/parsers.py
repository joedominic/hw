import json
import logging
import re
from typing import Any, Callable, Optional, Tuple, Type, Union

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ScoreFeedback(BaseModel):
    score: int = Field(ge=0, le=100, description="Score from 0 to 100")
    feedback: str = Field(description="Brief feedback text")


class AtsJudgeResult(BaseModel):
    """Structured ATS judge output (keywords, formatting, strategic advice)."""

    ats_match_score: int = Field(ge=0, le=100, description="ATS match score 0-100")
    missing_keywords: list[str] = Field(
        default_factory=list,
        description="Job-relevant keywords missing or weak in the resume",
    )
    formatting_issues: list[str] = Field(
        default_factory=list,
        description="ATS parseability / formatting problems",
    )
    strategic_feedback: str = Field(
        default="",
        description="Actionable advice for improving match and parseability",
    )

    def feedback_text(self) -> str:
        """Human-readable feedback for optimizer logs and writer loop."""
        sections: list[str] = []
        if self.strategic_feedback.strip():
            sections.append(f"Strategic feedback: {self.strategic_feedback.strip()}")
        if self.missing_keywords:
            sections.append("Missing keywords: " + ", ".join(self.missing_keywords))
        if self.formatting_issues:
            sections.append(
                "Formatting issues:\n- " + "\n- ".join(self.formatting_issues)
            )
        return "\n".join(sections) if sections else "No ATS feedback provided."


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


# Pipeline resume-summary LLM output (must not be filtered out by ATS-only heuristics).
_PIPELINE_RESUME_SUMMARY_KEYS = frozenset(
    {
        "hard_skills",
        "methodologies",
        "soft_skills",
        "business_outcomes",
        "domain_scale",
        "action_verbs",
    }
)


def _try_json_dict_lenient(text: str) -> Optional[dict]:
    """Best-effort json.loads for sloppy model output (e.g. trailing commas)."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip()
    candidates = [t, re.sub(r",(\s*[}\]])", r"\1", t)]
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _extract_json_object(
    content: str, *, accept_pipeline_skill_keys: bool = False
) -> Optional[dict]:
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
                                "ats_score",
                                "score",
                                "recruiter_score",
                                "analysis",
                                "feedback",
                                "missing_keywords",
                                "formatting_issues",
                                "strategic_feedback",
                                "actionable_feedback",
                            }
                            if norm_keys & root_keys:
                                return obj
                            if accept_pipeline_skill_keys and (
                                norm_keys & _PIPELINE_RESUME_SUMMARY_KEYS
                            ):
                                return obj
                            break
                        break
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
# "**Resume Match Score (0-100):** **78**" / "**Overall Fit Score:** **78 / 100**"
# (models often bold the label and number). Value may be wrapped in ** and "/ 100".
_PLAIN_SCORE_LINE_RE = re.compile(
    r"(?:\*+\s*)?"
    r"(?:Resume\s+Match\s+Score|ATS\s+Match\s+Score|Recruiter\s+Score|Match\s+Score|Overall\s+Fit\s+Score|Fit\s+Score|Score)"
    r"\s*(?:\([^)]*\))?"
    r"\s*(?:\*+\s*)?"
    r":\s*(?:\*+\s*)*(\d{1,3})(?:\s*/\s*100)?(?:\s*\*+)?\b",
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


# Markdown reports often use "**Header**" on its own line; score may appear inline on the first line.
_MARKDOWN_SECTION_SPLIT_RE = re.compile(r"(?m)^\s*\*\*([^*\n]+)\*\*\s*$")

# Reasoning / assessment headers with optional "(...)" — colon optional (LLMs vary).
_REASONING_HEADER_RE = re.compile(
    r"(?is)"
    r"(?:\*\*\s*)?"
    r"(?:Reasoning|Overall\s+Strategic\s+Assessment|Summary|Assessment)"
    r"\s*(?:\([^)]*\))?\s*"
    r"(?:\*\*\s*)?"
    r":?\s*\n+\s*"
    r"(.*?)"
    r"(?=\n\s*(?:\*{1,2}\s*)?(?:Fit\s+Assessment|Overall\s+Verdict|Interview|Strengths|Gaps)\b|\Z)",
)

_REASONING_COLON_RE = re.compile(
    r"(?is)(?:Reasoning|Overall\s+Strategic\s+Assessment|Summary|Assessment)\s*(?:\([^)]*\))?\s*:\s*(.+?)"
    r"(?=\n\s*(?:\*{1,2}\s*)?(?:Fit\s+Assessment|Overall\s+Verdict|Interview|Strengths|Gaps)\b|\n[A-Z][^\n]{0,80}:\s|\Z)",
)

_THOUGHTS_HEADER_RE = re.compile(
    r"(?is)(?:Thoughts|Feedback|Why|Analysis|Fit\s+Assessment)\s*(?:\([^)]*\))?\s*:\s*(.+?)"
    r"(?=\n\s*(?:\*{1,2}\s*)?(?:Overall\s+Verdict|Interview|Verdict)\b|\Z)",
)

_INTERVIEW_PLAIN_RES = (
    re.compile(
        r"(?:Interview\s+(?:probability|likelihood)|Likelihood\s+of\s+(?:an\s+)?interview)\s*[:=-]?\s*(\d{1,3})\s*(?:%|/100)?",
        re.I,
    ),
    re.compile(r"(?:Interview\s+(?:probability|likelihood))\s+is\s+(?:about\s+)?(\d{1,3})\s*%", re.I),
    re.compile(
        r"Interview\s+probability\s*:\s*(?:\*+\s*)*(\d{1,3})\s*(?:%|/100)?",
        re.I,
    ),
    re.compile(r"(?:^|\n)\s*(\d{1,3})\s*%\s*(?:chance|likelihood)\s+of\s+(?:an\s+)?interview", re.I | re.M),
)


def _line_looks_like_score_line(line: str) -> bool:
    if _PLAIN_SCORE_LINE_RE.search(line):
        return True
    s = line.strip().lower()
    if "overall fit score" in s and re.search(r"\d{1,3}", s):
        return True
    if re.search(r"\bfit\s+score\b", s) and re.search(r"\d{1,3}\s*/\s*100", s):
        return True
    return False


def _strip_leading_score_lines(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        if _line_looks_like_score_line(raw):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


def _markdown_sections_dict(text: str) -> dict[str, str]:
    """Split on '**Title**' lines (title alone on line). Values are body until next header."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return {}
    parts = _MARKDOWN_SECTION_SPLIT_RE.split(text)
    out: dict[str, str] = {}
    if parts and parts[0].strip():
        out["_preamble"] = parts[0].strip()
    rest = parts[1:]
    for j in range(0, len(rest) - 1, 2):
        title_key = rest[j].strip()
        body = rest[j + 1].strip()
        out[title_key.lower()] = body
    return out


def _build_narrative_from_markdown_sections(sections: dict[str, str]) -> str:
    """Order: reasoning, fit/strengths table, verdict; skip score-only headers."""
    blocks: list[tuple[int, str]] = []
    for title_l, body in sections.items():
        if title_l.startswith("_"):
            continue
        if "overall fit score" in title_l:
            continue
        if not body:
            continue
        b = body.strip()
        if len(b) < 120 and _line_looks_like_score_line(b) and "\n" not in b:
            continue
        display_title = " ".join(w.capitalize() for w in title_l.split())
        if title_l.startswith("reasoning"):
            blocks.append((0, f"**Reasoning**\n{b}"))
            continue
        if "fit assessment" in title_l or "strengths" in title_l or "gaps" in title_l:
            blocks.append((1, f"**{display_title}**\n{b}"))
            continue
        if "verdict" in title_l:
            blocks.append((2, f"**{display_title}**\n{b}"))
            continue
        blocks.append((3, f"**{display_title}**\n{b}"))
    if not blocks:
        return ""
    blocks.sort(key=lambda x: x[0])
    return "\n\n".join(x[1] for x in blocks).strip()


def _extract_interview_probability_plain(text: str) -> Optional[int]:
    for cre in _INTERVIEW_PLAIN_RES:
        m = cre.search(text)
        if m:
            try:
                return max(0, min(100, int(m.group(1))))
            except (TypeError, ValueError):
                continue
    return None


def _coerce_interview_probability_if_distinct_from_fit(
    text: str,
    interview_probability: Optional[int],
    score: int,
) -> Optional[int]:
    """
    Fit score and interview likelihood are different concepts. Models often echo the same number
    for both or weak regexes pick up the fit score. When they are equal, keep interview % only if
    the reply clearly discusses interview odds in prose (not just a bare duplicate field).
    """
    if interview_probability is None:
        return None
    if interview_probability != score:
        return interview_probability
    t = (text or "").lower()
    markers = (
        "interview probability",
        "interview likelihood",
        "probability of interview",
        "probability of an interview",
        "likelihood of interview",
        "likelihood of an interview",
        "interview odds",
        "chance of an interview",
        "chance of interview",
        "chance they would be interviewed",
        "likely to be interviewed",
        "likely to receive an interview",
        "would be invited to interview",
        "screening interview",
        "recruiter screen",
    )
    if any(m in t for m in markers):
        return interview_probability
    return None


# Keys that indicate a parsed judge payload (vs. a wrapper object from state/UI).
_JUDGE_PAYLOAD_KEYS = frozenset(
    {
        "ats_match_score",
        "ats_score",
        "score",
        "recruiter_score",
        "missing_keywords",
        "formatting_issues",
        "strategic_feedback",
        "feedback",
        "actionable_feedback",
        "analysis",
    }
)

_JUDGE_JSON_WRAPPER_KEYS = (
    "last_ats_json",
    "last_recruiter_json",
    "ats_judge",
    "recruiter_judge",
    "result",
    "response",
    "data",
    "output",
)


def _unwrap_judge_json_dict(data: dict) -> dict:
    """Unwrap nested judge JSON when the model echoes state keys like last_ats_json."""
    if not isinstance(data, dict):
        return data
    norm_keys = {_normalize_key(k) for k in data}
    if norm_keys & _JUDGE_PAYLOAD_KEYS:
        return data
    for wrapper in _JUDGE_JSON_WRAPPER_KEYS:
        inner = _get_key(data, wrapper)
        if isinstance(inner, dict):
            inner_norm = {_normalize_key(k) for k in inner}
            if inner_norm & _JUDGE_PAYLOAD_KEYS:
                return inner
    return data


def _coerce_str_list(value) -> list[str]:
    """Normalize LLM list fields that may arrive as a list, string, or absent."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _extract_partial_ats_score_from_text(content: str) -> Optional[int]:
    """Best-effort score when JSON is truncated or malformed."""
    if not content:
        return None
    for pattern in (
        r'"(?:ats_match_score|ats_score)"\s*:\s*(\d{1,3})',
        r"'(?:ats_match_score|ats_score)'\s*:\s*(\d{1,3})",
    ):
        m = re.search(pattern, content)
        if m:
            try:
                return max(0, min(100, int(m.group(1))))
            except (TypeError, ValueError):
                continue
    return None


def coerce_structured_judge_result(
    result: Any,
    structured_schema: Type[BaseModel],
    parse_fallback: Callable[[str, str], Tuple[BaseModel, Optional[dict]]],
    label: str,
) -> Tuple[Union[ScoreFeedback, AtsJudgeResult], Optional[dict]]:
    """
    Normalize LangChain structured-output return types (Pydantic model, dict, or message)
    into a validated judge result, falling back to text/JSON parsing when needed.
    """
    if isinstance(result, structured_schema):
        return result, None

    if isinstance(result, BaseModel):
        try:
            dumped = result.model_dump()
            validated = structured_schema.model_validate(dumped)
            return validated, dumped
        except Exception as exc:
            logger.debug("[%s] BaseModel coercion failed: %s", label, exc)

    if isinstance(result, dict):
        normalized = _normalize_dict_keys(result)
        unwrapped = _unwrap_judge_json_dict(normalized)
        try:
            validated = structured_schema.model_validate(unwrapped)
            return validated, unwrapped
        except Exception as exc:
            logger.warning(
                "[%s] structured dict failed validation (keys=%s): %s",
                label,
                list(unwrapped.keys())[:12],
                exc,
            )
        try:
            return parse_fallback(json.dumps(unwrapped, ensure_ascii=False), label)
        except Exception as exc:
            logger.debug("[%s] json.dumps fallback failed: %s", label, exc)

    if hasattr(result, "content"):
        content = getattr(result, "content", None)
        if content is not None:
            if isinstance(content, structured_schema):
                return content, None
            if isinstance(content, dict):
                return coerce_structured_judge_result(
                    content, structured_schema, parse_fallback, label
                )
            text = content if isinstance(content, str) else str(content)
            if text.strip():
                return parse_fallback(text, label)

    text = "" if result is None else str(result)
    return parse_fallback(text, label)


def parse_ats_judge_fallback(
    content: str, node_name: str = "ats_judge"
) -> tuple[AtsJudgeResult, Optional[dict]]:
    """Fallback when ATS structured output is not available or fails."""
    logger.debug("[%s] parse_ats_judge_fallback content len=%s", node_name, len(content) if content else 0)
    data = _extract_json_object(content)
    if isinstance(data, dict):
        data = _unwrap_judge_json_dict(_normalize_dict_keys(data))
    if data is not None:
        logger.warning(
            "[%s] parse_ats_judge_fallback extracted data keys=%s",
            node_name,
            list(data.keys()),
        )
        try:
            raw_score = _get_key(
                data, "ats_match_score", "ats_score", "score", "recruiter_score"
            )
            score = int(raw_score) if raw_score is not None else 70
            score = max(0, min(100, score))
            missing = _coerce_str_list(_get_key(data, "missing_keywords"))
            formatting = _coerce_str_list(_get_key(data, "formatting_issues"))
            strategic = _get_key(
                data, "strategic_feedback", "feedback", "actionable_feedback"
            )
            strategic_s = str(strategic).strip() if strategic is not None else ""
            if not strategic_s:
                legacy_fb = _get_key(data, "analysis")
                if isinstance(legacy_fb, str):
                    strategic_s = legacy_fb.strip()
            result = AtsJudgeResult(
                ats_match_score=score,
                missing_keywords=missing,
                formatting_issues=formatting,
                strategic_feedback=strategic_s,
            )
            return result, data
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("[%s] could not use parsed ATS JSON: %s", node_name, e)
    if content and isinstance(content, str):
        score = _extract_plain_text_score(content)
        if score is None:
            score = _extract_partial_ats_score_from_text(content)
        if score is not None:
            feedback = content.strip()[:2000] + ("..." if len(content) > 2000 else "")
            logger.warning(
                "[%s] parse_ats_judge_fallback extracted score from plain text: %s",
                node_name,
                score,
            )
            return (
                AtsJudgeResult(
                    ats_match_score=score,
                    strategic_feedback=feedback,
                ),
                None,
            )
        stripped = content.strip()
        if len(stripped) > 80:
            logger.warning(
                "[%s] parse_ats_judge_fallback using prose-only fallback (len=%s)",
                node_name,
                len(stripped),
            )
            return (
                AtsJudgeResult(
                    ats_match_score=70,
                    strategic_feedback=stripped[:4000],
                ),
                None,
            )
    logger.warning(
        "[%s] parse_ats_judge_fallback returning default (no data or exception)",
        node_name,
    )
    return (
        AtsJudgeResult(
            ats_match_score=70,
            strategic_feedback="Could not parse ATS judge output. Defaulting.",
        ),
        None,
    )


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
                ip = _extract_interview_probability_plain(text)
            ip = _coerce_interview_probability_if_distinct_from_fit(text, ip, score)
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
    stripped = _strip_leading_score_lines(text)
    md_sections = _markdown_sections_dict(stripped)
    narrative = _build_narrative_from_markdown_sections(md_sections)
    if narrative:
        reasoning = narrative

    if not reasoning:
        mh = _REASONING_HEADER_RE.search(stripped) or _REASONING_HEADER_RE.search(text)
        if mh:
            reasoning = mh.group(1).strip()
    if not reasoning:
        mc = _REASONING_COLON_RE.search(stripped) or _REASONING_COLON_RE.search(text)
        if mc:
            reasoning = mc.group(1).strip()

    if not thoughts:
        th = _THOUGHTS_HEADER_RE.search(stripped) or _THOUGHTS_HEADER_RE.search(text)
        if th:
            thoughts = th.group(1).strip()

    if not reasoning:
        reasoning_match = re.search(
            r"(?:Reasoning|Overall Strategic Assessment|Summary|Assessment)\s*:\s*(.+?)(?:\n[A-Z][^\n]{0,60}:|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if reasoning_match:
            reasoning = reasoning_match.group(1).strip()

    if not thoughts:
        thoughts_match = re.search(
            r"(?:Thoughts|Feedback|Why|Analysis)\s*:\s*(.+?)(?:\n[A-Z][^\n]{0,60}:|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if thoughts_match:
            thoughts = thoughts_match.group(1).strip()

    _REASONING_JOIN_MAX = 12000
    _THOUGHTS_JOIN_MAX = 8000
    if not reasoning:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        filtered = [ln for ln in lines if not _line_looks_like_score_line(ln)]
        if filtered:
            reasoning = "\n".join(filtered)[:_REASONING_JOIN_MAX]
            if len(filtered) > 1 and not thoughts:
                thoughts = "\n".join(filtered[1:])[:_THOUGHTS_JOIN_MAX]

    if score_val is not None:
        interview_probability = _extract_interview_probability_plain(text)
        if interview_probability is None:
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
        interview_probability = _coerce_interview_probability_if_distinct_from_fit(
            text, interview_probability, score_val
        )
        return FitCheckResult(
            score=score_val,
            interview_probability=interview_probability,
            reasoning=reasoning or "Parsed score from plain-text response.",
            thoughts=thoughts or reasoning or "",
        )

    _ip = _extract_interview_probability_plain(text)
    _ip = _coerce_interview_probability_if_distinct_from_fit(text, _ip, 50)
    return FitCheckResult(
        score=50,
        interview_probability=_ip,
        reasoning=(reasoning or text[:_REASONING_JOIN_MAX] or "Could not parse fit check. Defaulting."),
        thoughts=thoughts or "",
    )


def normalize_dict_keys(obj):
    return _normalize_dict_keys(obj)

