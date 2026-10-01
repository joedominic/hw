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
            if cached and isinstance(cached, dict) and cached.get("core_competencies"):
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
                desc = getattr(job_listing, "description", None) or getattr(job_listing, "snippet", "") or ""
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
                if parsed and parsed.get("core_competencies"):
                    parsed["source"] = "ollama_local"
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
                    "core_competencies": [],
                    "stretch_skills": [],
                    "fit_summary": f"Ollama Local analysis failed: {str(e)}",
                    "source": "error",
                    "error": str(e),
                }

        return {
            "match_score": 0,
            "core_competencies": [],
            "stretch_skills": [],
            "fit_summary": "Ollama Local is currently unavailable.",
            "source": "error",
            "error": "Ollama Local is unavailable",
        }

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

        if not core_clean and not stretch_clean:
            return None

        try:
            score_int = int(match_score)
            score_int = max(0, min(100, score_int))
        except (ValueError, TypeError):
            total = len(core_clean) + len(stretch_clean)
            score_int = round((len(core_clean) / total) * 100) if total > 0 else 75

        return {
            "match_score": score_int,
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

        return {
            "match_score": calc_pct,
            "core_competencies": core[:8],
            "stretch_skills": stretch[:6],
            "fit_summary": "Extracted via keyword matching; run Ollama Local for deep semantic diagnostics.",
        }

    @classmethod
    def _empty_result(cls) -> Dict[str, Any]:
        return {
            "match_score": 0,
            "core_competencies": [],
            "stretch_skills": [],
            "fit_summary": "",
            "source": "none",
        }
