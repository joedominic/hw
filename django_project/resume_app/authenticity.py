import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from dateutil import parser as date_parser
from django.core.cache import cache
from django.utils import timezone
from langchain_core.messages import HumanMessage, SystemMessage

from .models import JobListing

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 7 * 86400  # 7 days

CHECK_POINT_MAP = {
    "perm_legal_phrasing": 40,
    "phantom_pipeline_or_talent_pool": 50,
    "short_lifespan_or_compliance_window": 35,
    "generic_jfa_template_only": 20,
    "senior_promotion_or_scope_mismatch": 25,
    "bespoke_contractor_conversion_stack": 25,
    "hyper_specific_okrs": 20,
    "rigid_degree_equation": 20,
    "flat_salary_range": 15,
    "flexible_general_req": -20,
    # Backward compatibility aliases
    "statutory_minimum_window_clause": 25,
    "title_scope_mismatch": 20,
}

JOB_AUTHENTICITY_SYSTEM_PROMPT = """You are a deterministic labor compliance auditor and talent acquisition classifier.

Your task is to audit the job description provided in the user prompt for indicators of:
1. PERM labor certification compliance (mandatory immigrant visa recruitment testing under DOL ETA-9089)
2. Pre-identified internal promotions, contractor conversions, or succession placeholders
3. Phantom / evergreen talent pool postings with no immediate funded headcount
4. Genuine external hiring competition

---

### AUDIT CHECK RULES & FIXED POINT VALUES

Evaluate each of the following 10 checks in strict sequential order:

1. perm_legal_phrasing (+40 pts if detected, else 0)
   - True ONLY if statutory DOL ETA-9089 language appears verbatim:
     * "post-baccalaureate progressive experience"
     * "in the alternative"
     * "or foreign educational equivalent"
     * Instructions to mail a physical paper resume or email a legal/immigration compliance inbox.
   - Note: If text explicitly states "This role is not eligible for visa sponsorship", this MUST be False (0 pts).

2. phantom_pipeline_or_talent_pool (+50 pts if detected, else 0)
   - True if ANY of the following apply:
     * Requisition ID or title explicitly contains tokens like "PIPE", "POOL", "TALENT", "COMMUNITY", "EVERGREEN", or "GENERIC".
     * Text explicitly contains disclaimers stating no immediate seat exists, such as: "pipeline requisition", "talent community", "future hiring needs", or "does not represent an immediate opening / funded role".
   - Otherwise: False (0 pts).

3. short_lifespan_or_compliance_window (+35 pts if detected, else 0)
   - True if ANY of the following apply:
     * The delta between Posting Date and Posting End Date / Application Deadline is 4 calendar days or fewer (e.g., posted Friday, closing Monday).
     * The text explicitly states an artificial corporate compliance minimum (e.g., "accepting applications for a minimum of 5 business days").
     * Text includes disclaimers like "may come down early due to volume" paired with a window under 5 calendar days.
   - Otherwise: False (0 pts).

4. generic_jfa_template_only (+20 pts if detected, else 0)
   - True if the responsibilities section consists ENTIRELY of generic corporate Job Family Architecture (JFA) competencies (e.g., "act as an advisor to leadership", "provide vision and direction", "maintain knowledge of industry best practices") with ZERO mention of a specific team, named business product, or bounded engineering mission.
   - If the text describes a concrete product build, customer feature, or bounded organizational scope: False (0 pts).

5. senior_promotion_or_scope_mismatch (+25 pts if detected, else 0)
   - True if ANY of the following apply:
     * The role is a high-grade individual contributor or leadership tier (P5, Principal Engineer, Sr. Director, Enterprise Architect) posted with an ultra-short window (<= 4 days), indicating an in-band promotion or incumbent conversion.
     * Title is Director/Sr. Director, but description explicitly requires division "CTO" or multi-billion P&L ownership.
   - Otherwise: False (0 pts).

6. bespoke_contractor_conversion_stack (+25 pts if detected, else 0)
   - True ONLY if the qualifications mandate an unusual, highly specific cross-domain pairing of disparate legacy + modern enterprise systems (e.g., proprietary legacy ERP/BPMN paired with modern cloud frameworks and specific third-party integration engines) or exact vendor certifications that mirror an existing contractor rather than an open market profile.
   - Otherwise: False (0 pts).

7. hyper_specific_okrs (+20 pts if detected, else 0)
   - True ONLY if responsibilities cite explicit numeric targets (e.g., "achieve 90%+ zero-touch", "$6.1B run-rate") or proprietary, non-public internal platform codenames.
   - Generic business domain names (e.g., "money movement", "inheritance journeys", "data center", "Agile execution") do NOT count and MUST be evaluated as False (0 pts).

8. rigid_degree_equation (+20 pts if detected, else 0)
   - True ONLY if an inflexible degree equation is strictly enforced without alternative experience:
     * e.g., "Master's + 3 years OR Bachelor's + 5 years" with NO phrasing like "or equivalent experience".
   - If the posting accepts multiple degrees or states "or equivalent experience": False (0 pts).

9. flat_salary_range (+15 pts if detected, else 0)
   - True ONLY if base salary is a single rigid number OR the delta between maximum and minimum base salary is <= $15,000 (e.g., "$182,400 - $182,400").
   - Normal corporate bands with spreads > $15,000: False (0 pts).

10. flexible_general_req (-20 pts if detected, else 0)
    - True ONLY if technical requirements list broad interchangeable options separated by "or" (e.g., "F5, AVI, or Palo Alto", "Java, Python, or Go", "Computer Science or equivalent experience") with NO proprietary version locks.
    - If requirements are rigid or bespoke: False (0 pts).

---

### OUTPUT FORMAT INSTRUCTIONS

Emit ONLY a single, valid JSON object matching the exact schema below. Do not wrap in markdown code fences, and do not include conversational filler or thought blocks.

{
  "audit_trail": [
    {
      "check": "perm_legal_phrasing",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    },
    {
      "check": "phantom_pipeline_or_talent_pool",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    },
    {
      "check": "short_lifespan_or_compliance_window",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    },
    {
      "check": "generic_jfa_template_only",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    },
    {
      "check": "senior_promotion_or_scope_mismatch",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    },
    {
      "check": "bespoke_contractor_conversion_stack",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    },
    {
      "check": "hyper_specific_okrs",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    },
    {
      "check": "rigid_degree_equation",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    },
    {
      "check": "flat_salary_range",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    },
    {
      "check": "flexible_general_req",
      "evidence_quote": "Exact quote from text or NONE",
      "detected": false,
      "points": 0
    }
  ],
  "scoring_calculation": {
    "arithmetic_expression": "string showing explicit sum, e.g. 0 + 0 + 35 + 20 + 25 + 0 + 0 + 0 + 0 - 20",
    "calculated_score": 60
  },
  "final_verdict": "INTERNAL_PRESELECT",
  "confidence": "HIGH",
  "primary_rationale": "Single concise sentence explaining the decision."
}

Threshold Rules:
- calculated_score: Clamp result between 0 and 100 (treat negative sums as 0).
- If perm_legal_phrasing detected == true -> final_verdict is "PERM_COMPLIANCE".
- If phantom_pipeline_or_talent_pool detected == true -> final_verdict is "PHANTOM_PIPELINE".
- If calculated_score >= 40 -> final_verdict is "INTERNAL_PRESELECT".
- If calculated_score < 40 -> final_verdict is "GENUINE_EXTERNAL"."""


