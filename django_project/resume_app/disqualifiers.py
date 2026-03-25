"""
Disqualifier phrase suggestion from job descriptions.
Used when user dislikes a job: suggest phrases they can add to their blocklist.
"""
import re
from typing import List, Optional

from .models import UserDisqualifier, JobListing


def suggest_phrases(description: str, max_phrases: int = 15) -> List[str]:
    """
    Extract candidate phrases from a job description for the user to add as disqualifiers.
    Returns short, meaningful snippets: bullet lines, requirement-style sentences, and key n-grams.
    """
    if not description or not description.strip():
        return []
    text = description.strip()
    # Split into lines and clean
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    seen = set()
    out: List[str] = []

    def add(s: str) -> None:
        s = _normalize(s)
        if not s or len(s) < 3:
            return
        if len(s) > 200:  # too long; take first 200
            s = s[:197].rsplit(" ", 1)[0] + "..."
        if s not in seen:
            seen.add(s)
            out.append(s)

    # Bullet-style lines (• - * numbers)
    bullet_re = re.compile(r"^[\s]*[•\-*]\s+", re.MULTILINE)
    for line in lines:
        clean = re.sub(bullet_re, "", line).strip()
        if 10 <= len(clean) <= 200:
            add(clean)
        elif len(clean) > 200:
            # First sentence or first 120 chars
            first = clean.split(".")[0].strip() + "." if "." in clean else clean[:120]
            if len(first) >= 10:
                add(first)

    # Numbered requirements
    for line in lines:
        m = re.match(r"^\d+[.)]\s+(.+)", line)
        if m:
            clean = m.group(1).strip()
            if 10 <= len(clean) <= 200:
                add(clean)

    # Requirement-style sentences (must, required, no, will not)
    req_re = re.compile(
        r"(?:^|[.]\s+)([^.]*(?:must|required|no\s+\w+|will not|cannot|need to)[^.]*\.?)",
        re.IGNORECASE,
    )
    for m in req_re.finditer(text):
        s = m.group(1).strip()
        if 15 <= len(s) <= 200:
            add(s)

    # Dedupe and limit
    result = []
    for s in out:
        if s not in result:
            result.append(s)
        if len(result) >= max_phrases:
            break
    return result[:max_phrases]


def _normalize(s: str) -> str:
    return " ".join(s.split()).strip()


def get_disqualifier_phrases() -> List[str]:
    """Return list of user disqualifier phrases (first filter: exclude jobs containing any)."""
    try:
        return list(UserDisqualifier.objects.values_list("phrase", flat=True))
    except Exception:
        return []


def build_disqualifier_pattern(phrases: List[str]):
    """Combine all phrases into one compiled regex for O(1) search per job. Returns None if no valid phrases or on error."""
    if not phrases:
        return None
    try:
        escaped = [re.escape(p.strip()) for p in phrases if (p or "").strip()]
        if not escaped:
            return None
        pattern = re.compile(rf"\b(?:{'|'.join(escaped)})\b", re.IGNORECASE)
        return pattern
    except (re.error, ValueError, TypeError):
        return None


def job_matches_disqualifiers(job: JobListing, pattern: Optional[re.Pattern]) -> bool:
    """Return True if job description matches the combined disqualifier regex (whole-word, case-insensitive)."""
    if pattern is None:
        return False
    text = (getattr(job, "description", None) or "") or ""
    return bool(pattern.search(text))
