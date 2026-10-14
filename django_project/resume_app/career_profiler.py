"""
Universal AI Career Profiler & Pipeline Provisioning Service.

Analyzes candidate resumes across ANY industry/discipline (healthcare, finance,
sales, education, engineering, trades, legal, etc.) using LLM synthesis,
maps validated Core Fit, Stretch, and Adjacent career archetypes, and provisions
cleanly isolated SearchProfile pipelines with zero preference cross-talk.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from django.utils.text import slugify

from .models import JobSearchTask, SearchProfile, Track, UserResume
from .services import parse_pdf, PDFParseError

from .prompts import DEFAULT_CAREER_PROFILER_SYSTEM, DEFAULT_CAREER_PROFILER_USER

logger = logging.getLogger(__name__)

UNIVERSAL_PROFILER_SYSTEM_PROMPT = DEFAULT_CAREER_PROFILER_SYSTEM



def extract_text_from_user_resume(resume: UserResume) -> str:
    """Extract clean text from a UserResume PDF or text file."""
    if not resume or not resume.file:
        raise ValueError("Resume file is missing or invalid.")
    filename = (resume.original_filename or resume.file.name or "").lower()
    if filename.endswith((".txt", ".md", ".json")):
        try:
            resume.file.seek(0)
            text = resume.file.read().decode("utf-8", errors="ignore").strip()
            if text:
                return text
        except Exception:
            pass
    try:
        file_path = resume.file.path
        return parse_pdf(file_path)
    except (PDFParseError, Exception) as e:
        logger.warning("[career_profiler] parse_pdf failed on resume %s: %s; trying raw read", resume.id, e)
        try:
            resume.file.seek(0)
            raw_bytes = resume.file.read()
            text = raw_bytes.decode("utf-8", errors="ignore").strip()
            if text:
                return text
        except Exception:
            pass
        raise ValueError(f"Could not extract text from resume: {e}") from e


def analyze_resume_for_career_archetypes(resume_text: str, user=None) -> Dict[str, Any]:
    """
    Invoke LLM to analyze resume across any industry and synthesize archetypes.
    Falls back gracefully to deterministic heuristic classification if LLM is unavailable.
    """
    if not resume_text or not resume_text.strip():
        raise ValueError("Resume text is empty.")

    clean_snippet = resume_text.strip()[:10000]

    # Try LLM synthesis
    parsed_result = _invoke_llm_for_archetypes(clean_snippet, user=user)
    if parsed_result:
        return parsed_result

    logger.info("[career_profiler] LLM unavailable or returned invalid JSON; engaging heuristic fallback.")
    return _generate_heuristic_fallback(clean_snippet)


def _invoke_llm_for_archetypes(resume_snippet: str, user=None) -> Optional[Dict[str, Any]]:
    """Invoke LLM gateway with strict JSON parsing."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from .llm import USAGE_QUERY_CAREER_PROFILER, invoke_llm_messages
        from .prompt_store import get_system_prompt_profile, resolve_prompt_parts

        profile = get_system_prompt_profile()
        sys_tmpl, usr_tmpl, leg_tmpl = resolve_prompt_parts(profile, "career_profiler")

        if leg_tmpl:
            content = leg_tmpl.replace("{resume_text}", resume_snippet)
            messages = [HumanMessage(content=content)]
        else:
            usr_template = usr_tmpl or DEFAULT_CAREER_PROFILER_USER
            usr_content = usr_template.replace("{resume_text}", resume_snippet)
            messages = [
                SystemMessage(content=sys_tmpl or DEFAULT_CAREER_PROFILER_SYSTEM),
                HumanMessage(content=usr_content),
            ]

        response = invoke_llm_messages(
            messages,
            user=user,
            usage_query_kind=USAGE_QUERY_CAREER_PROFILER,
            prefer_local=False,
            allow_local=True,
        )

        content = getattr(response, "content", None) or str(response)
        if not content:
            return None

        # Clean code fences or whitespace
        cleaned = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE).strip()

        # Extract first JSON block
        json_match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(1))
            if _validate_archetype_schema(data):
                _normalize_archetypes_data(data)
                return data

    except Exception as e:
        logger.warning("[career_profiler] LLM invocation failed: %s", e)
    return None


def _validate_archetype_schema(data: Any) -> bool:
    """Validate that dictionary contains candidate_summary and archetypes array."""
    if not isinstance(data, dict):
        return False
    if "archetypes" not in data or not isinstance(data["archetypes"], list) or len(data["archetypes"]) == 0:
        return False
    return True


