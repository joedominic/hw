import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from django.core.cache import cache
from langchain_core.messages import HumanMessage, SystemMessage
from .llm.gateway import invoke_llm_messages

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


CLOUD_PLATFORM_KEYWORDS = (
    "aws", "amazon web services", "gcp", "google cloud", "azure", "microsoft azure", "oci", "oracle cloud"
)


def _has_verified_cloud_background(resume_text: str) -> bool:
    """Return True if candidate demonstrates substantiated major public cloud experience in resume."""
    r = (resume_text or "").lower()
    return any(
        bool(re.search(r"\b" + re.escape(c) + r"\b", r))
        for c in ("gcp", "google cloud", "azure", "aws", "amazon web services", "oci", "cloud-native", "cloud native", "cloud architecture", "kubernetes")
    )


def _is_cloud_platform_gap(gap_str: str) -> bool:
    """Return True if gap represents an alternative public cloud platform rather than runtime/domain gap."""
    g = (gap_str or "").strip().lower()
    if not g:
        return False
    return any(bool(re.search(r"\b" + re.escape(c) + r"\b", g)) for c in CLOUD_PLATFORM_KEYWORDS)


CREDENTIAL_INDICATORS = {
    "certification", "certifications", "certified", "degree", "degrees",
    "license", "licenses", "licensure", "certificate", "certificates",
    "credential", "credentials", "training",
}


