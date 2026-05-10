import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class JDCleanserService:
    """
    Service to cleanse job descriptions by stripping boilerplate and fluff sections
    to focus on core responsibilities and requirements.
    Uses Local Ollama for high-quality extraction (prompt from Prompt Library → jd_cleanse),
    with a heuristic fallback.
    """

    @staticmethod
    def cleanse(description: str, title: str = "", max_chars: int = 5000, use_llm: bool = False) -> str:
        """
        Main entry point for cleansing a job description.
        """
        if not description or not (description.strip()):
            return ""

        # If explicitly requested, try the LLM-based extraction first.
        if use_llm:
            llm_result = JDCleanserService.cleanse_with_llm(description, title)
            if llm_result:
                return llm_result[:max_chars].strip()

        return JDCleanserService.cleanse_heuristically(description, title, max_chars)

    @staticmethod
    def cleanse_with_llm(description: str, title: str = "") -> Optional[str]:
        """
        Use Local Ollama to extract core job information.
        Prompt templates: Settings → Prompt library, tab **JD cleanse**. Placeholders: ``{title}``, ``{job_description}``.
        """
        try:
            from .llm_factory import get_llm
            from .models import LLMProviderConfig
            from .prompt_store import build_jd_cleanse_llm_messages

            provider = "Ollama Local"
            config = LLMProviderConfig.objects.filter(provider=provider, is_active=True).first()
            if not config:
                return None

            llm = get_llm(provider, model=config.default_model or "nemotron")

            body = (description or "")[:8000]
            messages = build_jd_cleanse_llm_messages(
                None,
                title=(title or "").strip(),
                job_description=body,
            )

            response = llm.invoke(messages)
            content = response.content if hasattr(response, 'content') else str(response)

            if content and len(content) > 50:
                return content.strip()

            return None
        except Exception as e:
            logger.warning("[JDCleanser] LLM cleansing failed: %s", e)
            return None

    @staticmethod
    def cleanse_heuristically(description: str, title: str = "", max_chars: int = 5000) -> str:
        """
        Fast heuristic cleansing as a fallback or for low-latency paths.
        """
        # Role vs fluff section headers (case-insensitive substrings)
        ROLE_HEADER_KEYWORDS = (
            "job description", "description", "responsibilities", "qualifications",
            "requirements", "about the role", "overview", "you will", "what you'll do",
            "what you will", "role", "summary", "duties", "must have", "experience",
            "technical skills", "what we're looking for", "key qualifications",
        )

        FLUFF_HEADER_KEYWORDS = (
            "why join", "benefits", "culture", "our team", "equal opportunity",
            "chart your journey", "deliver your impact", "about us", "about the company",
            "who we are", "life at", "perks", "compensation", "how to apply",
            "physical requirements", "work environment", "travel requirements",
            "diversity and inclusion",
        )

        d = description.strip()

        # 0. Compatibility with old behavior: if no obvious headers, find first role marker
        role_markers_pattern = r"(responsible for|you will|requirements?|qualifications?|the ideal candidate|about the role|what you'll do|key responsibilities)"

        # 1. Section-based filtering
        lines = d.split('\n')

        # Heuristic: if we don't see any lines that look like headers,
        # try to skip leading fluff by finding the first role marker.
        header_pattern = re.compile(r"^(?:\*\*|##|###)?\s*([^*#:]+)(?:\*\*|:)?\s*$")
        has_headers = False
        for line in lines:
            stripped = line.strip()
            if stripped and header_pattern.match(stripped) and len(stripped.split()) < 6:
                has_headers = True
                break

        if not has_headers:
            m = re.search(role_markers_pattern, d, re.IGNORECASE)
            if m:
                # Start from the line containing the marker
                marker_pos = m.start()
                pre_marker = d[:marker_pos]
                last_newline = pre_marker.rfind('\n')
                if last_newline != -1:
                    d = d[last_newline+1:]
                else:
                    d = d[marker_pos:]
                lines = d.split('\n')

        cleansed_lines = []
        is_fluff_section = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                if not is_fluff_section:
                    cleansed_lines.append(line)
                continue

            # Check if line looks like a header
            match = header_pattern.match(stripped)
            if match:
                header_text = match.group(1).strip().lower()
                # Use a word-count heuristic to avoid false positives on long sentences
                if len(header_text.split()) < 6:
                    if any(kw in header_text for kw in FLUFF_HEADER_KEYWORDS):
                        is_fluff_section = True
                        continue
                    elif any(kw in header_text for kw in ROLE_HEADER_KEYWORDS):
                        is_fluff_section = False

            if not is_fluff_section:
                # 2. Sentence-level boilerplate stripping within non-fluff sections
                if not JDCleanserService._is_boilerplate(stripped):
                    cleansed_lines.append(line)

        result = "\n".join(cleansed_lines).strip()

        # 3. Secondary pass: if title is provided, ensure we haven't stripped too much
        # If result is very short but original was long, fallback to original truncated
        if len(result) < 200 and len(description) > 1000:
            return description[:max_chars].strip()

        # If it's a very short text (like in some tests), just return it
        if not result and description:
            return description[:max_chars].strip()

        return result[:max_chars].strip()

    @staticmethod
    def _is_boilerplate(text: str) -> bool:
        """Heuristic to check if a single line/sentence is boilerplate."""
        BOILERPLATE_SENTENCE_PHRASES = (
            "equal opportunity", "eeo", "affirmative action", "we offer competitive",
            "join our team", "join us", "apply now", "background check", "drug screening",
            "must be able to", "ability to work", "diversity and inclusion",
            "inclusion and diversity", "we are an equal", "all qualified applicants",
            "reasonable accommodation", "without regard to", "race, color",
            "gender, race", "sexual orientation", "veteran status", "disability status",
            "competitive salary", "medical, dental, vision", "401(k)", "401k",
            "unlimited pto", "flexible work", "work-life balance", "stipend",
            "competitive compensation", "comprehensive benefits", "paid time off",
        )
        if len(text) < 15:
            return False

        lower_text = text.lower()
        matches = sum(1 for phrase in BOILERPLATE_SENTENCE_PHRASES if phrase in lower_text)
        if matches >= 1:
            if "experience" in lower_text or "degree" in lower_text or "proficiency" in lower_text:
                return False
            return True
        return False