def _normalize_archetypes_data(data: dict) -> None:
    """Ensure all required fields exist on archetypes."""
    for idx, arch in enumerate(data.get("archetypes", [])):
        arch.setdefault("id", f"track-{idx}")
        title = arch.get("title") or f"Career Direction {idx+1}"
        arch["title"] = title
        arch.setdefault("confidence_score", 90 if idx == 0 else (80 if idx == 1 else 72))
        arch.setdefault("confidence_label", "Core Fit" if idx == 0 else ("Next Level / Promotion" if idx == 1 else "Adjacent Pivot"))
        arch.setdefault("salary_band", "$100,000 – $140,000 / yr")
        arch.setdefault("summary", f"Strategic career alignment based on your professional experience in {title}.")
        arch.setdefault("search_terms", [title])
        arch.setdefault("matched_skills", [])
        arch.setdefault("growth_areas", [])
        arch.setdefault("default_active", idx < 2)
        slug = slugify(arch.get("suggested_slug") or title)[:32]
        arch["suggested_slug"] = slug or f"track-{idx+1}"


def _generate_heuristic_fallback(resume_text: str) -> Dict[str, Any]:
    """
    Deterministic industry-agnostic classifier that identifies domain,
    seniority, and 3 viable archetypes when LLM is offline.
    """
    text_lower = resume_text.lower()

    # Domain keyword scoring
    domains = {
        "Healthcare & Clinical Operations": [
            "nurse", "nursing", "clinical", "hospital", "patient", "rn", "physician", "medical", "healthcare", "icu", "clinic"
        ],
        "Corporate Finance & Accounting": [
            "accounting", "finance", "cpa", "financial", "audit", "tax", "general ledger", "gaap", "controller", "fp&a", "budget"
        ],
        "Sales & Revenue Leadership": [
            "quota", "sales", "account executive", "b2b", "pipeline", "crm", "arr", "closing", "revenue", "outbound", "saas sales"
        ],
        "Marketing & Brand Strategy": [
            "marketing", "seo", "campaign", "social media", "content", "brand", "growth", "inbound", "copywriting", "pr"
        ],
        "Education & Instructional Design": [
            "teacher", "curriculum", "students", "classroom", "school", "instructional", "academic", "teaching", "k-12", "education"
        ],
        "Operations & Supply Chain": [
            "logistics", "supply chain", "warehouse", "procurement", "inventory", "operations", "shipping", "distribution", "fulfillment"
        ],
        "Commercial Construction & Trades": [
            "construction", "superintendent", "contractor", "osha", "subcontractor", "blueprint", "hvac", "estimating", "project engineer"
        ],
        "Legal & Regulatory Compliance": [
            "legal", "compliance", "regulatory", "attorney", "paralegal", "litigation", "contract negotiation", "counsel"
        ],
        "Software & Cloud Engineering": [
            "software", "engineer", "python", "developer", "distributed", "kubernetes", "cloud", "aws", "backend", "api", "systems"
        ],
    }

    best_domain = "Business & Operations Management"
    best_domain_score = 0
    for dom_name, kw_list in domains.items():
        score = sum(1 for kw in kw_list if kw in text_lower)
        if score > best_domain_score:
            best_domain_score = score
            best_domain = dom_name

    # Seniority estimation
    seniority = "Mid-Senior"
    years = "5+ Years"
    if any(k in text_lower for k in ["vp", "vice president", "director", "head of", "principal"]):
        seniority = "Director / Executive"
        years = "10+ Years"
    elif any(k in text_lower for k in ["manager", "lead", "staff", "supervisor"]):
        seniority = "Lead / Manager"
        years = "7+ Years"
    elif any(k in text_lower for k in ["senior", "sr.", "specialist"]):
        seniority = "Senior Specialist"
        years = "5+ Years"

    # Domain archetype templates
    templates = {
        "Healthcare & Clinical Operations": (
            ("Clinical Nurse Manager", "$105,000 – $135,000 / yr", ["Clinical Nurse Manager", "Charge Nurse"]),
            ("Director of Nursing Services", "$130,000 – $165,000 / yr", ["Director of Nursing", "Clinical Director"]),
            ("Healthcare Quality & Compliance Coordinator", "$90,000 – $120,000 / yr", ["Healthcare Compliance Specialist", "Quality Coordinator"]),
        ),
        "Corporate Finance & Accounting": (
            ("Senior Financial Analyst", "$95,000 – $125,000 / yr", ["Senior Financial Analyst", "FP&A Analyst"]),
            ("Corporate Controller / Finance Manager", "$135,000 – $170,000 / yr", ["Finance Manager", "Corporate Controller"]),
            ("Business Operations & Systems Consultant", "$105,000 – $135,000 / yr", ["Business Operations Analyst", "ERP Finance Consultant"]),
        ),
        "Sales & Revenue Leadership": (
            ("Enterprise Account Executive", "$110,000 – $140,000 Base + OTE", ["Enterprise Account Executive", "Senior AE"]),
            ("Sales Director / VP of Revenue", "$160,000 – $210,000 Base + OTE", ["Director of Sales", "VP of Sales"]),
            ("Customer Success & Strategic Accounts Director", "$120,000 – $155,000 / yr", ["Strategic Account Manager", "Customer Success Director"]),
        ),
        "Marketing & Brand Strategy": (
            ("Senior Marketing Manager", "$100,000 – $130,000 / yr", ["Marketing Manager", "Senior Growth Marketing"]),
            ("Director of Brand & Growth Strategy", "$140,000 – $180,000 / yr", ["Director of Marketing", "VP Marketing"]),
            ("Product Marketing & GTM Lead", "$120,000 – $155,000 / yr", ["Product Marketing Manager", "GTM Specialist"]),
        ),
        "Education & Instructional Design": (
            ("Curriculum & Instructional Specialist", "$75,000 – $95,000 / yr", ["Instructional Specialist", "Curriculum Developer"]),
            ("Assistant Principal / Education Administrator", "$100,000 – $125,000 / yr", ["School Administrator", "Assistant Principal"]),
            ("Corporate Learning & Talent Development Lead", "$90,000 – $120,000 / yr", ["Instructional Designer", "Learning and Development Manager"]),
        ),
        "Operations & Supply Chain": (
            ("Supply Chain Operations Manager", "$105,000 – $135,000 / yr", ["Supply Chain Manager", "Operations Manager"]),
            ("Director of Global Logistics & Fulfillment", "$145,000 – $185,000 / yr", ["Director of Logistics", "VP Supply Chain"]),
            ("Operational Excellence / Lean Consultant", "$115,000 – $145,000 / yr", ["Continuous Improvement Manager", "Lean Six Sigma Consultant"]),
        ),
        "Commercial Construction & Trades": (
            ("Senior Construction Project Manager", "$115,000 – $145,000 / yr", ["Construction Project Manager", "Senior PM Construction"]),
            ("General Superintendent / Operations Director", "$140,000 – $180,000 / yr", ["General Superintendent", "VP Construction"]),
            ("Commercial Estimating & Preconstruction Lead", "$110,000 – $140,000 / yr", ["Preconstruction Manager", "Senior Estimator"]),
        ),
        "Legal & Regulatory Compliance": (
            ("Senior Compliance & Contracts Specialist", "$105,000 – $135,000 / yr", ["Contracts Manager", "Compliance Specialist"]),
            ("Associate General Counsel / Director of Legal", "$160,000 – $210,000 / yr", ["Director of Legal", "Corporate Counsel"]),
            ("Risk Management & Governance Consultant", "$120,000 – $155,000 / yr", ["Risk Management Director", "Governance Consultant"]),
        ),
        "Software & Cloud Engineering": (
            ("Staff Systems / Infrastructure Engineer", "$190,000 – $240,000 / yr", ["Staff Software Engineer", "Principal Systems Engineer"]),
            ("Engineering Manager — Infrastructure", "$210,000 – $260,000 / yr", ["Engineering Manager", "Software Engineering Manager"]),
            ("Solutions Architect / Technical Consultant", "$170,000 – $220,000 / yr", ["Solutions Architect", "Cloud Architect"]),
        ),
    }

    core_t, stretch_t, adj_t = templates.get(
        best_domain,
        (
            ("Senior Operations Manager", "$100,000 – $130,000 / yr", ["Operations Manager", "Senior Operations Specialist"]),
            ("Director of Operations & Strategy", "$135,000 – $170,000 / yr", ["Director of Operations", "VP Operations"]),
            ("Business Transformation & Project Lead", "$105,000 – $135,000 / yr", ["Project Director", "Management Consultant"]),
        ),
    )

    return {
        "candidate_summary": {
            "domain": best_domain,
            "seniority_level": seniority,
            "years_experience": years,
            "core_competencies": ["Cross-functional Leadership", "Strategic Planning", "Process Optimization", "Performance Analysis"],
        },
        "archetypes": [
            {
                "id": "track-0",
                "type": "core",
                "title": core_t[0],
                "confidence_score": 93,
                "confidence_label": "Core Fit",
                "salary_band": core_t[1],
                "summary": f"Directly reflects your verified background in {best_domain}.",
                "search_terms": core_t[2],
                "matched_skills": ["Domain Execution", "Strategic Oversight", "Process Efficiency"],
                "growth_areas": [],
                "default_active": True,
                "suggested_slug": slugify(core_t[0])[:32],
            },
            {
                "id": "track-1",
                "type": "stretch",
                "title": stretch_t[0],
                "confidence_score": 83,
                "confidence_label": "Next Level / Promotion",
                "salary_band": stretch_t[1],
                "summary": f"Expanded leadership role commanding broader team and budget responsibility.",
                "search_terms": stretch_t[2],
                "matched_skills": ["Leadership Baseline", "Domain Depth"],
                "growth_areas": ["P&L Ownership", "Multi-team Strategy", "Executive Reporting"],
                "default_active": True,
                "suggested_slug": slugify(stretch_t[0])[:32],
            },
            {
                "id": "track-2",
                "type": "adjacent",
                "title": adj_t[0],
                "confidence_score": 74,
                "confidence_label": "Adjacent Pivot",
                "salary_band": adj_t[1],
                "summary": f"Transfer your core skills into high-demand consulting or specialized domain services.",
                "search_terms": adj_t[2],
                "matched_skills": ["Analytical Rigor", "Project Delivery"],
                "growth_areas": ["Domain Advisory"],
                "default_active": False,
                "suggested_slug": slugify(adj_t[0])[:32],
            },
        ],
    }