class JobAuthenticityService:
    """
    Deterministic Labor Compliance & Authenticity Auditor.
    Evaluates JobListing descriptions for indicators of:
    - PERM labor certification compliance (ETA-9089)
    - Pre-identified internal promotions, contractor conversions, or succession placeholders
    - Phantom / evergreen talent pool postings
    - Genuine external hiring competition
    """

    @classmethod
    def calculate_score_and_verdict(
        cls, audit_trail: List[Dict[str, Any]]
    ) -> Tuple[int, str, str, str]:
        """
        Enforce deterministic scoring rules and mathematical consistency:
        - Points fixed per rule:
            perm_legal_phrasing: +40
            phantom_pipeline_or_talent_pool: +50
            short_lifespan_or_compliance_window: +35
            generic_jfa_template_only: +20
            senior_promotion_or_scope_mismatch: +25
            bespoke_contractor_conversion_stack: +25
            hyper_specific_okrs: +20
            rigid_degree_equation: +20
            flat_salary_range: +15
            flexible_general_req: -20
        - Score clamped to [0, 100]
        - Thresholds:
            perm_legal_phrasing detected -> PERM_COMPLIANCE
            phantom_pipeline_or_talent_pool detected -> PHANTOM_PIPELINE
            score >= 40 -> INTERNAL_PRESELECT
            score < 40 -> GENUINE_EXTERNAL
        """
        raw_sum = 0
        perm_detected = False
        phantom_detected = False
        terms = []

        for item in audit_trail:
            chk = item.get("check")
            detected = bool(item.get("detected", False))
            pts_default = CHECK_POINT_MAP.get(chk, 0)
            pts = pts_default if detected else 0
            item["points"] = pts

            if chk == "perm_legal_phrasing" and detected:
                perm_detected = True
            elif chk == "phantom_pipeline_or_talent_pool" and detected:
                phantom_detected = True

            raw_sum += pts
            terms.append(f"{pts:+d}" if terms else f"{pts}")

        arithmetic_expr = " ".join(terms) if terms else "0"
        clamped_score = max(0, min(100, raw_sum))

        if perm_detected:
            verdict = JobListing.AuthenticityVerdict.PERM_COMPLIANCE
        elif phantom_detected:
            verdict = getattr(JobListing.AuthenticityVerdict, "PHANTOM_PIPELINE", "PHANTOM_PIPELINE")
        elif clamped_score >= 40:
            verdict = JobListing.AuthenticityVerdict.INTERNAL_PRESELECT
        else:
            verdict = JobListing.AuthenticityVerdict.GENUINE_EXTERNAL

        return clamped_score, verdict, arithmetic_expr, "HIGH"

    @classmethod
    def _parse_llm_response(cls, text: str) -> Optional[Dict[str, Any]]:
        """Extract and validate clean JSON from model output."""
        if not text:
            return None

        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            data = json.loads(cleaned)
        except Exception:
            match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                except Exception:
                    return None
            else:
                return None

        if not isinstance(data, dict):
            return None

        trail = data.get("audit_trail")
        if not isinstance(trail, list) or len(trail) < 3:
            return None

        clamped_score, verdict, expr, confidence = cls.calculate_score_and_verdict(trail)

        data["audit_trail"] = trail
        data["scoring_calculation"] = {
            "arithmetic_expression": expr,
            "calculated_score": clamped_score,
        }
        data["final_verdict"] = verdict
        data["confidence"] = data.get("confidence") or confidence
        if not data.get("primary_rationale"):
            if verdict == JobListing.AuthenticityVerdict.PERM_COMPLIANCE:
                data["primary_rationale"] = "Statutory DOL ETA-9089 immigrant labor certification language detected."
            elif verdict == getattr(JobListing.AuthenticityVerdict, "PHANTOM_PIPELINE", "PHANTOM_PIPELINE"):
                data["primary_rationale"] = "Evergreen talent pool or phantom requisition with no immediate funded headcount."
            elif verdict == JobListing.AuthenticityVerdict.INTERNAL_PRESELECT:
                data["primary_rationale"] = "Rigid degree, compliance window, or internal target constraints indicate a pre-identified candidate."
            else:
                data["primary_rationale"] = "Posting demonstrates normal open-market hiring competition with flexible qualifications."

        return data

    @classmethod
    def extract_window_and_deadline(
        cls, job_listing: Any
    ) -> Tuple[str, str, bool, str, Optional[int]]:
        """
        Extracts posted_date, deadline, application window delta, and Rule 3 triggers.
        Returns:
            posted_date_str (str): Formatted YYYY-MM-DD or 'Not specified'
            window_str (str): Human-readable window description
            has_short_window (bool): True if lifespan <= 4 calendar days or statutory compliance window clause
            window_quote (str): Quote evidence from description
            calendar_days (int or None): Delta in calendar days if computable
        """
        if isinstance(job_listing, str):
            title = ""
            desc = job_listing
            posted_at = None
            raw_json = {}
        else:
            title = getattr(job_listing, "title", "") or ""
            desc = getattr(job_listing, "description", "") or ""
            posted_at = getattr(job_listing, "posted_at", None)
            raw_json = getattr(job_listing, "raw_json", None) or {}

        posted_date_str = posted_at.strftime("%Y-%m-%d") if posted_at else "Not specified"
        posted_date_obj = posted_at.date() if posted_at else None

        # Check for raw_json explicit deadline/valid_through fields first
        deadline_date_obj = None
        if isinstance(raw_json, dict):
            raw_valid_through = (
                raw_json.get("valid_through")
                or raw_json.get("validThrough")
                or raw_json.get("deadline")
                or raw_json.get("applicationDeadline")
            )
            if raw_valid_through:
                try:
                    deadline_date_obj = date_parser.parse(str(raw_valid_through)).date()
                except Exception:
                    pass

        # 1. Statutory / artificial minimum window clause in description
        statutory_m = re.search(
            r"\b(?:accepting applications for a minimum of (\d+)\s*(?:business\s*)?days|minimum of (\d+)\s*business days)\b",
            desc,
            re.IGNORECASE,
        )
        if statutory_m:
            days = statutory_m.group(1) or statutory_m.group(2)
            quote = statutory_m.group(0).strip()
            window_str = f"Statutory minimum of {days} business days ({quote})"
            return (
                posted_date_str,
                window_str,
                True,
                quote,
                int(days) if (days and days.isdigit()) else 5,
            )

        # 2. Ongoing / open-ended clauses
        ongoing_m = re.search(
            r"\b(?:applications will be accepted on an ongoing basis|open until(?: it is successfully)? filled|does not have an application deadline)\b",
            desc,
            re.IGNORECASE,
        )
        if ongoing_m:
            quote = ongoing_m.group(0).strip()
            window_str = f"Open-ended / Ongoing ('{quote}')"
            return (posted_date_str, window_str, False, "NONE", None)

        # 3. Explicit deadline date in text
        deadline_patterns = [
            r"(?:will close on|closes? on|closes? at|deadline to apply|application deadline|applications accepted until|accepting applications until|anticipated to close on|expected to close on)\s*[:\-–]?\s*([A-Za-z0-9\s,\/\.\-–]{3,28})",
        ]

        parsed_deadline_str = None
        deadline_quote = "NONE"

        for pat in deadline_patterns:
            m = re.search(pat, desc, re.IGNORECASE)
            if m:
                raw_target = m.group(1).strip()
                raw_target = re.split(r"[\n\r\.\;]", raw_target)[0].strip()
                try:
                    if len(raw_target) >= 4 and any(c.isdigit() for c in raw_target):
                        dt = date_parser.parse(raw_target, fuzzy=False)
                        deadline_date_obj = dt.date()
                        clean_quote = re.split(r"[\r\n]", m.group(0))[0].strip()
                        deadline_quote = clean_quote[:80]
                        parsed_deadline_str = deadline_date_obj.strftime("%Y-%m-%d")
                        break
                except Exception:
                    try:
                        dt = date_parser.parse(raw_target, fuzzy=True)
                        if dt.year in (2025, 2026, 2027):
                            deadline_date_obj = dt.date()
                            clean_quote = re.split(r"[\r\n]", m.group(0))[0].strip()
                            deadline_quote = clean_quote[:80]
                            parsed_deadline_str = deadline_date_obj.strftime("%Y-%m-%d")
                            break
                    except Exception:
                        pass

        if posted_date_obj and deadline_date_obj:
            delta = (deadline_date_obj - posted_date_obj).days
            if delta >= 0:
                window_str = f"{delta} calendar days (Posted: {posted_date_str}, Closes: {deadline_date_obj.strftime('%Y-%m-%d')})"
                is_short = delta <= 4
                quote = deadline_quote if is_short else "NONE"
                return (posted_date_str, window_str, is_short, quote, delta)

        if parsed_deadline_str:
            window_str = f"Closes {parsed_deadline_str} (Posted: {posted_date_str})"
            return (posted_date_str, window_str, False, "NONE", None)

        window_str = "Not specified in requisition metadata"
        return (posted_date_str, window_str, False, "NONE", None)

    @classmethod
    def _fallback_heuristic(
        cls, job_listing: Any
    ) -> Dict[str, Any]:
        """
        Deterministic regex fallback when local LLM is offline or unconfigured.
        Detects verbatim statutory DOL language or standard flexible job phrasing.
        """
        if isinstance(job_listing, str):
            title = ""
            desc = job_listing.lower()
        else:
            title = (getattr(job_listing, "title", "") or "").lower()
            desc = (getattr(job_listing, "description", "") or "").lower()

        # Extract posted_date and application window/lifespan
        posted_str, window_str, has_short_window, win_quote, _ = cls.extract_window_and_deadline(job_listing)

        perm_patterns = [
            r"post-baccalaureate progressive",
            r"in the alternative",
            r"or foreign educational equivalent",
            r"eta-9089",
            r"perm labor certification",
            r"mail resume to",
            r"mail paper resume",
        ]
        has_perm = any(re.search(p, desc) for p in perm_patterns)
        perm_quote = "NONE"
        if has_perm:
            for p in perm_patterns:
                m = re.search(p, desc)
                if m:
                    perm_quote = m.group(0)
                    break

        has_flex = bool(
            re.search(r"\b(or equivalent experience|or equivalent|computer science, engineering, or|equivalent work experience)\b", desc)
        )
        flex_quote = "or equivalent experience" if has_flex else "NONE"

        has_flat_salary = False
        salary_quote = "NONE"
        raw_json = getattr(job_listing, "raw_json", None) or {}
        min_amt = raw_json.get("min_amount") if isinstance(raw_json, dict) else None
        max_amt = raw_json.get("max_amount") if isinstance(raw_json, dict) else None
        if min_amt is not None and max_amt is not None:
            try:
                diff = abs(float(max_amt) - float(min_amt))
                if diff <= 15000:
                    has_flat_salary = True
                    salary_quote = f"${min_amt:,.0f} - ${max_amt:,.0f}"
            except (ValueError, TypeError):
                pass

        # 2. Inflexible degree equation without alternative experience
        deg_match = re.search(
            r"\b(?:master'?s\s*\+\s*\d+\s*years?|bachelor'?s\s*\+\s*\d+\s*years?)\b",
            desc
        )
        has_rigid_deg = bool(deg_match and not has_flex)
        deg_quote = deg_match.group(0) if (has_rigid_deg and deg_match) else "NONE"

        # 2. Phantom pipeline / talent pool
        phantom_match = re.search(
            r"\b(?:talent\s+community|talent\s+pool|future\s+opportunities|general\s+application|pipeline\s+(?:only|requisition)|evergreen\s+requisition|not\s+for\s+an\s+active\s+opening)\b",
            desc + " " + title,
            re.IGNORECASE,
        )
        has_phantom = bool(phantom_match)
        phantom_quote = phantom_match.group(0) if phantom_match else "NONE"

        # 3. Short lifespan or compliance window
        has_window = has_short_window
        window_evidence = win_quote if has_short_window else "NONE"

        # 4. Generic JFA template only (heuristic: check for excessive corporate boilerplate without concrete tools)
        has_jfa = False
        jfa_quote = "NONE"

        # 5. Senior promotion or scope mismatch (Director / Senior Director functioning as enterprise CTO, or senior tier with short window)
        is_director = bool(re.search(r"\b(?:director|sr\.?\s*director|senior director)\b", title, re.IGNORECASE))
        has_cto_scope = bool(re.search(r"\b(?:chief technology officer|unit cto|business[- ]unit cto|cto for)\b", desc + " " + title, re.IGNORECASE))
        is_senior_tier = bool(re.search(r"\b(?:principal|prin\b|sr\.?\s*prin|staff|sr\.?\s*principal|lead\s*director|senior\s*director|executive\s*director|enterprise\s*architect|distinguished)\b", title, re.IGNORECASE))
        has_senior_short_window = is_senior_tier and has_short_window
        has_senior_mismatch = (is_director and has_cto_scope) or has_senior_short_window
        if is_director and has_cto_scope:
            senior_mismatch_quote = "Director title functions as Business Unit CTO"
        elif has_senior_short_window:
            senior_mismatch_quote = f"{title.title() if title else 'Senior role'} posted with short compliance window ({win_quote})"
        else:
            senior_mismatch_quote = "NONE"

        # 6. Bespoke contractor conversion stack
        has_bespoke = False
        bespoke_quote = "NONE"

        # 7. Hyper-specific OKRs or proprietary internal platform targets
        okr_match = re.search(
            r"\b(?:\$\d+(?:\.\d+)?[bm]\s+run[- ]rate|\d{2,3}%\+?\s+zero[- ]touch)\b",
            desc,
            re.IGNORECASE,
        )
        has_okr = bool(okr_match)
        okr_quote = okr_match.group(0) if okr_match else "NONE"

        # 8. Inflexible degree equation without alternative experience
        deg_match = re.search(
            r"\b(?:master'?s\s*\+\s*\d+\s*years?|bachelor'?s\s*\+\s*\d+\s*years?)\b",
            desc,
            re.IGNORECASE,
        )
        has_rigid_deg = bool(deg_match and not has_flex)
        deg_quote = deg_match.group(0) if (has_rigid_deg and deg_match) else "NONE"

        # 9. Flat salary range
        # (calculated above as has_flat_salary, salary_quote)

        # 10. Flexible general requirements
        # (calculated above as has_flex, flex_quote)

        trail = [
            {
                "check": "perm_legal_phrasing",
                "evidence_quote": perm_quote,
                "detected": has_perm,
                "points": 40 if has_perm else 0,
            },
            {
                "check": "phantom_pipeline_or_talent_pool",
                "evidence_quote": phantom_quote,
                "detected": has_phantom,
                "points": 50 if has_phantom else 0,
            },
            {
                "check": "short_lifespan_or_compliance_window",
                "evidence_quote": win_quote if has_window else "NONE",
                "detected": has_window,
                "points": 35 if has_window else 0,
            },
            {
                "check": "generic_jfa_template_only",
                "evidence_quote": jfa_quote,
                "detected": has_jfa,
                "points": 20 if has_jfa else 0,
            },
            {
                "check": "senior_promotion_or_scope_mismatch",
                "evidence_quote": senior_mismatch_quote,
                "detected": has_senior_mismatch,
                "points": 25 if has_senior_mismatch else 0,
            },
            {
                "check": "bespoke_contractor_conversion_stack",
                "evidence_quote": bespoke_quote,
                "detected": has_bespoke,
                "points": 25 if has_bespoke else 0,
            },
            {
                "check": "hyper_specific_okrs",
                "evidence_quote": okr_quote,
                "detected": has_okr,
                "points": 20 if has_okr else 0,
            },
            {
                "check": "rigid_degree_equation",
                "evidence_quote": deg_quote,
                "detected": has_rigid_deg,
                "points": 20 if has_rigid_deg else 0,
            },
            {
                "check": "flat_salary_range",
                "evidence_quote": salary_quote,
                "detected": has_flat_salary,
                "points": 15 if has_flat_salary else 0,
            },
            {
                "check": "flexible_general_req",
                "evidence_quote": flex_quote,
                "detected": has_flex,
                "points": -20 if has_flex else 0,
            },
        ]

        clamped_score, verdict, expr, confidence = cls.calculate_score_and_verdict(trail)

        rationale = (
            "Heuristic detected statutory DOL ETA-9089 language."
            if verdict == JobListing.AuthenticityVerdict.PERM_COMPLIANCE
            else "Standard external job posting heuristics evaluated."
        )

        return {
            "audit_trail": trail,
            "authenticity_audit_trail": trail,
            "scoring_calculation": {
                "arithmetic_expression": expr,
                "calculated_score": clamped_score,
            },
            "calculated_score": clamped_score,
            "authenticity_score": clamped_score,
            "final_verdict": verdict,
            "authenticity_verdict": verdict,
            "confidence": "MEDIUM",
            "authenticity_confidence": "MEDIUM",
            "primary_rationale": rationale,
            "authenticity_rationale": rationale,
            "source": "heuristic",
        }

    @classmethod
    def audit(
        cls,
        job_listing: JobListing,
        *,
        user=None,
        force_refresh: bool = False,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        """
        Audit a JobListing for labor compliance and posting authenticity.
        Caches in Redis and persists to JobListing.
        """
        cache_key = f"job_authenticity_{job_listing.id}"

        # 1. Check existing DB persistence unless force_refresh
        if (
            not force_refresh
            and job_listing.authenticity_audited_at
            and job_listing.authenticity_verdict
        ):
            res = {
                "audit_trail": job_listing.authenticity_audit_trail or [],
                "authenticity_audit_trail": job_listing.authenticity_audit_trail or [],
                "scoring_calculation": {
                    "calculated_score": job_listing.authenticity_score or 0,
                    "arithmetic_expression": "",
                },
                "calculated_score": job_listing.authenticity_score or 0,
                "authenticity_score": job_listing.authenticity_score or 0,
                "final_verdict": job_listing.authenticity_verdict,
                "authenticity_verdict": job_listing.authenticity_verdict,
                "confidence": job_listing.authenticity_confidence or "HIGH",
                "authenticity_confidence": job_listing.authenticity_confidence or "HIGH",
                "primary_rationale": job_listing.authenticity_rationale or "",
                "authenticity_rationale": job_listing.authenticity_rationale or "",
                "source": "persisted",
            }
            cache.set(cache_key, res, CACHE_TTL_SECONDS)
            return res

        # 2. Check Redis cache
        if not force_refresh:
            cached = cache.get(cache_key)
            if cached and isinstance(cached, dict) and cached.get("final_verdict"):
                cached_res = dict(cached)
                cached_res["source"] = "cached"
                return cached_res

        # 3. Local LLM Evaluation
        from .llm.gateway import local_llm_available, invoke_llm_messages

        parsed = None
        if use_llm and user and local_llm_available(user):
            try:
                title = (job_listing.title or "").strip()
                company = (job_listing.company_name or "").strip()
                loc = (job_listing.location or "").strip()
                desc = (job_listing.description or "").strip()

                from .prompt_store import (
                    get_system_prompt_profile,
                    resolve_prompt_parts,
                    inject_variables_into_template,
                )
                from .prompts import (
                    DEFAULT_JOB_AUTHENTICITY_SYSTEM,
                    DEFAULT_JOB_AUTHENTICITY_USER,
                )

                try:
                    profile = get_system_prompt_profile()
                    sys_tmpl, usr_tmpl, _ = resolve_prompt_parts(profile, "job_authenticity")
                except Exception:
                    sys_tmpl, usr_tmpl = DEFAULT_JOB_AUTHENTICITY_SYSTEM, DEFAULT_JOB_AUTHENTICITY_USER

                posted_date_str, window_str, _, _, _ = cls.extract_window_and_deadline(job_listing)

                sys_content = sys_tmpl or DEFAULT_JOB_AUTHENTICITY_SYSTEM
                usr_content = inject_variables_into_template(
                    usr_tmpl or DEFAULT_JOB_AUTHENTICITY_USER,
                    {
                        "title": title,
                        "company": company,
                        "location": loc,
                        "posted_date": posted_date_str,
                        "application_window": window_str,
                        "job_description": desc[:6500],
                    },
                )

                messages = [
                    SystemMessage(content=sys_content),
                    HumanMessage(content=usr_content),
                ]

                response = invoke_llm_messages(
                    messages,
                    user=user,
                    prefer_local=True,
                    only_local=True,
                    allow_local=True,
                    usage_query_kind="job_authenticity",
                )
                raw_content = response.content if hasattr(response, "content") else str(response)
                parsed = cls._parse_llm_response(raw_content)
                if parsed:
                    parsed["source"] = "ollama_local"
            except Exception as e:
                logger.warning(
                    "[JobAuthenticityService] Ollama Local audit failed for job %s: %s",
                    job_listing.id,
                    e,
                )
                parsed = None

        # 4. Fallback if LLM unavailable or parsing failed
        if not parsed:
            parsed = cls._fallback_heuristic(job_listing)

        verdict = parsed.get("final_verdict") or JobListing.AuthenticityVerdict.GENUINE_EXTERNAL
        score = parsed.get("scoring_calculation", {}).get("calculated_score", 0)
        conf = parsed.get("confidence") or "HIGH"
        rationale = parsed.get("primary_rationale") or ""
        trail = parsed.get("audit_trail") or []

        parsed["authenticity_verdict"] = verdict
        parsed["authenticity_score"] = score
        parsed["calculated_score"] = score
        parsed["authenticity_confidence"] = conf
        parsed["authenticity_rationale"] = rationale
        parsed["authenticity_audit_trail"] = trail

        # 5. Persist to JobListing database record
        try:
            job_listing.authenticity_verdict = verdict
            job_listing.authenticity_score = score
            job_listing.authenticity_confidence = conf
            job_listing.authenticity_rationale = rationale
            job_listing.authenticity_audit_trail = trail
            job_listing.authenticity_audited_at = timezone.now()
            job_listing.save(
                update_fields=[
                    "authenticity_verdict",
                    "authenticity_score",
                    "authenticity_confidence",
                    "authenticity_rationale",
                    "authenticity_audit_trail",
                    "authenticity_audited_at",
                ]
            )
        except Exception as e:
            logger.exception(
                "[JobAuthenticityService] Failed to persist authenticity to job %s: %s",
                job_listing.id,
                e,
            )

        # 6. Cache in Redis
        cache.set(cache_key, parsed, CACHE_TTL_SECONDS)
        return parsed
