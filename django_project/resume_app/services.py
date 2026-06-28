import os
import logging
import pdfplumber

logger = logging.getLogger(__name__)


class PDFParseError(Exception):
    """Raised when PDF cannot be parsed or is invalid."""
    pass


def parse_pdf(file_path: str) -> str:
    """
    Extract text from a PDF file.
    Raises PDFParseError if file is missing, unreadable, or yields no text.
    """
    if not file_path or not isinstance(file_path, str):
        raise PDFParseError("Invalid file path")
    if not os.path.isfile(file_path):
        raise PDFParseError(f"File not found: {file_path}")
    if os.path.getsize(file_path) == 0:
        raise PDFParseError("PDF file is empty")

    try:
        with pdfplumber.open(file_path) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text() or ""
    except Exception as e:
        logger.exception("PDF parse failed for %s", file_path)
        raise PDFParseError(f"Could not read PDF: {e}") from e

    if not text or not text.strip():
        raise PDFParseError("PDF contains no extractable text")

    return text.strip()


class DraftSaveError(Exception):
    """User-facing draft save validation error."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def save_optimized_draft_content(resume_id: int, content: str, *, user):
    """
    Persist user-edited optimized resume text before PDF/Word export.

    ``user`` is required to enforce ownership — any authenticated user who
    guesses a valid ``resume_id`` must not be able to overwrite another
    tenant's draft.
    """
    from .models import OptimizedResume

    optimized = OptimizedResume.objects.for_user(user).filter(pk=int(resume_id)).first()
    if not optimized:
        raise DraftSaveError("Optimized resume not found.", status_code=404)
    if optimized.status not in (
        OptimizedResume.STATUS_COMPLETED,
        OptimizedResume.STATUS_RUNNING,
    ):
        raise DraftSaveError(
            "Draft can only be saved when the run is completed or still in progress."
        )
    if not (content or "").strip():
        raise DraftSaveError("Draft cannot be empty.")
    optimized.optimized_content = content
    optimized.save(update_fields=["optimized_content", "updated_at"])
    return optimized


def run_ollama_guard_on_payloads(payloads: list, track_slug: str) -> list:
    """
    Run local Ollama check on job payloads to verify seniority and fit.
    Updates payloads in-place with 'ollama_guard_status' and 'ollama_guard_reason'.
    """
    import re

    from .llm_factory import get_llm
    from .models import LLMProviderConfig, Track

    provider = "Ollama Local"
    config = LLMProviderConfig.objects.filter(provider=provider).first()
    if not config or not config.is_active:
        return payloads

    try:
        llm = get_llm(provider, model=config.default_model or "nemotron")
    except Exception as e:
        logger.warning("[ollama_guard] Could not initialize Ollama: %s", e)
        return payloads

    track_obj = Track.objects.filter(slug=track_slug).first()
    target_level = track_obj.label if track_obj else "Professional"

    prompt_template = (
        "Check if this job matches a {target_level} level seniority and scope.\n"
        "Job Title: {title}\n"
        "Snippet: {snippet}\n\n"
        "Respond in JSON format: {{\"match\": \"YES\"|\"NO\"|\"UNCERTAIN\", \"reason\": \"one sentence reason\"}}"
    )

    for p in payloads:
        try:
            import json

            from langchain_core.messages import HumanMessage

            prompt = prompt_template.format(
                target_level=target_level, title=p.title, snippet=p.snippet
            )
            response = llm.invoke([HumanMessage(content=prompt)])
            content = response.content if hasattr(response, "content") else str(response)

            # Simple JSON extraction
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                p.ollama_guard_status = data.get("match", "UNCERTAIN").upper()
                p.ollama_guard_reason = data.get("reason", "")

                # Penalize score if it's a clear NO
                if p.ollama_guard_status == "NO" and p.focus_percent is not None:
                    p.focus_percent = max(0, p.focus_percent - 30)
                    if p.preference_margin_percent is not None:
                        p.preference_margin_percent -= 30
            else:
                p.ollama_guard_status = "UNCERTAIN"
        except Exception as e:
            logger.warning("[ollama_guard] Evaluation failed for job %s: %s", p.id, e)
            p.ollama_guard_status = "ERROR"

    return payloads
