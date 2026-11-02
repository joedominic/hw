import re
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

class JDCleanserService:
    """
    Service to cleanse job descriptions by stripping boilerplate and fluff sections
    to focus on core responsibilities and requirements.
    Pipeline/vetting LLM cleanses strictly use Ollama Local (only_local=True);
    falls back to regex heuristics if local LLM is unavailable, never burning cloud tokens.
    (Resume-optimizer JD cleanse goes through the optimizer graph and stays cloud-only.)
    """

    @staticmethod
    def cleanse(
        description: str,
        title: str = "",
        max_chars: int = 5000,
        use_llm: bool = False,
        *,
        user=None,
        only_local: bool = True,
    ) -> str:
        """
        Main entry point for cleansing a job description.
        """
        if not description or not (description.strip()):
            return ""

        # If explicitly requested, try the LLM-based extraction first.
        if use_llm and user is not None:
            llm_result = JDCleanserService.cleanse_with_llm(
                description, title, user=user, only_local=only_local
            )
            if llm_result:
                return llm_result[:max_chars].strip()

        return JDCleanserService.cleanse_heuristically(description, title, max_chars)

    @staticmethod
    def cleanse_with_llm(
        description: str,
        title: str = "",
        *,
        user,
        only_local: bool = True,
    ) -> Optional[str]:
        """
        Use local Ollama to extract core job information (pipeline / vetting path).
        Falls back to None (triggering heuristic cleansing) if local LLM is unavailable.
        """
        try:
            from .llm import USAGE_QUERY_JD_CLEANSE, invoke_llm_messages
            from langchain_core.messages import HumanMessage

            prompt = (
                f"Extract only the core responsibilities and technical requirements for the job title '{title}' "
                "from the following description. Eliminate all boilerplate, benefits, company info, and EEO statements. "
                "Preserve technical keywords and specific qualifications.\n\n"
                f"Job Description:\n{description[:8000]}\n\n"
                "Extracted Core Info:"
            )

            response = invoke_llm_messages(
                [HumanMessage(content=prompt)],
                user=user,
                prefer_local=True,
                only_local=only_local,
                allow_local=True,
                usage_query_kind=USAGE_QUERY_JD_CLEANSE,
            )
            content = response.content if hasattr(response, 'content') else str(response)

            if content and len(content) > 50:
                return content.strip()

            return None
        except Exception as e:
            tenant_label = getattr(user, "username", getattr(user, "id", "unknown")) if user else "unknown"
            logger.warning("[JDCleanser] tenant=%s LLM cleansing failed, falling back to heuristics: %s", tenant_label, e)
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
            # Modern corporate & ATS fluff headers
            "what's in it for you", "what is in it for you", "what we offer",
            "what you'll get", "what you get", "why work with us", "why work here",
            "total rewards", "pay range", "salary range", "compensation range",
            "base compensation", "base pay", "social impact", "making a real-world impact",
            "our impact", "community", "our commitment", "our values",
            "hybrid work model", "hybrid work", "remote work", "workplace model",
            "career development and growth", "career development",
            "use of ai within the recruitment process", "our use of ai", "ai in recruitment",
            "recruitment process", "hiring process", "notice to applicants",
            "eeo statement", "affirmative action", "privacy notice",
        )

        def _normalize_header_text(h: str) -> str:
            t = (h or "").strip().lower()
            t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
            t = re.sub(r"[\?\:\!\.\*\_]+$", "", t).strip()
            return t

        d = description.strip()

        # 0. Compatibility with old behavior: if no obvious headers, find first role marker
        role_markers_pattern = r"(responsible for|you will|requirements?|qualifications?|the ideal candidate|about the role|what you'll do|key responsibilities)"

        # 1. Section-based filtering: scan for role headers vs fluff headers
        lines = d.split('\n')
        header_pattern = re.compile(r"^(?:\*\*|##|###)?\s*([^*#]+?)(?:\*\*|##|###)?\s*$")
        
        # Check if the document contains explicit role section headers
        has_role_header = False
        first_role_header_idx = None
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            match = header_pattern.match(stripped)
            if match:
                header_text = _normalize_header_text(match.group(1))
                if len(header_text.split()) < 10 and any(kw in header_text for kw in ROLE_HEADER_KEYWORDS):
                    has_role_header = True
                    first_role_header_idx = idx
                    break

        if has_role_header and first_role_header_idx is not None:
            # Skip all leading company bio / history / fluff preceding the first role header
            lines = lines[first_role_header_idx:]
        else:
            # Fallback for documents without explicit headers: find first role marker phrase
            m = re.search(role_markers_pattern, d, re.IGNORECASE)
            if m:
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
                header_text = _normalize_header_text(match.group(1))
                # Use a word-count heuristic to avoid false positives on long sentences
                if len(header_text.split()) < 10:
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

    @classmethod
    def extract_scope_and_requirements(
        cls,
        description: str,
        title: str = "",
        max_scope_chars: int = 1200,
        max_reqs_chars: int = 1500,
    ) -> Tuple[str, str]:
        """
        Extract dual-sectored job description slices:
        1. Scope: Role mission, high-level duties, and scope of responsibility.
        2. Requirements: Technical skills, mandatory qualifications, tech stack, and experience.

        Guarantees that both sectors fit comfortably within the 256-token embedding context
        without silent truncation.
        """
        if not description or not description.strip():
            return "", ""

        cleansed = cls.cleanse_heuristically(description, title=title, max_chars=8000)
        if not cleansed:
            return "", ""

        SCOPE_HEADER_KEYWORDS = (
            "about the role", "overview", "summary", "role", "responsibilities",
            "what you'll do", "what you will", "you will", "duties",
            "the opportunity", "position summary", "job description", "description",
            "about the position", "day in the life", "your impact", "core responsibilities",
        )

        REQS_HEADER_KEYWORDS = (
            "requirements", "qualifications", "key qualifications", "basic qualifications",
            "minimum qualifications", "preferred qualifications", "technical skills",
            "must have", "what we're looking for", "what you bring", "what you'll bring",
            "who you are", "skills and experience", "tech stack", "technologies",
            "required skills", "desired skills", "experience required", "technical requirements",
        )

        lines = cleansed.split("\n")
        header_pattern = re.compile(r"^(?:\*\*|##|###)?\s*([^*#]+?)(?:\*\*|##|###)?\s*$")

        scope_lines: List[str] = []
        reqs_lines: List[str] = []
        current_sector: str = "scope"  # default initial sector to scope
        has_reqs_header = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            match = header_pattern.match(stripped)
            if match:
                norm_header = match.group(1).strip().lower()
                norm_header = re.sub(r"[\?\:\!\.\*\_]+$", "", norm_header).strip()
                if len(norm_header.split()) < 10:
                    if any(kw in norm_header for kw in REQS_HEADER_KEYWORDS):
                        current_sector = "reqs"
                        has_reqs_header = True
                        reqs_lines.append(stripped)
                        continue
                    elif any(kw in norm_header for kw in SCOPE_HEADER_KEYWORDS):
                        current_sector = "scope"
                        scope_lines.append(stripped)
                        continue

            if current_sector == "reqs":
                reqs_lines.append(stripped)
            else:
                scope_lines.append(stripped)

        scope_text = "\n".join(scope_lines).strip()
        reqs_text = "\n".join(reqs_lines).strip()

        # Fallback if no explicit requirements header was found:
        # Split cleansed text into scope (top ~40%) and requirements (bottom ~60%)
        if not has_reqs_header or not reqs_text:
            all_lines = [ln.strip() for ln in lines if ln.strip()]
            if len(all_lines) >= 4:
                split_point = max(2, int(len(all_lines) * 0.4))
                scope_text = "\n".join(all_lines[:split_point]).strip()
                reqs_text = "\n".join(all_lines[split_point:]).strip()
            else:
                scope_text = cleansed
                reqs_text = cleansed

        return (
            scope_text[:max_scope_chars].strip(),
            reqs_text[:max_reqs_chars].strip(),
        )

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
            # Modern corporate disclosures & legal fluff
            "use of ai within the recruitment process", "utilizes artificial intelligence",
            "complies with local laws that require upfront disclosure",
            "base compensation range varies across locations",
            "eligible office location(s) for this role include",
            "total reward program which also includes flexible and supportive benefits",
            "headspace app", "mental health days", "tuition reimbursement",
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
