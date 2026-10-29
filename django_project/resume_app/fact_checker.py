import re
from typing import Any, Dict, List, Optional


def _extract_bullet_context(text: str, match_start: int, match_end: int) -> str:
    """Extract the bullet or sentence containing the matched substring."""
    line_start = text.rfind("\n", 0, match_start)
    line_start = 0 if line_start == -1 else line_start + 1

    line_end = text.find("\n", match_end)
    line_end = len(text) if line_end == -1 else line_end

    snippet = text[line_start:line_end].strip()
    if snippet.startswith(("- ", "* ", "• ")):
        snippet = snippet[2:].strip()
    if len(snippet) > 160:
        snippet = snippet[:157] + "..."
    return snippet


def detect_resume_discrepancies(
    source_text: str,
    optimized_text: str,
    supplemental_text: str = "",
) -> Dict[str, Any]:
    """
    Scans the tailored resume draft against the source resume and any supplemental
    accomplishments to detect factual discrepancies (hallucinated metrics, ungrounded
    percentages, fabricated team headcounts, or soft-hallucinated technologies/credentials).

    Returns a structured dictionary indicating whether discrepancies exist, along with
    a list of specific discrepancies and their surrounding context.
    """
    if not optimized_text or not optimized_text.strip():
        return {
            "has_discrepancies": False,
            "discrepancies_count": 0,
            "summary": "No draft content to evaluate.",
            "discrepancies": [],
        }

    combined_grounding = f"{source_text or ''}\n{supplemental_text or ''}".lower()
    grounding_clean = re.sub(r"\s+", " ", combined_grounding)

    discrepancies: List[Dict[str, Any]] = []
    seen_keys = set()

    # 1. Percentages (e.g. 30%, 22%, 35%, 45%, 55%)
    for m in re.finditer(r"(?<![\w])(\d+(?:\.\d+)?%)(?![\w])", optimized_text):
        pct = m.group(1)
        key = ("percentage", pct)
        if key in seen_keys:
            continue
        # Check presence in source text
        pct_variants = [pct.lower(), pct.lower().replace("%", " %"), pct.lower().replace("%", "")]
        if not any(v in grounding_clean for v in pct_variants):
            seen_keys.add(key)
            context = _extract_bullet_context(optimized_text, m.start(), m.end())
            discrepancies.append({
                "type": "metric",
                "label": "Unverified Percentage",
                "text": pct,
                "context": context,
                "severity": "high",
                "message": f"Percentage '{pct}' was not found in your original resume or supplemental notes.",
            })

    # 2. Team sizes & personnel counts (e.g. 'team of 12 engineers', 'directed 12 engineers')
    team_patterns = [
        r"(?i)\b(?:team of|managed|directed|led|leading)\s+(\d+)\s+(?:engineers|developers|architects|reports|professionals|members|analysts|people)\b",
        r"(?i)\b(\d+)\s+(?:engineers|developers|architects|direct reports)\b",
    ]
    for pattern in team_patterns:
        for m in re.finditer(pattern, optimized_text):
            full_match = m.group(0).strip()
            num = m.group(1)
            key = ("team_size", num)
            if key in seen_keys:
                continue
            # If the number is not mentioned in source grounding
            if num not in grounding_clean:
                seen_keys.add(key)
                context = _extract_bullet_context(optimized_text, m.start(), m.end())
                discrepancies.append({
                    "type": "scope",
                    "label": "Unverified Team Size",
                    "text": full_match,
                    "context": context,
                    "severity": "high",
                    "message": f"Team size '{full_match}' was not found in your original resume.",
                })

    # 3. Dollar metrics (e.g. $55MM, $1.25MM, $2.5M)
    for m in re.finditer(r"(?i)(\$\d+(?:\.\d+)?(?:\s*(?:k|m|mm|b|million|billion))?)\b", optimized_text):
        dollar_raw = m.group(1).strip()
        key = ("dollar", dollar_raw.lower().replace(" ", ""))
        if key in seen_keys:
            continue
        dollar_clean = dollar_raw.lower().replace(" ", "")
        # Check if dollar amount appears in ground truth
        if dollar_clean not in grounding_clean.replace(" ", ""):
            seen_keys.add(key)
            context = _extract_bullet_context(optimized_text, m.start(), m.end())
            discrepancies.append({
                "type": "metric",
                "label": "Unverified Dollar Metric",
                "text": dollar_raw,
                "context": context,
                "severity": "high",
                "message": f"Financial figure '{dollar_raw}' was not found in your original resume.",
            })

    # 4. Soft-Hallucination & Hedging verbs before technologies
    # (e.g. 'Evaluated AWS and GCP...', 'Familiar with TOGAF...', 'Collaborated with AWS...')
    hedge_pattern = r"(?i)\b(evaluated|exploring|explored|assessed|familiar with|collaborating with|transitioning from)\s+([A-Z][A-Za-z0-9_\-\.\+]+)"
    for m in re.finditer(hedge_pattern, optimized_text):
        verb = m.group(1).strip()
        tech = m.group(2).strip()
        key = ("hedged_tech", tech.lower())
        if key in seen_keys:
            continue
        if re.search(r"\b" + re.escape(tech.lower()) + r"\b", grounding_clean) is None:
            seen_keys.add(key)
            context = _extract_bullet_context(optimized_text, m.start(), m.end())
            discrepancies.append({
                "type": "technology",
                "label": "Hedged / Unverified Technology",
                "text": f"{verb} {tech}",
                "context": context,
                "severity": "medium",
                "message": f"Technology '{tech}' introduced with hedging verb '{verb}' was not found in your original resume.",
            })

    # 5. Tier 1 High-Risk Technologies & Certifications (Strict Red Zone)
    high_risk_techs = [
        "AWS", "GCP", "TOGAF", "Kubernetes", "Docker", "Terraform",
        "Snowflake", "Databricks", "GraphQL", "gRPC", "Kafka",
        "Redis", "Cassandra", "Hadoop", "Spark", "Golang", "Rust",
        "PMP", "CISA", "CISSP"
    ]
    for tech in high_risk_techs:
        key = ("high_risk_tech", tech.lower())
        if key in seen_keys or ("hedged_tech", tech.lower()) in seen_keys:
            continue
        # Look for whole word in optimized draft
        pattern = r"\b" + re.escape(tech) + r"\b"
        match = re.search(pattern, optimized_text, re.IGNORECASE)
        if match:
            # Check if present in source grounding
            if re.search(r"\b" + re.escape(tech.lower()) + r"\b", grounding_clean) is None:
                seen_keys.add(key)
                context = _extract_bullet_context(optimized_text, match.start(), match.end())
                discrepancies.append({
                    "type": "technology",
                    "label": "Unverified Technology / Credential",
                    "text": tech,
                    "context": context,
                    "severity": "medium",
                    "message": f"Technology or certification '{tech}' appears in the draft but was not found in your original resume.",
                })

    has_disc = len(discrepancies) > 0
    return {
        "has_discrepancies": has_disc,
        "discrepancies_count": len(discrepancies),
        "summary": (
            f"Detected {len(discrepancies)} potential discrepancy(ies) against your source resume."
            if has_disc
            else "Factual grounding verified: No unverified metrics or high-risk technologies detected."
        ),
        "discrepancies": discrepancies,
    }
