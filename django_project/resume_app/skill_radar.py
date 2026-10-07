import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from django.core.cache import cache
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 7 * 86400  # 7 days

VAGUE_KEYWORDS_BLACKLIST = {
    "communication", "communication skills", "written and verbal", "team player",
    "problem solving", "experience with", "fast paced", "responsibilities include",
    "track record", "self starter", "best practices", "strong work ethic",
    "cross functional", "high level", "equal opportunity", "attention to detail",
    "bachelor degree", "years of experience", "ability to", "working knowledge",
    "skills include", "hands on", "team members", "business requirements",
    "critical thinking", "passionate about", "day to day",
}


def clean_skill_string(s: str) -> str:
    """Normalize skill name: trim, remove leading bullets/numbers."""
    s = re.sub(r"^[\d\.\-\*\•\s]+", "", s).strip()
    return s


def is_valid_competency(skill: str) -> bool:
    """Return True if skill is meaningful and not generic filler."""
    if not skill or len(skill) < 2 or len(skill) > 60:
        return False
    lower = skill.lower().strip()
    if lower in VAGUE_KEYWORDS_BLACKLIST:
        return False
    for bad in VAGUE_KEYWORDS_BLACKLIST:
        if lower == bad or lower.startswith(bad + " ") or lower.endswith(" " + bad):
            return False
    return True