def clean_skill_string(s: str) -> str:
    """Normalize skill name: trim, remove leading bullets/numbers without stripping .NET or similar."""
    s = re.sub(r"^(\d+[\.\)]|\-|\*|\•)\s*", "", s).strip()
    # Strip common conversational negative prefixes if returned by LLM
    s = re.sub(
        r"^(no demonstrated (experience with|experience in|background in|knowledge of)|no explicit|lack of|missing|absence of)\s+",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    return s


def is_valid_competency(skill: str, max_len: int = 90) -> bool:
    """Return True if skill is meaningful and not generic filler."""
    if not skill or len(skill) < 2 or len(skill) > max_len:
        return False
    lower = skill.lower().strip()
    if lower in VAGUE_KEYWORDS_BLACKLIST:
        return False
    for bad in VAGUE_KEYWORDS_BLACKLIST:
        if lower == bad or lower.startswith(bad + " ") or lower.endswith(" " + bad):
            return False
    return True


def is_skill_covered_by_core(stretch_item: str, core_items: list[str]) -> bool:
    """Return True if a stretch skill candidate is already substantiated in core competencies."""
    s_clean = (stretch_item or "").strip().lower()
    if not s_clean:
        return True
    s_tokens = set(re.findall(r"\b[a-zA-Z0-9\+\#\.]+\b", s_clean))
    for core in core_items:
        c_lower = (core or "").strip().lower()
        if s_clean == c_lower or s_clean in c_lower:
            return True
        c_tokens = set(re.findall(r"\b[a-zA-Z0-9\+\#\.]+\b", c_lower))
        if s_tokens and s_tokens.issubset(c_tokens):
            return True
    return False


def _extract_seniority_tier(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\b(vp|vice president|head of|director|chief|cto|cio)\b", t):
        return "executive"
    if re.search(r"\b(principal|distinguished|staff|fellow|level [5-7]|level [v|vi|vii]|ic[5-7]|engineer (software )?[5-7]|software (engineer )?[5-7]|swe [5-7])\b", t):
        return "staff_principal"
    if re.search(r"\b(architect|solutions architect|enterprise architect)\b", t):
        return "architect"
    if re.search(r"\b(engineering manager|manager|lead|tech lead)\b", t):
        return "lead_manager"
    if re.search(r"\b(senior|sr\.?|iii|level 3|level 4|level iv|engineer (software )?[3-4]|software (engineer )?[3-4]|swe [3-4]|ic[3-4])\b", t):
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
    if re.search(r"\b(architect|architects|architecture|enterprise architect|solutions architect)\b", top_resume):
        tiers.add("architect")
    if re.search(r"\b(manager|engineering manager|tech lead|lead)\b", top_resume):
        tiers.add("lead_manager")
    if re.search(r"\b(senior|sr\.)\b", top_resume):
        tiers.add("senior")
    return tiers or {"mid"}


COMMON_DOMAIN_STOPWORDS = {
    "and", "the", "for", "with", "system", "systems", "platform", "platforms",
    "architecture", "architectures", "management", "design", "strategy", "patterns",
    "engineering", "development", "practices", "best", "level", "suite", "tools",
    "services", "solutions", "environment", "environments", "operations", "infrastructure",
    "ehr", "emr", "crm", "erp", "database", "databases", "cloud", "api", "apis",
    "framework", "frameworks", "software", "model", "models", "stack", "pipeline", "pipelines",
    "integration", "integrations", "specific", "compliance", "implementation", "implementations",
    "process", "processes", "application", "applications", "technologies", "technology",
    "standard", "standards", "methodology", "methodologies", "automated", "automation",
    "enterprise", "business", "commercial", "corporate", "context", "domain",
}

HARDWARE_EMBEDDED_KEYWORDS = {
    "embedded", "firmware", "semiconductor", "silicon", "fpga", "asic",
    "rtl", "verilog", "vhdl", "pcb", "microcontroller", "arm cortex",
    "thermal management", "circuit design", "autonomous vehicle", "robotics",
}

HARDWARE_SYSTEMS_COMPANIES = {
    "broadcom", "qualcomm", "intel", "amd", "nvidia", "marvell", "asml",
    "arm", "applied materials", "lam research", "texas instruments",
    "nxp", "analog devices", "micron", "synopsys", "cadence",
    "microchip", "tsmc", "kla", "western digital", "seagate",
}


def is_duplicate_or_subsumed(new_item: str, existing_items: list[str]) -> bool:
    """Return True if new_item is an exact duplicate, substring, or shares distinctive non-stopword tokens with an existing item."""
    if not new_item or not existing_items:
        return False
    n_clean = clean_skill_string(new_item).lower()
    if not n_clean:
        return True
    for existing in existing_items:
        e_clean = clean_skill_string(existing).lower()
        if not e_clean:
            continue
        if n_clean == e_clean or n_clean in e_clean or e_clean in n_clean:
            return True
        n_tokens = {w for w in re.findall(r"\b[a-zA-Z0-9\+\#\.]+\b", n_clean) if len(w) >= 3 and w not in COMMON_DOMAIN_STOPWORDS}
        e_tokens = {w for w in re.findall(r"\b[a-zA-Z0-9\+\#\.]+\b", e_clean) if len(w) >= 3 and w not in COMMON_DOMAIN_STOPWORDS}
        if n_tokens and e_tokens and (n_tokens == e_tokens or n_tokens.issubset(e_tokens) or e_tokens.issubset(n_tokens)):
            return True
    return False

CLINICAL_HEALTHCARE_KEYWORDS = {
    "physician", "doctor", "surgeon", "registered nurse", "clinical nurse",
    "patient care", "oncology", "pediatrics", "icu nurse", "pharmacist",
}

SUPPLY_CHAIN_LOGISTICS_KEYWORDS = {
    "supply chain", "logistics", "warehouse", "wms", "tms", "inventory management",
    "procurement", "fulfillment", "distribution center",
}

FINTECH_BANKING_KEYWORDS = {
    "banking", "financial crimes", "fincen", "sar", "cdd", "aml", "kyc",
    "trading", "capital markets", "origination", "wealth management", "credit risk",
}

PEOPLE_LEADERSHIP_PATTERNS = {
    "leadership", "talent development", "people management", "organizational leadership",
    "team development", "mentoring", "mentorship", "talent acquisition", "engineering leadership",
    "team leadership", "organizational scaling", "talent management", "workforce planning",
}


def _sanitize_resume_for_skills(text: str) -> str:
    """
    Remove corporate acquisition notes and parenthetical employer metadata
    that should not be conflated with hands-on technical skills (e.g. '(Acquired by SAP)').
    """
    if not text:
        return ""
    return re.sub(
        r"\((acquired by|formerly|merged with|subsidiary of|division of|now part of|now)\s+[^)]+\)",
        "",
        text,
        flags=re.IGNORECASE,
    )


def _token_matches(token: str, text_lower: str) -> bool:
    """Check if token or its morphological stem/variants appear in text_lower."""
    if not token or not text_lower:
        return False
    if bool(re.search(r"\b" + re.escape(token) + r"\b", text_lower)):
        return True
    # Strip common noun/verb suffixes to match inflections (leads/leader/leadership, manage/manager/management)
    for suffix in ("ship", "ing", "ers", "er", "ment", "tional", "tion", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            stem = token[:-len(suffix)]
            if re.search(r"\b" + re.escape(stem) + r"[a-z]*\b", text_lower):
                return True
    return False


def _has_verified_people_leadership(resume_text: str) -> bool:
    """Return True if candidate demonstrates substantive team, manager, or organizational leadership."""
    if not resume_text:
        return False
    r = resume_text.lower()
    has_mgr_scope = bool(re.search(r"\b(directed|managed|led)\s+([a-z0-9\-]+\s+)?(team|organization|engineers|managers|reports|staff|department)\b", r))
    has_headcount = bool(re.search(r"\b(\d+[\-\s]?member|\d+\s+engineers|\d+\s+managers|\d+\s+direct reports)\b", r))
    has_tier = bool(re.search(r"\b(executive director|director|vice president|vp|engineering manager|head of)\b", r))
    return (has_mgr_scope and has_headcount) or (has_tier and has_mgr_scope)


INCIDENTAL_WORKFLOW_TOOLS = {
    "jira", "confluence", "trello", "asana", "power bi", "tableau", "excel",
    "microsoft excel", "slack", "visio", "powerpoint", "word", "sharepoint",
    "zoom", "teams", "miro", "lucidchart", "smartsheet", "basecamp", "monday.com",
    "postman", "swagger", "git", "github", "gitlab", "bitbucket",
}


def evaluate_skill_gap_severity(
    skill: str,
    job_title: str = "",
    job_description: str = "",
) -> Tuple[int, int, str]:
    """
    Evaluates the penalty impact of a stretch skill in the context of the specific job.
    Returns: (match_penalty, probability_penalty, severity_tier)

    Tiers:
    - CRITICAL_HEADLINE: Skill appears in Job Title (e.g. 'Power BI' for 'Power BI Developer')
        Penalty: -6% match, -8% probability
    - STRUCTURAL_REQUIREMENT: High frequency (>= 3 mentions in JD)
        Penalty: -4% match, -5% probability
    - SECONDARY_GAP: Standard role gap (1-2 mentions in body)
        Penalty: -3% match, -4% probability
    - INCIDENTAL_TOOL: Universal productivity/workflow tool (Jira, Power BI, Slack, Confluence, Excel)
      AND not in job title AND not a high-frequency focus in JD.
        Penalty: -1% match, -1% probability
    """
    if not skill:
        return 0, 0, "NONE"

    s_lower = skill.strip().lower()
    title_lower = (job_title or "").strip().lower()
    jd_lower = (job_description or "").strip().lower()

    # Rule 1: Headline Prominence (Skill appears in the job title)
    tokens = [w for w in re.findall(r"\b[a-zA-Z0-9\+\#\.]+\b", s_lower) if len(w) >= 3 and w not in COMMON_DOMAIN_STOPWORDS]
    if s_lower in title_lower or (tokens and any(re.search(r"\b" + re.escape(t) + r"\b", title_lower) for t in tokens)):
        return 6, 8, "CRITICAL_HEADLINE"

    # Rule 2: Incidental productivity/workflow tool dampening
    is_incidental = s_lower in INCIDENTAL_WORKFLOW_TOOLS or any(
        tool in s_lower for tool in INCIDENTAL_WORKFLOW_TOOLS
    )

    # Check mention frequency in JD
    mention_count = 0
    if tokens:
        mention_count = sum(len(re.findall(r"\b" + re.escape(t) + r"\b", jd_lower)) for t in tokens)
    elif s_lower:
        mention_count = len(re.findall(r"\b" + re.escape(s_lower) + r"\b", jd_lower))

    if is_incidental:
        if mention_count >= 3:
            return 3, 3, "SECONDARY_GAP"
        return 1, 1, "INCIDENTAL_TOOL"

    # Rule 3: Structural vs Secondary Requirement
    if mention_count >= 3:
        return 4, 5, "STRUCTURAL_REQUIREMENT"

    return 3, 4, "SECONDARY_GAP"


def calculate_categorical_scores(
    domain_alignment: Optional[str],
    seniority_alignment: Optional[str] = None,
    core_count: int = 4,
    stretch_count: int = 2,
    stretch_skills: Optional[List[str]] = None,
    knockout_gaps: Optional[List[str]] = None,
    secondary_gaps: Optional[List[str]] = None,
    job_title: str = "",
    job_description: str = "",
) -> Tuple[int, int]:
    """
    Computes match_score and interview_probability deterministically from categorical alignments,
    structural knockout gaps, and contextual job-prominence severity.
    Eliminates SLM integer inflation on cross-industry roles, enforces hard ceilings for missing core
    prerequisites (knockout gaps), and penalizes executive scope vs. hands-on execution mismatches.
    """
    align = (domain_alignment or "").strip().upper()
    sen = (seniority_alignment or "").strip().upper()

    has_explicit_qual_gaps = knockout_gaps is not None or secondary_gaps is not None
    ko_list = [k for k in (knockout_gaps or []) if k]
    sec_list = [s for s in (secondary_gaps or []) if s]

    if align == "CROSS_INDUSTRY_MISMATCH":
        base_match = 35
        base_ip = 18
        effective_stretch = (len(ko_list) + len(sec_list)) if has_explicit_qual_gaps else stretch_count
        # Strictly cap match <= 42 and probability <= 25
        match_score = min(42, max(15, base_match - (effective_stretch * 2)))
        ip = min(25, max(5, base_ip - (effective_stretch * 2)))
        return match_score, ip

    if align == "ADJACENT_DOMAIN":
        base_match = 68
        base_ip = 58
    elif align == "DIRECT_MATCH":
        base_match = 88
        base_ip = 82
    else:
        # Default fallback if unspecified
        base_match = 75
        base_ip = 65

    # Contextual deductions for qualification gaps
    total_m_pen = 0
    total_ip_pen = 0

    if has_explicit_qual_gaps:
        # Explicit knockout vs secondary gap rubric
        total_m_pen = (len(ko_list) * 12) + (len(sec_list) * 3)
        total_ip_pen = (len(ko_list) * 16) + (len(sec_list) * 4)
    elif stretch_skills:
        for s in stretch_skills:
            m_pen, ip_pen, _ = evaluate_skill_gap_severity(s, job_title=job_title, job_description=job_description)
            total_m_pen += m_pen
            total_ip_pen += ip_pen
    else:
        total_m_pen = stretch_count * 3
        total_ip_pen = stretch_count * 4

    match_score = base_match - total_m_pen
    ip = base_ip - total_ip_pen

    # Core competency depth adjustments
    if core_count >= 5:
        match_score += 2
        ip += 2
    elif core_count == 3:
        match_score -= 2
        ip -= 2
    elif core_count <= 2:
        match_score -= 5
        ip -= 6

    # Seniority adjustments
    if sen == "SCOPE_MISMATCH":
        match_score -= 10
        ip -= 15
    elif sen == "OVERQUALIFIED":
        match_score -= 8
        ip -= 10
    elif sen == "UNDERQUALIFIED":
        match_score -= 15
        ip -= 20

    # Hard Ceilings when Knockouts are explicitly identified
    if len(ko_list) >= 2:
        if align == "DIRECT_MATCH":
            match_score = min(match_score, 52)
            ip = min(ip, 28)
        else:
            match_score = min(match_score, 45)
            ip = min(ip, 22)
    elif len(ko_list) == 1:
        if align == "DIRECT_MATCH":
            match_score = min(match_score, 62)
            ip = min(ip, 35)
        else:
            match_score = min(match_score, 55)
            ip = min(ip, 30)

    # Realistic recruiting bounds by tier
    if align == "DIRECT_MATCH":
        if ko_list:
            if len(ko_list) >= 2:
                match_score = max(25, min(52, match_score))
                ip = max(10, min(28, ip))
            else:
                match_score = max(25, min(62, match_score))
                ip = max(10, min(35, ip))
        else:
            match_score = max(65, min(95, match_score))
            ip = max(55, min(92, ip))
    elif align == "ADJACENT_DOMAIN":
        if ko_list:
            match_score = max(20, min(55, match_score))
            ip = max(10, min(30, ip))
        else:
            match_score = max(45, min(75, match_score))
            ip = max(30, min(65, ip))
    else:
        match_score = max(10, min(95, match_score))
        ip = max(5, min(90, ip))

    return match_score, ip


def _has_distinctive_match(phrase: str, text_lower: str) -> bool:
    """Return True if phrase or its distinctive domain words appear in text_lower."""
    if not phrase or not text_lower:
        return False
    p_lower = phrase.strip().lower()
    if p_lower in text_lower:
        return True

    clean_paren = re.sub(r"\(.*?\)", "", p_lower).strip()
    if clean_paren and clean_paren in text_lower:
        return True

    has_credential_word = any(
        re.search(r"\b" + re.escape(w) + r"\b", p_lower) for w in CREDENTIAL_INDICATORS
    )

    tokens = [w for w in re.findall(r"\b[a-zA-Z0-9\+\#\.]+\b", p_lower) if len(w) >= 3 and w not in COMMON_DOMAIN_STOPWORDS]
    if not tokens:
        tokens = [w for w in re.findall(r"\b[a-zA-Z0-9\+\#\.]+\b", p_lower) if len(w) >= 2 and w not in COMMON_DOMAIN_STOPWORDS]
    if not tokens:
        return p_lower in text_lower

    if len(tokens) == 1:
        # Guard: If phrase had multiple words and contains a credential indicator (e.g. 'Software Engineering Certification'),
        # matching solely on 'certification' or a lone stopword residue is invalid.
        all_words = [w for w in re.findall(r"\b[a-zA-Z0-9\+\#\.]+\b", p_lower) if len(w) >= 3]
        if len(all_words) >= 2 and (has_credential_word or tokens[0] in CREDENTIAL_INDICATORS):
            subject_words = [w for w in all_words if w not in CREDENTIAL_INDICATORS and len(w) >= 3]
            if subject_words:
                return all(_token_matches(w, text_lower) for w in subject_words) and any(_token_matches(w, text_lower) for w in all_words if w in CREDENTIAL_INDICATORS)
            return p_lower in text_lower
        return _token_matches(tokens[0], text_lower)

    # Multi-token phrase: all distinctive domain tokens must be present in the text (exact or morphological stem)
    # (prevents a generic token like 'integration' from falsely matching 'Hardware-Specific Firmware Integration')
    if has_credential_word:
        all_words = [w for w in re.findall(r"\b[a-zA-Z0-9\+\#\.]+\b", p_lower) if len(w) >= 3]
        subject_words = [w for w in all_words if w not in CREDENTIAL_INDICATORS and len(w) >= 3]
        if subject_words and not all(_token_matches(w, text_lower) for w in subject_words):
            return False

    return all(_token_matches(t, text_lower) for t in tokens)


def reconcile_skills_with_ground_truth(
    parsed: Dict[str, Any],
    resume_text: str,
    job_description: str = "",
    job_title: str = "",
    is_frontier: bool = False,
) -> Dict[str, Any]:
    """
    Reconcile and ground parsed diagnostics against candidate resume and job description.
    Works universally across ANY industry (Tech, Healthcare, Finance, Construction, Legal, Sales).
    
    Tiered Behavior:
    - is_frontier=True (Cloud Frontier LLMs):
      Preserves the LLM's superior semantic determinations (does not second-guess or promote
      explicit knockout/stretch gaps into core competencies via fuzzy regex). Performs clean
      deduplication, synchronizes qualification gaps, and enforces deterministic scoring rubrics.
    - is_frontier=False (Local SLMs):
      Applies full heuristic guardrails: false-negative keyword recovery, hallucination pruning,
      cross-industry misalignment corrections, and fit summary synthesis.
    """
    if not parsed or not isinstance(parsed, dict):
        return parsed

    r_lower = _sanitize_resume_for_skills(resume_text or "").lower()
    jd_lower = (job_description or "").lower()
    has_leadership = _has_verified_people_leadership(resume_text)

    raw_core = list(parsed.get("core_competencies") or [])
    raw_stretch = list(parsed.get("stretch_skills") or [])

    reconciled_core: list[str] = []
    reconciled_stretch: list[str] = []
    demoted_to_stretch = False

    # Step 1: Process stretch skills
    if is_frontier:
        # Trust Frontier LLM's semantic gap identification; do not promote gaps to core via regex
        for s in raw_stretch:
            s_clean = clean_skill_string(s)
            if is_valid_competency(s_clean) and s_clean not in reconciled_stretch:
                reconciled_stretch.append(s_clean)
    else:
        for s in raw_stretch:
            s_clean = clean_skill_string(s)
            if not is_valid_competency(s_clean):
                continue

            # Guard: Never recover hardware/firmware skills into core if candidate resume has zero hardware background
            s_lower = s_clean.lower()
            is_hw_skill = any(kw in s_lower for kw in HARDWARE_EMBEDDED_KEYWORDS)
            if is_hw_skill and not any(kw in r_lower for kw in HARDWARE_EMBEDDED_KEYWORDS):
                if s_clean not in reconciled_stretch:
                    reconciled_stretch.append(s_clean)
                continue

            if r_lower and _has_distinctive_match(s_clean, r_lower):
                if s_clean not in reconciled_core:
                    reconciled_core.append(s_clean)
            else:
                if s_clean not in reconciled_stretch:
                    reconciled_stretch.append(s_clean)

    # Step 2: Process core competencies
    if is_frontier:
        # Preserve high-order synthesis capabilities from Frontier LLM
        for c in raw_core:
            c_clean = clean_skill_string(c)
            if is_valid_competency(c_clean) and c_clean not in reconciled_core:
                reconciled_core.append(c_clean)
    else:
        for c in raw_core:
            c_clean = clean_skill_string(c)
            if not is_valid_competency(c_clean):
                continue

            # Guard: Do not demote leadership/talent development if candidate has verified organizational people leadership
            is_leadership_skill = any(pat in c_clean.lower() for pat in PEOPLE_LEADERSHIP_PATTERNS)
            if is_leadership_skill and has_leadership:
                if c_clean not in reconciled_core:
                    reconciled_core.append(c_clean)
                continue

            if r_lower and not _has_distinctive_match(c_clean, r_lower):
                # Unsubstantiated in resume! Demote if required by job
                if jd_lower and _has_distinctive_match(c_clean, jd_lower):
                    if c_clean not in reconciled_stretch:
                        reconciled_stretch.append(c_clean)
                        demoted_to_stretch = True
            else:
                if c_clean not in reconciled_core:
                    reconciled_core.append(c_clean)

    # Step 3: Collision filter on stretch skills
    final_stretch: list[str] = []
    for s in reconciled_stretch:
        if (
            not is_skill_covered_by_core(s, reconciled_core)
            and not is_duplicate_or_subsumed(s, final_stretch)
        ):
            final_stretch.append(s)

    # Reconcile knockout_gaps and secondary_gaps
    raw_ko = parsed.get("knockout_gaps") or (
        parsed.get("qualification_gaps", {}).get("knockout_gaps")
        if isinstance(parsed.get("qualification_gaps"), dict)
        else []
    ) or []
    raw_sec = parsed.get("secondary_gaps") or (
        parsed.get("qualification_gaps", {}).get("secondary_gaps")
        if isinstance(parsed.get("qualification_gaps"), dict)
        else []
    ) or []

    # Cloud Parity Reclassification:
    # If candidate has verified cloud architecture in resume (e.g., GCP/Azure), an alternative
    # public cloud platform delta (e.g. AWS) is a secondary platform delta, not a fatal knockout.
    has_cloud_background = _has_verified_cloud_background(resume_text)
    adjusted_ko = []
    adjusted_sec = list(raw_sec)
    for k in raw_ko:
        k_clean = clean_skill_string(k)
        if has_cloud_background and _is_cloud_platform_gap(k_clean):
            if k_clean not in adjusted_sec:
                adjusted_sec.append(k_clean)
        else:
            adjusted_ko.append(k_clean)
    raw_ko = adjusted_ko
    raw_sec = adjusted_sec

    final_ko: list[str] = []
    for k in raw_ko:
        k_clean = clean_skill_string(k)
        if (
            is_valid_competency(k_clean)
            and not is_skill_covered_by_core(k_clean, reconciled_core)
            and not is_duplicate_or_subsumed(k_clean, final_ko)
        ):
            final_ko.append(k_clean)

    final_sec: list[str] = []
    for s in raw_sec:
        s_clean = clean_skill_string(s)
        if (
            is_valid_competency(s_clean)
            and not is_skill_covered_by_core(s_clean, reconciled_core)
            and not is_duplicate_or_subsumed(s_clean, final_ko)
            and not is_duplicate_or_subsumed(s_clean, final_sec)
        ):
            final_sec.append(s_clean)

    parsed["core_competencies"] = reconciled_core[:10]
    parsed["stretch_skills"] = final_stretch[:8]
    parsed["knockout_gaps"] = final_ko[:5]
    parsed["secondary_gaps"] = final_sec[:5]
    parsed["qualification_gaps"] = {
        "knockout_gaps": final_ko[:5],
        "secondary_gaps": final_sec[:5],
    }

    if not is_frontier:
        # Step 4: Re-anchor score if a primary required qualification was missing and hallucinated (SLM only)
        orig_score = parsed.get("match_score", 0)
        if demoted_to_stretch and orig_score >= 80:
            penalty = 18 if len(final_stretch) >= 2 else 12
            parsed["match_score"] = max(45, orig_score - penalty)

        # Step 4b: Hardware / Firmware cross-industry domain check (SLM only)
        title_lower = (job_title or "").lower()
        has_hw_jd = any(kw in jd_lower for kw in HARDWARE_EMBEDDED_KEYWORDS) or any(
            any(kw in s.lower() for kw in HARDWARE_EMBEDDED_KEYWORDS) for s in final_stretch
        )
        has_hw_company = any(c in title_lower or c in jd_lower for c in HARDWARE_SYSTEMS_COMPANIES)
        has_hw_resume = any(kw in r_lower for kw in HARDWARE_EMBEDDED_KEYWORDS)
        if has_hw_jd and not has_hw_resume:
            parsed["domain_alignment"] = "CROSS_INDUSTRY_MISMATCH"
            parsed["match_score"] = min(parsed.get("match_score", 35), 40)
            parsed["interview_probability"] = min(parsed.get("interview_probability", 18), 20)
            if final_stretch and reconciled_core:
                parsed["fit_summary"] = (
                    f"Transferable background in {reconciled_core[0]}, but lacks required domain background in {final_stretch[0]}."
                )
        elif has_hw_company and not has_hw_resume and parsed.get("domain_alignment") == "DIRECT_MATCH":
            parsed["domain_alignment"] = "ADJACENT_DOMAIN"

        # Step 4d: Cross-sector misclassification guard (Supply Chain vs. Banking/FinTech)
        has_sc_jd = any(kw in title_lower or kw in jd_lower for kw in SUPPLY_CHAIN_LOGISTICS_KEYWORDS)
        has_sc_resume = any(kw in r_lower for kw in SUPPLY_CHAIN_LOGISTICS_KEYWORDS)
        has_fin_resume = any(kw in r_lower for kw in FINTECH_BANKING_KEYWORDS)
        if has_sc_jd and not has_sc_resume and has_fin_resume and parsed.get("domain_alignment") == "DIRECT_MATCH":
            parsed["domain_alignment"] = "ADJACENT_DOMAIN"

        # Step 4c: Guard against SLM falsely tagging Enterprise Software / AI SaaS as CROSS_INDUSTRY_MISMATCH
        elif parsed.get("domain_alignment") == "CROSS_INDUSTRY_MISMATCH":
            is_true_cross_industry = (
                any(kw in jd_lower for kw in HARDWARE_EMBEDDED_KEYWORDS)
                or any(kw in jd_lower for kw in CLINICAL_HEALTHCARE_KEYWORDS)
                or any(any(kw in s.lower() for kw in HARDWARE_EMBEDDED_KEYWORDS) for s in final_stretch)
                or any(any(kw in s.lower() for kw in CLINICAL_HEALTHCARE_KEYWORDS) for s in final_stretch)
            )
            if not is_true_cross_industry and len(reconciled_core) >= 3:
                sen_align = parsed.get("seniority_alignment", "MATCH")
                if sen_align == "OVERQUALIFIED":
                    sen_align = "MATCH"
                    parsed["seniority_alignment"] = "MATCH"

                has_ai_core = any("ai" in c.lower() or "llm" in c.lower() or "cloud" in c.lower() for c in reconciled_core)
                has_ai_jd = "ai" in jd_lower or "agent" in jd_lower or "cloud" in jd_lower
                target_align = "DIRECT_MATCH" if (has_ai_core and has_ai_jd and len(reconciled_core) >= 4) else "ADJACENT_DOMAIN"
                parsed["domain_alignment"] = target_align
                cat_m, cat_ip = calculate_categorical_scores(target_align, sen_align, len(reconciled_core), len(final_stretch))
                parsed["match_score"] = cat_m
                parsed["interview_probability"] = cat_ip

        # Step 5: Update fit summary if core/stretch changed significantly (SLM only)
        if demoted_to_stretch and final_stretch and reconciled_core and not (has_hw_jd and not has_hw_resume):
            top_core = ", ".join(reconciled_core[:2])
            top_gap = final_stretch[0]
            parsed["fit_summary"] = f"Strong background in {top_core}, with an addressable gap in {top_gap}."

    # Final step: calculate contextual scores and maintain dual-scoring telemetry
    raw_llm_m = parsed.get("match_score")
    raw_llm_ip = parsed.get("interview_probability")

    cat_align = parsed.get("domain_alignment")
    sen_align = parsed.get("seniority_alignment") or "MATCH"

    if cat_align in ("DIRECT_MATCH", "ADJACENT_DOMAIN", "CROSS_INDUSTRY_MISMATCH"):
        det_m, det_ip = calculate_categorical_scores(
            cat_align,
            sen_align,
            core_count=len(reconciled_core),
            stretch_count=len(final_stretch),
            stretch_skills=final_stretch,
            knockout_gaps=parsed.get("knockout_gaps"),
            secondary_gaps=parsed.get("secondary_gaps"),
            job_title=job_title,
            job_description=job_description,
        )
    else:
        # Legacy/heuristic fallback when domain_alignment was not provided in parsed payload
        det_m = parsed.get("match_score", 75)
        det_ip = parsed.get("interview_probability")
        if det_ip is None:
            det_ip = calibrate_interview_probability(
                det_m,
                reconciled_core,
                final_stretch,
                job_title=job_title,
                resume_text=resume_text,
                raw_interview_probability=None,
            )

    parsed["deterministic_match_score"] = det_m
    parsed["deterministic_interview_probability"] = det_ip

    if is_frontier and raw_llm_m is not None:
        # Frontier Cloud LLM: Keep LLM score as primary, compare with deterministic baseline
        try:
            m_val = int(round(float(str(raw_llm_m).strip().rstrip("%"))))
            m_val = max(0, min(100, m_val))
        except (ValueError, TypeError):
            m_val = det_m

        if raw_llm_ip is not None:
            try:
                ip_val = int(round(float(str(raw_llm_ip).strip().rstrip("%"))))
                ip_val = max(0, min(100, ip_val))
            except (ValueError, TypeError):
                ip_val = det_ip
        else:
            ip_val = det_ip

        # Enforce safety ceiling if true cross-industry mismatch
        if cat_align == "CROSS_INDUSTRY_MISMATCH":
            m_val = min(m_val, 42)
            ip_val = min(ip_val, 25)

        parsed["match_score"] = m_val
        parsed["interview_probability"] = ip_val
        parsed["llm_match_score"] = m_val
        parsed["llm_interview_probability"] = ip_val
        parsed["score_delta_match"] = m_val - det_m
        parsed["score_delta_interview"] = ip_val - det_ip
    else:
        # Local SLM (or payloads without raw LLM scores): enforce deterministic rubric as canonical
        parsed["match_score"] = det_m
        parsed["interview_probability"] = det_ip
        parsed["llm_match_score"] = None
        parsed["llm_interview_probability"] = None
        parsed["score_delta_match"] = 0
        parsed["score_delta_interview"] = 0

    return parsed


def calibrate_interview_probability(
    match_score: int,
    core_competencies: list[str],
    stretch_skills: list[str],
    job_title: str = "",
    resume_text: str = "",
    raw_interview_probability: int | float | str | None = None,
    is_frontier: bool = False,
) -> int:
    """
    Calibrate realistic screening interview callback probability (0-100) distinct from raw technical match.
    
    Factors considered:
    1. Explicit distinct LLM estimate: If the model provided a distinct probability (and not just an echo
       of match_score), normalize decimals/percentages and preserve it within realistic recruiting bounds.
    2. Base technical requirements coverage (match_score).
    3. Stretch gaps penalty: ratio of missing requirements dampens callback probability.
    4. Seniority level alignment between job title and candidate career level in resume.
    5. Realistic recruiting bounds (even great fits rarely exceed 85-90% in competitive tech hiring).
    """
    # Normalize raw_interview_probability if provided (handle floats / decimals <= 1.0 like 0.92 -> 92)
    norm_raw_ip = None
    if raw_interview_probability is not None:
        try:
            clean_ip = str(raw_interview_probability).strip().rstrip("%").strip()
            f_val = float(clean_ip)
            if 0.0 < f_val <= 1.0:
                f_val = f_val * 100.0
            if 0 <= f_val <= 100:
                norm_raw_ip = int(round(f_val))
        except (ValueError, TypeError):
            norm_raw_ip = None

    if norm_raw_ip is not None:
        if match_score <= 42:
            return max(5, min(25, norm_raw_ip))
        if norm_raw_ip == match_score and not is_frontier:
            # Echo fallback for SLM: force realistic delta between match and callback odds
            pass
        else:
            return max(5, min(92, norm_raw_ip))

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
        if gap_ratio > 0.25:
            gap_penalty = int((gap_ratio - 0.2) * 28)

    base_estimate = norm_raw_ip if norm_raw_ip is not None else match_score
    base = base_estimate + sen_adj - gap_penalty
    if base_estimate >= 80 and sen_adj == 0 and gap_penalty == 0:
        calibrated = min(85, base_estimate - 6)
    elif base_estimate < 40:
        calibrated = min(base, int(base_estimate * 0.5))
    else:
        calibrated = base

    if calibrated == match_score:
        calibrated = max(5, match_score - 7)
    return max(5, min(90, int(calibrated)))


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
        return f"cockpit_llm_skill_radar_{u_tag}_{r_tag}_{job_id}"

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
        is_manual_invocation: bool = False,
        only_local: Optional[bool] = None,
        target_tier: Optional[str] = None,
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
            if not cached:
                u_tag = str(getattr(user, "id", None)) if user else "anon"
                r_tag = str(resume_id) if resume_id else "none"
                cached = cache.get(f"cockpit_ollama_skill_radar_{u_tag}_{r_tag}_{job_id}")
            if (
                cached
                and isinstance(cached, dict)
                and cached.get("source") != "error"
                and (cached.get("core_competencies") or cached.get("match_score") is not None)
            ):
                cached_res = dict(cached)
                cached_res["source"] = "cached"
                return cached_res

        # Skill Radar execution for pipeline vetting & Career Cockpit diagnostics:
        # - On-demand manual clicks ("Run Deep Analysis") -> Cloud LLM for fast interactive speed
        # - Automated pipeline jobs (auto-calculations in tasks.py) -> Local SLM to save cloud token costs
        from .prompt_store import get_system_prompt_profile, resolve_prompt_parts

        if user:
            try:
                profile = get_system_prompt_profile()
                sys_tmpl, usr_tmpl, leg_tmpl = resolve_prompt_parts(profile, "skill_radar")

                from django.conf import settings

                title = getattr(job_listing, "title", "Role") or "Role"
                raw_desc = (job_description or getattr(job_listing, "description", None) or getattr(job_listing, "snippet", "") or "").strip()
                max_jd_chars = int(getattr(settings, "SKILL_RADAR_JD_MAX_CHARS", 3500))
                max_resume_chars = int(getattr(settings, "SKILL_RADAR_RESUME_MAX_CHARS", 4000))

                # Strip EEO, benefits, and HR fluff before LLM ingestion to reduce token load and latency
                from .jd_cleanser import JDCleanserService
                desc = JDCleanserService.cleanse_heuristically(raw_desc, title=title, max_chars=max_jd_chars)
                if not desc:
                    desc = raw_desc[:max_jd_chars]
                desc_slice = desc.strip()
                resume_slice = (resume_text or "")[:max_resume_chars].strip()

                is_stub_desc = len(desc_slice) < 160
                if is_stub_desc:
                    company = getattr(job_listing, "company_name", None) or getattr(job_listing, "company", "") or ""
                    company_ctx = f" at {company}" if company else ""
                    notice = (
                        f"\n(Note: Full job description text was not provided for this posting. "
                        f"Evaluate baseline viability strictly from the role title '{title}'{company_ctx}. "
                        f"Do not fabricate unmentioned low-level tools or assume unverified stack alignment.)"
                    )
                    desc_slice = (desc_slice + notice).strip() if desc_slice else notice.strip()

                def _safe_format(template: str, **kwargs) -> str:
                    known = set(kwargs.keys())
                    escaped = template.replace("{", "{{").replace("}", "}}")
                    for name in known:
                        escaped = escaped.replace("{{" + name + "}}", "{" + name + "}")
                    return escaped.format(**kwargs)

                fmt_kwargs = dict(
                    job_title=title,
                    job_description=desc_slice,
                    resume_text=resume_slice,
                )

                # Ensure static resume tokens precede dynamic job description tokens for Ollama KV caching
                def _ensure_kv_cache_prefix_order(t: str) -> str:
                    if not t or "{resume_text}" not in t or "{job_description}" not in t:
                        return t
                    if t.find("{job_description}") < t.find("{resume_text}"):
                        return f"<CANDIDATE_RESUME>\n{{resume_text}}\n</CANDIDATE_RESUME>\n\n" + t.replace("{resume_text}", "[See candidate resume above]")
                    return t

                if leg_tmpl:
                    tmpl = _ensure_kv_cache_prefix_order(leg_tmpl)
                    prompt_text = _safe_format(tmpl, **fmt_kwargs)
                    messages = [HumanMessage(content=prompt_text)]
                else:
                    tmpl = _ensure_kv_cache_prefix_order(usr_tmpl)
                    sys_rendered = _safe_format(sys_tmpl, **fmt_kwargs)
                    usr_rendered = _safe_format(tmpl, **fmt_kwargs)
                    messages = [
                        SystemMessage(content=sys_rendered),
                        HumanMessage(content=usr_rendered),
                    ]

                from .llm import (
                    USAGE_QUERY_SKILL_RADAR,
                    MODEL_TIER_SLM,
                    MODEL_TIER_FRONTIER,
                )

                # Route through LLM Gateway with requested or default tier:
                # - "frontier" / "cloud": Evaluates using Cloud Frontier LLMs (Claude, GPT-4o) for maximum depth
                # - "slm" / default: Prefers Local SLM (Ollama). If offline/cooldown, falls back only to Cloud SLMs
                norm_tier = (target_tier or "").strip().lower()
                if norm_tier in ("frontier", "cloud", "llm"):
                    prefer_local = False
                    only_local = False
                    allow_local = False
                    max_model_tier = MODEL_TIER_FRONTIER
                    source_tag = "cloud_llm"
                else:
                    if only_local is None:
                        only_local = True if is_manual_invocation else False
                    prefer_local = True
                    allow_local = True
                    max_model_tier = MODEL_TIER_SLM
                    source_tag = "local_slm"

                response = invoke_llm_messages(
                    messages,
                    user=user,
                    prefer_local=prefer_local,
                    only_local=only_local,
                    allow_local=allow_local,
                    max_model_tier=max_model_tier,
                    usage_query_kind=USAGE_QUERY_SKILL_RADAR,
                )
                raw_content = response.content if hasattr(response, "content") else str(response)
                # Fallback to reasoning_content if model outputted JSON inside reasoning
                if (not raw_content or not raw_content.strip()) and hasattr(response, "additional_kwargs"):
                    raw_content = response.additional_kwargs.get("reasoning_content", "") or ""

                parsed = cls._parse_llm_response(raw_content)
                if parsed and (parsed.get("core_competencies") or parsed.get("match_score") is not None):
                    # Deterministically reconcile & ground skills against resume & JD
                    is_frontier = (source_tag == "cloud_llm") or (norm_tier in ("frontier", "cloud", "llm"))
                    parsed = reconcile_skills_with_ground_truth(
                        parsed,
                        resume_text=resume_text,
                        job_description=desc_slice,
                        job_title=title,
                        is_frontier=is_frontier,
                    )
                    parsed["source"] = source_tag
                    parsed["is_stub_description"] = is_stub_desc
                    ms = parsed.get("match_score", 0)
                    calibrated_ip = calibrate_interview_probability(
                        ms,
                        parsed.get("core_competencies", []),
                        parsed.get("stretch_skills", []),
                        job_title=title,
                        resume_text=resume_text,
                        raw_interview_probability=parsed.get("interview_probability"),
                        is_frontier=is_frontier,
                    )
                    if is_stub_desc:
                        parsed["confidence_level"] = "LOW_STUB_DESCRIPTION"
                        calibrated_ip = min(calibrated_ip, 58)
                        if "deterministic_interview_probability" in parsed and parsed["deterministic_interview_probability"] is not None:
                            parsed["deterministic_interview_probability"] = min(parsed["deterministic_interview_probability"], 58)
                    else:
                        parsed["confidence_level"] = "HIGH_VERIFIED"

                    parsed["interview_probability"] = calibrated_ip
                    if is_frontier and parsed.get("llm_match_score") is not None:
                        parsed["llm_interview_probability"] = calibrated_ip
                        det_ip_base = parsed.get("deterministic_interview_probability", calibrated_ip)
                        parsed["score_delta_interview"] = calibrated_ip - det_ip_base
                    cache.set(cache_key, parsed, CACHE_TTL_SECONDS)
                    return parsed
            except Exception as e:
                logger.warning(
                    "[SkillRadarService] Skill Radar evaluation failed (is_manual=%s, only_local=%s) for job %s: %s",
                    is_manual_invocation,
                    only_local if 'only_local' in locals() else None,
                    job_id,
                    e,
                )
                if force_refresh and is_manual_invocation:
                    return {
                        "match_score": 0,
                        "interview_probability": None,
                        "core_competencies": [],
                        "stretch_skills": [],
                        "fit_summary": f"Skill Radar analysis failed: {str(e)}",
                        "source": "error",
                        "error": str(e),
                    }

        fallback = cls._fallback_heuristic(job_listing, resume_text)
        fallback["source"] = "heuristic"
        return fallback

    @classmethod
    def _parse_llm_response(cls, text: Any) -> Optional[Dict[str, Any]]:
        """Parse JSON response from Skill Radar or Resume Judge output."""
        if not text:
            return None

        if isinstance(text, dict):
            data = text
        else:
            cleaned = str(text).strip()
            # Strip thinking tags if model emitted them (<think>...</think>)
            cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
            # Strip markdown fences if present
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)

            # Defensively detect and truncate key repetition loops after the first fit_summary
            # (e.g. SLM re-emitting "core_competencies" or other keys after fit_summary)
            fit_summary_match = re.search(
                r'("fit_summary"\s*:\s*"(?:\\.|[^"\\])*")(?:\s*,\s*"(?:core_competencies|stretch_skills|match_score|interview_probability|domain_alignment|seniority_alignment|qualification_gaps)")',
                cleaned,
                re.DOTALL,
            )
            if fit_summary_match:
                cut_pos = fit_summary_match.end(1)
                cleaned = cleaned[:cut_pos] + "\n}"

            # Strip leading commentary before first '{'
            first_brace = cleaned.find("{")
            if first_brace != -1:
                cleaned = cleaned[first_brace:]

            cleaned = re.sub(r"\s*```$", "", cleaned).strip()

            # Balance braces if truncated or stop token stripped outer closing brace(s)
            open_braces = cleaned.count("{")
            close_braces = cleaned.count("}")
            if open_braces > close_braces:
                cleaned = cleaned.rstrip().rstrip(",") + ("\n}" * (open_braces - close_braces))

            try:
                data = json.loads(cleaned)
            except Exception:
                json_match = re.search(r"(\{[\s\S]*\})", cleaned)
                if json_match:
                    try:
                        data = json.loads(json_match.group(1))
                    except Exception:
                        return None
                else:
                    return None

        if not isinstance(data, dict):
            return None

        # Unwrap nested message envelope (e.g. {"message": {"content": {...}}})
        if isinstance(data.get("message"), dict) and "content" in data["message"]:
            msg_content = data["message"]["content"]
            if isinstance(msg_content, dict):
                data = msg_content
            elif isinstance(msg_content, str):
                try:
                    parsed_inner = json.loads(msg_content)
                    if isinstance(parsed_inner, dict):
                        data = parsed_inner
                except Exception:
                    pass

        # Unwrap nested assistant content envelope (e.g. {"role": "assistant", "content": {...}})
        if "content" in data and ("core_competencies" not in data or ("match_score" not in data and "domain_alignment" not in data)):
            inner_content = data.get("content")
            if isinstance(inner_content, dict):
                data = inner_content
            elif isinstance(inner_content, str):
                try:
                    parsed_inner = json.loads(inner_content)
                    if isinstance(parsed_inner, dict):
                        data = parsed_inner
                except Exception:
                    pass

        # Check for Resume Judge multi-lens structure
        ats_eval = data.get("ats_evaluation") or {}
        rec_eval = data.get("recruiter_evaluation") or {}
        synthesis = data.get("synthesis") or {}

        domain_alignment = data.get("domain_alignment") or (rec_eval.get("domain_alignment") if rec_eval else None)
        seniority_alignment = data.get("seniority_alignment") or (rec_eval.get("seniority_alignment") if rec_eval else None)
        if isinstance(domain_alignment, str):
            domain_alignment = domain_alignment.strip().upper()
        if isinstance(seniority_alignment, str):
            seniority_alignment = seniority_alignment.strip().upper()

        if ats_eval or rec_eval or synthesis:
            raw_core = ats_eval.get("matched_keywords") or data.get("core_competencies") or []
            raw_stretch = ats_eval.get("missing_keywords") or data.get("stretch_skills") or []
            match_score = synthesis.get("composite_score") or ats_eval.get("ats_score") or data.get("match_score")
            raw_ip = rec_eval.get("recruiter_score") or synthesis.get("composite_score") or data.get("interview_probability")
            summary = str(synthesis.get("executive_summary") or rec_eval.get("seniority_alignment") or data.get("fit_summary") or "").strip()
            quant_gaps = rec_eval.get("quantification_gaps") or []
            priority_revisions = synthesis.get("priority_revisions") or []
            ats_score = ats_eval.get("ats_score")
            recruiter_score = rec_eval.get("recruiter_score")
            approved = synthesis.get("approved", False)
        else:
            raw_core = data.get("core_competencies") or []
            raw_stretch = data.get("stretch_skills") or []
            match_score = data.get("match_score")
            raw_ip = (
                data.get("interview_probability")
                or data.get("interviewProbability")
                or data.get("interview_likelihood")
            )
            summary = str(data.get("fit_summary") or "").strip()
            quant_gaps = []
            priority_revisions = []
            ats_score = None
            recruiter_score = None
            approved = False

        qual_gaps = data.get("qualification_gaps") or {}
        raw_ko = qual_gaps.get("knockout_gaps") or data.get("knockout_gaps") or []
        raw_sec = qual_gaps.get("secondary_gaps") or data.get("secondary_gaps") or []

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
                if (
                    is_valid_competency(s_clean)
                    and not is_duplicate_or_subsumed(s_clean, stretch_clean)
                    and not is_skill_covered_by_core(s_clean, core_clean)
                ):
                    stretch_clean.append(s_clean)

        ko_clean = []
        for k in raw_ko:
            if isinstance(k, str):
                k_clean = clean_skill_string(k)
                if (
                    is_valid_competency(k_clean)
                    and not is_duplicate_or_subsumed(k_clean, ko_clean)
                    and not is_skill_covered_by_core(k_clean, core_clean)
                ):
                    ko_clean.append(k_clean)

        sec_clean = []
        for s in raw_sec:
            if isinstance(s, str):
                s_clean = clean_skill_string(s)
                if (
                    is_valid_competency(s_clean)
                    and not is_duplicate_or_subsumed(s_clean, sec_clean)
                    and not is_duplicate_or_subsumed(s_clean, ko_clean)
                    and not is_skill_covered_by_core(s_clean, core_clean)
                ):
                    sec_clean.append(s_clean)

        if not stretch_clean and (ko_clean or sec_clean):
            for item in (ko_clean + sec_clean):
                if not is_duplicate_or_subsumed(item, stretch_clean):
                    stretch_clean.append(item)

        if not core_clean and not stretch_clean and match_score is None and not domain_alignment:
            return None

        # Compute deterministic categorical scores if domain_alignment is provided
        cat_match, cat_ip = None, None
        if domain_alignment in ("DIRECT_MATCH", "ADJACENT_DOMAIN", "CROSS_INDUSTRY_MISMATCH"):
            cat_match, cat_ip = calculate_categorical_scores(
                domain_alignment,
                seniority_alignment,
                core_count=len(core_clean),
                stretch_count=len(stretch_clean),
                stretch_skills=stretch_clean,
                knockout_gaps=ko_clean if ko_clean else None,
                secondary_gaps=sec_clean if sec_clean else None,
            )

        if match_score is not None:
            try:
                clean_score = str(match_score).strip().rstrip("%").strip()
                score_f = float(clean_score)
                if 0.0 < score_f <= 1.0:
                    score_f = score_f * 100.0
                score_int = int(round(score_f))
                score_int = max(0, min(100, score_int))
            except (ValueError, TypeError):
                total = len(core_clean) + len(stretch_clean)
                score_int = round((len(core_clean) / total) * 100) if total > 0 else 75
            # If cross-industry mismatch, enforce ceiling <= 42 even if raw score was inflated
            if domain_alignment == "CROSS_INDUSTRY_MISMATCH":
                score_int = min(score_int, 42)
        elif cat_match is not None:
            score_int = cat_match
        else:
            total = len(core_clean) + len(stretch_clean)
            score_int = round((len(core_clean) / total) * 100) if total > 0 else 75

        ip_int = None
        if raw_ip is not None:
            try:
                clean_ip = str(raw_ip).strip().rstrip("%").strip()
                ip_f = float(clean_ip)
                if 0.0 < ip_f <= 1.0:
                    ip_f = ip_f * 100.0
                ip_int = max(0, min(100, int(round(ip_f))))
            except (ValueError, TypeError):
                ip_int = None
            if domain_alignment == "CROSS_INDUSTRY_MISMATCH" and ip_int is not None:
                ip_int = min(ip_int, 25)
        elif cat_ip is not None:
            ip_int = cat_ip

        result: Dict[str, Any] = {
            "match_score": score_int,
            "interview_probability": ip_int,
            "domain_alignment": domain_alignment,
            "seniority_alignment": seniority_alignment,
            "core_competencies": core_clean[:10],
            "stretch_skills": stretch_clean[:8],
            "knockout_gaps": ko_clean[:5],
            "secondary_gaps": sec_clean[:5],
            "qualification_gaps": {
                "knockout_gaps": ko_clean[:5],
                "secondary_gaps": sec_clean[:5],
            },
            "fit_summary": summary,
            "quantification_gaps": quant_gaps,
            "priority_revisions": priority_revisions,
            "ats_score": ats_score,
            "recruiter_score": recruiter_score,
            "approved": approved,
        }
        if ats_eval:
            result["ats_evaluation"] = ats_eval
        if rec_eval:
            result["recruiter_evaluation"] = rec_eval
        if synthesis:
            result["synthesis"] = synthesis

        return result

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
                if p_title not in stretch and not is_skill_covered_by_core(p_title, core):
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
            "is_stub_description": len((getattr(job_listing, "description", None) or getattr(job_listing, "snippet", "") or "").strip()) < 160,
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
