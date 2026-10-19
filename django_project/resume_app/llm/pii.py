import re
from typing import Any

# High-precision regexes for common Personally Identifiable Information (PII)
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)
_LABELED_PHONE_RE = re.compile(
    r"(?i)\b((?:phone|tel|mobile|cell|fax)(?:[^\S\r\n]*(?:number|no|#))?[^\S\r\n]*[:=][^\S\r\n]*)(\+?[\d\s\(\)\-\.]{7,20}\b)"
)
_SSN_RE = re.compile(
    r"\b\d{3}-\d{2}-\d{4}\b"
)
_LINKEDIN_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_-]+/?",
    re.IGNORECASE,
)
_GITHUB_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9_-]+/?",
    re.IGNORECASE,
)
_STREET_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9\.\s\-]+(?:\bStreet\b|\bSt\b|\bAvenue\b|\bAve\b|\bBoulevard\b|\bBlvd\b|\bRoad\b|\bRd\b|\bDrive\b|\bDr\b|\bLane\b|\bLn\b|\bWay\b|\bCourt\b|\bCt\b|\bCircle\b|\bCir\b|\bPlace\b|\bPl\b|\bParkway\b|\bPkwy\b|\bTerrace\b|\bTer\b|\bHighway\b|\bHwy\b|\bExpressway\b|\bExpy\b)\.?(?:[^\S\r\n]+(?:Apt|Apartment|Suite|Ste|Unit|Room|Rm|Bldg|Building|#)[^\S\r\n]*[A-Za-z0-9\-]+)?\b",
    re.IGNORECASE,
)
_PO_BOX_RE = re.compile(
    r"\b(?:P\.?O\.?\s*Box\s+\d+)\b",
    re.IGNORECASE,
)
_CITY_STATE_ZIP_RE = re.compile(
    r"\b[A-Z][a-zA-Z\s\.\-]{1,30},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"
)
_LABELED_ADDRESS_RE = re.compile(
    r"(?i)\b((?:address|residence)[^\S\r\n]*[:=][^\S\r\n]*)([^\n\r,]+(?:,[^\S\r\n]*[^\n\r]+){1,3})"
)
_LABELED_ZIP_RE = re.compile(
    r"(?i)\b((?:zip(?:[^\S\r\n]*code)?|postal(?:[^\S\r\n]*code)?)[^\S\r\n]*[:=][^\S\r\n]*)(\d{5}(?:-\d{4})?)\b"
)

# Name detection regexes (use horizontal whitespace [^\S\r\n] to avoid multi-line cross-field matches)
_LABELED_NAME_RE = re.compile(
    r"(?i)\b((?:candidate|applicant|client|user|employee|contact|person)?[^\S\r\n]*name[^\S\r\n]*[:=][^\S\r\n]*)([A-Za-z\.\'\-]+(?:[^\S\r\n]+[A-Za-z\.\'\-]+){1,3})"
)
_LABELED_FULL_NAME_RE = re.compile(
    r"(?i)\b(full[^\S\r\n]*name[^\S\r\n]*[:=][^\S\r\n]*)([A-Za-z\.\'\-]+(?:[^\S\r\n]+[A-Za-z\.\'\-]+){1,3})"
)
_LABELED_FIRST_NAME_RE = re.compile(
    r"(?i)\b(first[^\S\r\n]*name[^\S\r\n]*[:=][^\S\r\n]*)([A-Za-z\.\'\-]+)"
)
_LABELED_LAST_NAME_RE = re.compile(
    r"(?i)\b(last[^\S\r\n]*name[^\S\r\n]*[:=][^\S\r\n]*)([A-Za-z\.\'\-]+)"
)
_LABELED_CANDIDATE_RE = re.compile(
    r"(?i)\b((?:candidate|applicant)[^\S\r\n]*[:=][^\S\r\n]*)([A-Z][a-z\.\'\-]+(?:[^\S\r\n]+[A-Z][a-z\.\'\-]+){1,3})"
)
_SALUTATION_RE = re.compile(
    r"(?i)\b(dear|hello|hi)[^\S\r\n]+([A-Z][a-z\.\'\-]+(?:[^\S\r\n]+[A-Z][a-z\.\'\-]+)?)\b"
)
_SIGN_OFF_RE = re.compile(
    r"(?i)\b(sincerely|best regards|regards|warmly|thank you),[^\S\r\n]*\n+[^\S\r\n]*([A-Z][a-z\.\'\-]+(?:[^\S\r\n]+[A-Z][a-z\.\'\-]+){1,2})\b"
)
_RESUME_HEADER_NAME_RE = re.compile(
    r"(?m)^([^\S\r\n]*#*[^\S\r\n]*|[^\S\r\n]*\*{1,2})([A-Z][a-z\.\'\-]+(?:[^\S\r\n]+[A-Z][a-z\.\'\-]+){1,2})(\*{1,2})?[^\S\r\n]*$(?=\s*\n\s*(?:[#\*\-\s]*\[(?:EMAIL|PHONE|ADDRESS)\]))"
)

# JSON field regexes inside text
_JSON_NAME_RE = re.compile(
    r'(?i)("(?:candidate_)?name"|"(?:full_)?name"|"(?:first|last)_name"|"applicant_name")\s*:\s*"([^"]+)"'
)
_JSON_EMAIL_RE = re.compile(
    r'(?i)("(?:candidate_)?email")\s*:\s*"([^"]+)"'
)
_JSON_PHONE_RE = re.compile(
    r'(?i)("(?:phone(?:_number)?|mobile|telephone)")\s*:\s*"([^"]+)"'
)
_JSON_ADDRESS_RE = re.compile(
    r'(?i)("(?:street_)?address")\s*:\s*"([^"]+)"'
)

PII_NAME_KEYS = frozenset({
    "name", "full_name", "candidate_name", "first_name", "last_name",
    "applicant_name", "contact_name",
})
PII_EMAIL_KEYS = frozenset({
    "email", "candidate_email", "contact_email",
})
PII_PHONE_KEYS = frozenset({
    "phone", "phone_number", "mobile", "telephone", "cell_phone",
})
PII_ADDRESS_KEYS = frozenset({
    "address", "street_address", "home_address", "postal_address",
})
PII_SSN_KEYS = frozenset({
    "ssn", "social_security_number",
})
PII_ZIP_KEYS = frozenset({
    "zip", "zip_code", "postal_code",
})


def mask_pii_text(text: str) -> str:
    """
    Scrub personal identifiers (name, address, email, phone number, SSN, URLs)
    from text while preserving document structure and non-PII prompt/output content.
    """
    if not text or not isinstance(text, str):
        return text

    # First scrub high-confidence tokens: email, phone, SSN, URLs, addresses
    scrubbed = _EMAIL_RE.sub("[EMAIL]", text)
    scrubbed = _PHONE_RE.sub("[PHONE]", scrubbed)
    scrubbed = _LABELED_PHONE_RE.sub(r"\1[PHONE]", scrubbed)
    scrubbed = _SSN_RE.sub("[SSN]", scrubbed)
    scrubbed = _LINKEDIN_RE.sub("[LINKEDIN]", scrubbed)
    scrubbed = _GITHUB_RE.sub("[GITHUB]", scrubbed)
    scrubbed = _STREET_RE.sub("[ADDRESS]", scrubbed)
    scrubbed = _PO_BOX_RE.sub("[ADDRESS]", scrubbed)
    scrubbed = _CITY_STATE_ZIP_RE.sub("[ADDRESS]", scrubbed)
    scrubbed = _LABELED_ADDRESS_RE.sub(r"\1[ADDRESS]", scrubbed)
    scrubbed = _LABELED_ZIP_RE.sub(r"\1[ZIP]", scrubbed)

    # Resume header name (matches when immediately preceding [EMAIL], [PHONE], or [ADDRESS])
    scrubbed = _RESUME_HEADER_NAME_RE.sub(r"\1[NAME]\3", scrubbed)

    # Labeled names in text
    scrubbed = _LABELED_NAME_RE.sub(r"\1[NAME]", scrubbed)
    scrubbed = _LABELED_FULL_NAME_RE.sub(r"\1[NAME]", scrubbed)
    scrubbed = _LABELED_FIRST_NAME_RE.sub(r"\1[NAME]", scrubbed)
    scrubbed = _LABELED_LAST_NAME_RE.sub(r"\1[NAME]", scrubbed)
    scrubbed = _LABELED_CANDIDATE_RE.sub(r"\1[NAME]", scrubbed)

    # Salutations and sign-offs
    scrubbed = _SALUTATION_RE.sub(r"\1 [NAME]", scrubbed)
    scrubbed = _SIGN_OFF_RE.sub(r"\1,\n[NAME]", scrubbed)

    # JSON fields embedded in text
    scrubbed = _JSON_NAME_RE.sub(r'\1: "[NAME]"', scrubbed)
    scrubbed = _JSON_EMAIL_RE.sub(r'\1: "[EMAIL]"', scrubbed)
    scrubbed = _JSON_PHONE_RE.sub(r'\1: "[PHONE]"', scrubbed)
    scrubbed = _JSON_ADDRESS_RE.sub(r'\1: "[ADDRESS]"', scrubbed)

    return scrubbed


def mask_pii_data(data: Any) -> Any:
    """
    Recursively apply PII masking to strings, dictionaries, lists, or tuples.
    Can be passed directly to LiteLLM / Langfuse as a masking function.
    """
    if data is None:
        return None
    if isinstance(data, str):
        return mask_pii_text(data)
    elif isinstance(data, dict):
        out = {}
        for k, v in data.items():
            k_lower = str(k).lower().strip()
            if k_lower in PII_NAME_KEYS and isinstance(v, str):
                out[k] = "[NAME]"
            elif k_lower in PII_EMAIL_KEYS and isinstance(v, str):
                out[k] = "[EMAIL]"
            elif k_lower in PII_PHONE_KEYS and isinstance(v, str):
                out[k] = "[PHONE]"
            elif k_lower in PII_ADDRESS_KEYS and isinstance(v, str):
                out[k] = "[ADDRESS]"
            elif k_lower in PII_SSN_KEYS and isinstance(v, str):
                out[k] = "[SSN]"
            elif k_lower in PII_ZIP_KEYS and isinstance(v, (str, int)):
                out[k] = "[ZIP]"
            else:
                out[k] = mask_pii_data(v)
        return out
    elif isinstance(data, list):
        return [mask_pii_data(item) for item in data]
    elif isinstance(data, tuple):
        return tuple(mask_pii_data(item) for item in data)
    return data