def _extract_seniority_tier(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\b(vp|vice president|head of|director|chief|cto|cio)\b", t):
        return "executive"
    if re.search(r"\b(principal|distinguished|staff|fellow)\b", t):
        return "staff_principal"
    if re.search(r"\b(architect|solutions architect|enterprise architect)\b", t):
        return "architect"
    if re.search(r"\b(engineering manager|manager|lead|tech lead)\b", t):
        return "lead_manager"
    if re.search(r"\b(senior|sr\.?|iii|level 3)\b", t):
        return "senior"
    if re.search(r"\b(junior|jr\.?|associate|entry|intern|internship|level 1|level i|sde i\b|sde 1\b)\b", t):
        return "junior"
    return "mid"


def _detect_candidate_seniority(resume_text: str) -> set[str]:
    top_resume = (resume_text or "")[:3000].lower()
    tiers = set()
    if re.search(r"\b(director|vp|vice president|head of|cto|cio|chief)\b", top_resume):
        tiers.add("executive")
    if re.search(r"\b(principal|staff|distinguished)\b", top_resume):
        tiers.add("staff_principal")
    if re.search(r"\b(architect|enterprise architect|solutions architect)\b", top_resume):
        tiers.add("architect")
    if re.search(r"\b(manager|engineering manager|tech lead|lead)\b", top_resume):
        tiers.add("lead_manager")
    if re.search(r"\b(senior|sr\.)\b", top_resume):
        tiers.add("senior")
    return tiers or {"mid"}


def calibrate_interview_probability(
    match_score: int,
    core_competencies: list[str],
    stretch_skills: list[str],
    job_title: str = "",
    resume_text: str = "",
    raw_interview_probability: int | None = None,
) -> int:
    """
    Calibrate realistic screening interview callback probability (0-100) distinct from raw technical match.
    
    Factors considered:
    1. Base technical requirements coverage (match_score).
    2. Stretch gaps penalty: ratio of missing requirements dampens callback probability.
    3. Seniority level alignment between job title and candidate career level in resume.
    4. Realistic recruiting bounds (even great fits rarely exceed 85% in competitive tech hiring).
    """
    if raw_interview_probability is not None and raw_interview_probability != match_score and 0 <= raw_interview_probability <= 100:
        return max(5, min(92, int(raw_interview_probability)))
    if match_score <= 0:
        return 0

    job_tier = _extract_seniority_tier(job_title)
    cand_tiers = _detect_candidate_seniority(resume_text)

    sen_adj = 0
    if job_tier == "executive":
        if "executive" not in cand_tiers and "lead_manager" not in cand_tiers:
            sen_adj = -25
        elif "executive" not in cand_tiers:
            sen_adj = -12
    elif job_tier == "staff_principal":
        if "staff_principal" not in cand_tiers and "architect" not in cand_tiers and "executive" not in cand_tiers:
            sen_adj = -18
    elif job_tier == "junior":
        if "executive" in cand_tiers or "staff_principal" in cand_tiers or "architect" in cand_tiers:
            sen_adj = -20
        elif "senior" in cand_tiers:
            sen_adj = -10

    core_count = len(core_competencies)
    stretch_count = len(stretch_skills)
    total = core_count + stretch_count
    gap_penalty = 0
    if total > 0:
        gap_ratio = stretch_count / total
        if gap_ratio > 0.3:
            gap_penalty = int((gap_ratio - 0.2) * 25)

    base = match_score + sen_adj - gap_penalty
    if match_score >= 80 and sen_adj == 0 and gap_penalty == 0:
        calibrated = min(85, match_score - 8)
    elif match_score < 40:
        calibrated = min(base, int(match_score * 0.5))
    else:
        calibrated = base

    if calibrated == match_score:
        calibrated = max(5, match_score - 7)
    return max(5, min(92, int(calibrated)))


class SkillRadarService:
    """
    Fit Diagnostics & Skill Radar extraction service.
    Uses local Ollama LLM to extract verified competencies, role gaps (stretch skills),
    and a diagnostic fit summary.
    """

    @classmethod
    def get_cache_key(cls, user_id: Any, job_id: Any, resume_id: Any = None) -> str:
        u_tag = str(user_id) if user_id else "anon"
        r_tag = str(resume_id) if resume_id else "none"
        return f"cockpit_ollama_skill_radar_{u_tag}_{r_tag}_{job_id}"

    @classmethod
    def analyze(
        cls,
        job_listing,
        resume_text: str,
        *,
        user=None,
        resume_id: Any = None,
        job_description: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        Analyze fit between a candidate's resume and a job listing.
        Returns a dict:
        {
            "match_score": int (0-100),
            "core_competencies": list[str],
            "stretch_skills": list[str],
            "fit_summary": str,
            "source": "ollama_local" | "cached" | "heuristic",
        }
        """
        if not job_listing:
            return cls._empty_result()

        job_id = getattr(job_listing, "id", None)
        cache_key = cls.get_cache_key(getattr(user, "id", None), job_id, resume_id)

        if not force_refresh:
            cached = cache.get(cache_key)
            if (
                cached
                and isinstance(cached, dict)
                and cached.get("source") != "error"
                and (cached.get("core_competencies") or cached.get("match_score") is not None)
            ):
                cached_res = dict(cached)
                cached_res["source"] = "cached"
                return cached_res

        # Try Ollama Local first
        from .llm.gateway import local_llm_available, invoke_llm_messages
        from .prompt_store import get_system_prompt_profile, resolve_prompt_parts

        if user and local_llm_available(user):
            try:
                profile = get_system_prompt_profile()
                sys_tmpl, usr_tmpl, leg_tmpl = resolve_prompt_parts(profile, "skill_radar")

                title = getattr(job_listing, "title", "Role") or "Role"
                desc = (job_description or getattr(job_listing, "description", None) or getattr(job_listing, "snippet", "") or "").strip()
                # Keep prompt payload compact for fast local inference (~3-4k chars each)
                desc_slice = desc[:4500].strip()
                resume_slice = (resume_text or "")[:4000].strip()

                if leg_tmpl:
                    prompt_text = leg_tmpl.replace("{job_title}", title).replace("{job_description}", desc_slice).replace("{resume_text}", resume_slice)
                    messages = [HumanMessage(content=prompt_text)]
                else:
                    sys_rendered = sys_tmpl
                    usr_rendered = usr_tmpl.replace("{job_title}", title).replace("{job_description}", desc_slice).replace("{resume_text}", resume_slice)
                    messages = [
                        SystemMessage(content=sys_rendered),
                        HumanMessage(content=usr_rendered),
                    ]

                response = invoke_llm_messages(
                    messages,
                    user=user,
                    prefer_local=True,
                    only_local=True,
                    allow_local=True,
                    usage_query_kind="skill_radar",
                )
                raw_content = response.content if hasattr(response, "content") else str(response)

                parsed = cls._parse_llm_response(raw_content)
                if parsed and (parsed.get("core_competencies") or parsed.get("match_score") is not None):
                    parsed["source"] = "ollama_local"
                    ms = parsed.get("match_score", 0)
                    calibrated_ip = calibrate_interview_probability(
                        ms,
                        parsed.get("core_competencies", []),
                        parsed.get("stretch_skills", []),
                        job_title=title,
                        resume_text=resume_text,
                        raw_interview_probability=parsed.get("interview_probability"),
                    )
                    parsed["interview_probability"] = calibrated_ip
                    cache.set(cache_key, parsed, CACHE_TTL_SECONDS)
                    return parsed
            except Exception as e:
                logger.warning(
                    "[SkillRadarService] Ollama Local extraction failed for job %s: %s",
                    job_id,
                    e,
                )
                return {
                    "match_score": 0,
                    "interview_probability": None,
                    "core_competencies": [],
                    "stretch_skills": [],
                    "fit_summary": f"Ollama Local analysis failed: {str(e)}",
                    "source": "error",
                    "error": str(e),
                }

        fallback = cls._fallback_heuristic(job_listing, resume_text)
        fallback["source"] = "heuristic"
        return fallback

    @classmethod
    def _parse_llm_response(cls, text: str) -> Optional[Dict[str, Any]]:
        """Parse JSON response from Ollama Local."""
        if not text:
            return None

        # Strip markdown fences if present
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        # Extract JSON object substring if model added preamble or trailing commentary
        json_match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if json_match:
            cleaned = json_match.group(1).strip()

        try:
            data = json.loads(cleaned)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        raw_core = data.get("core_competencies") or []
        raw_stretch = data.get("stretch_skills") or []
        match_score = data.get("match_score")
        raw_ip = (
            data.get("interview_probability")
            or data.get("interviewProbability")
            or data.get("interview_likelihood")
        )
        summary = str(data.get("fit_summary") or "").strip()

        core_clean = []
        for c in raw_core:
            if isinstance(c, str):
                c_clean = clean_skill_string(c)
                if is_valid_competency(c_clean) and c_clean not in core_clean:
                    core_clean.append(c_clean)

        stretch_clean = []
        for s in raw_stretch:
            if isinstance(s, str):
                s_clean = clean_skill_string(s)
                if is_valid_competency(s_clean) and s_clean not in stretch_clean and s_clean not in core_clean:
                    stretch_clean.append(s_clean)

        if not core_clean and not stretch_clean and match_score is None:
            return None

        try:
            score_int = int(match_score)
            score_int = max(0, min(100, score_int))
        except (ValueError, TypeError):
            total = len(core_clean) + len(stretch_clean)
            score_int = round((len(core_clean) / total) * 100) if total > 0 else 75

        ip_int = None
        if raw_ip is not None:
            try:
                ip_int = max(0, min(100, int(raw_ip)))
            except (ValueError, TypeError):
                ip_int = None

        return {
            "match_score": score_int,
            "interview_probability": ip_int,
            "core_competencies": core_clean[:8],
            "stretch_skills": stretch_clean[:6],
            "fit_summary": summary,
        }

    @classmethod
    def _fallback_heuristic(cls, job_listing, resume_text: str) -> Dict[str, Any]:
        """Fast heuristic fallback with strict anti-vague filtering."""
        from .resume_keyword_miner import mine_keywords_from_jobs

        title = getattr(job_listing, "title", "") or ""
        desc = getattr(job_listing, "description", None) or getattr(job_listing, "snippet", "") or ""
        kw = mine_keywords_from_jobs([(title, desc)], max_phrases=25)

        r_lower = (resume_text or "").lower()
        core = []
        stretch = []

        for item in kw:
            phrase = item.get("phrase", "")
            if not is_valid_competency(phrase):
                continue
            p_title = phrase.title()
            words = [w for w in phrase.lower().split() if len(w) > 3]
            if phrase.lower() in r_lower or (words and any(w in r_lower for w in words)):
                if p_title not in core:
                    core.append(p_title)
            else:
                if p_title not in stretch:
                    stretch.append(p_title)

        total = len(core) + len(stretch)
        calc_pct = round((len(core) / total) * 100) if total > 0 else (getattr(job_listing, "focus_percent", None) or 75)
        calibrated_ip = calibrate_interview_probability(
            calc_pct,
            core[:8],
            stretch[:6],
            job_title=title,
            resume_text=resume_text,
        )

        return {
            "match_score": calc_pct,
            "interview_probability": calibrated_ip,
            "core_competencies": core[:8],
            "stretch_skills": stretch[:6],
            "fit_summary": "Extracted via keyword matching; run Ollama Local for deep semantic diagnostics.",
        }

    @classmethod
    def _empty_result(cls) -> Dict[str, Any]:
        return {
            "match_score": 0,
            "interview_probability": None,
            "core_competencies": [],
            "stretch_skills": [],
            "fit_summary": "",
            "source": "none",
        }