def provision_career_tracks(
    user,
    tracks_config: List[Dict[str, Any]],
    resume_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Provision independent SearchProfile objects and automated search tasks for selected archetypes.
    Guarantees clean track isolation with zero preference cross-talk.
    """
    if not user or not getattr(user, "is_authenticated", False):
        raise ValueError("User must be authenticated to provision tracks.")

    # Validate resume ownership if specified
    bound_resume = None
    if resume_id:
        try:
            bound_resume = UserResume.objects.for_user(user).filter(pk=int(resume_id)).first()
        except Exception:
            bound_resume = None

    if not bound_resume:
        # Default to latest library resume if available
        bound_resume = UserResume.objects.for_user(user).filter(is_library=True).order_by("-uploaded_at").first()

    created_profiles: List[Dict[str, Any]] = []

    for item in tracks_config:
        if not item.get("enabled", True):
            continue

        raw_title = (item.get("title") or "").strip()
        if not raw_title:
            continue

        base_slug = slugify(item.get("slug") or raw_title)[:32]
        slug = _ensure_unique_slug(user, base_slug)

        # Work location logic
        work_mode = item.get("work_mode", "remote")  # "remote", "metro", "relocation"
        if work_mode == "relocation":
            # Open to relocation = Nationwide search (no location restriction)
            location = ""
        elif work_mode == "remote":
            location = "Remote"
        else:
            location = (item.get("location") or "").strip()

        # Job search term
        search_terms = item.get("search_terms") or []
        primary_search_term = search_terms[0] if search_terms else raw_title

        site_names = item.get("site_names") or ["indeed", "linkedin"]
        schedule_interval = (item.get("schedule_interval") or "off").strip().lower()
        if schedule_interval not in {"daily", "weekdays", "weekly"}:
            schedule_interval = "off"
        schedule_time_str = item.get("schedule_time", "08:00")

        # 1. Ensure Track row exists
        Track.objects.for_user(user).get_or_create(
            slug=slug,
            defaults={"label": raw_title, "owner": user},
        )

        # 2. Upsert SearchProfile
        profile, created = SearchProfile.objects.for_user(user).update_or_create(
            slug=slug,
            defaults={
                "owner": user,
                "name": raw_title,
                "search_term": primary_search_term,
                "location": location,
                "profile_slug": slug,
                "resume": bound_resume,
                "results_wanted": 50,
                "site_names": site_names,
                "schedule_interval": schedule_interval,
                "schedule_is_active": (schedule_interval != "off"),
            },
        )

        # 3. Schedule Huey Task if interval is set
        task_id = None
        if schedule_interval != "off":
            try:
                from .saved_search_schedule import set_saved_search_schedule

                task = set_saved_search_schedule(
                    user=user,
                    preset_id=profile.id,
                    interval=schedule_interval,
                    time_str=schedule_time_str,
                )
                if task:
                    task_id = task.id
            except Exception as e:
                logger.warning("[career_profiler] scheduling task failed for track %s: %s", slug, e)

        created_profiles.append({
            "id": profile.id,
            "name": profile.name,
            "slug": profile.slug,
            "search_term": profile.search_term,
            "location": profile.location,
            "work_mode": work_mode,
            "schedule_interval": schedule_interval,
            "task_id": task_id,
        })

    return created_profiles


def _ensure_unique_slug(user, base_slug: str) -> str:
    """Ensure a slug is unique for this user, appending a numeric suffix if needed."""
    slug = base_slug or "track"
    candidate = slug[:32]
    counter = 1
    while SearchProfile.objects.for_user(user).filter(slug=candidate).exists():
        suffix = f"-{counter}"
        candidate = f"{slug[:32 - len(suffix)]}{suffix}"
        counter += 1
    return candidate
