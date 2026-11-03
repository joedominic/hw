import os
import logging
import tempfile
from contextlib import contextmanager
from typing import Any
import pdfplumber
from django.core.exceptions import SuspiciousOperation
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)


class PDFParseError(Exception):
    """Raised when PDF cannot be parsed or is invalid."""
    pass


@contextmanager
def local_file_or_temp(file_source: Any):
    """
    Yield a local filesystem path for a file path, FieldFile, or file-like object.
    If the file is stored on remote storage (e.g. S3), it is safely downloaded to
    a temporary file and deleted on exit.

    Safe on Windows / pdfplumber: creates with delete=False, closes handle before
    yielding, and cleans up in a finally block.
    """
    if file_source is None:
        raise PDFParseError("No file provided")

    # If already a string or os.PathLike
    if isinstance(file_source, (str, bytes, os.PathLike)):
        yield os.fspath(file_source) if isinstance(file_source, os.PathLike) else str(file_source)
        return

    # Attempt to access .path directly (works on local FileSystemStorage)
    try:
        if hasattr(file_source, "path"):
            yield file_source.path
            return
    except (NotImplementedError, AttributeError, ValueError, SuspiciousOperation):
        pass

    # Remote storage or stream without a local path: stream to a temporary file
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
            if hasattr(file_source, "open"):
                with file_source.open("rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        tmp.write(chunk)
            elif hasattr(file_source, "read"):
                while chunk := file_source.read(64 * 1024):
                    tmp.write(chunk)
            elif hasattr(file_source, "name"):
                with default_storage.open(file_source.name, "rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        tmp.write(chunk)
            else:
                raise PDFParseError(f"Cannot read file source of type {type(file_source).__name__}")
            tmp.flush()

        yield tmp_path
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def parse_pdf(file_source: Any) -> str:
    """
    Extract text from a PDF file. Accepts a local path string, FieldFile, or File-like object.
    Raises PDFParseError if file is missing, unreadable, or yields no text.
    """
    if not file_source:
        raise PDFParseError("Invalid file path or empty file")

    with local_file_or_temp(file_source) as file_path:
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
    from .utils import sanitize_resume_markdown
    content = sanitize_resume_markdown(content)
    optimized.optimized_content = content
    optimized.save(update_fields=["optimized_content", "updated_at"])
    return optimized


def run_ollama_guard_on_payloads(payloads: list, track_slug: str, *, user) -> list:
    """
    Run local Ollama check on job payloads to verify seniority and fit.
    Updates payloads in-place with 'ollama_guard_status' and 'ollama_guard_reason'.
    """
    import re
    import json

    from langchain_core.messages import HumanMessage
    from .llm import invoke_llm_messages, local_llm_available, USAGE_QUERY_FIT_CHECK
    from .models import Track

    if not user or not local_llm_available(user):
        return payloads

    track_obj = Track.objects.for_user(user).filter(slug=track_slug).first()
    target_level = track_obj.label if track_obj else "Professional"

    prompt_template = (
        "Check if this job matches a {target_level} level seniority and scope.\n"
        "Job Title: {title}\n"
        "Snippet: {snippet}\n\n"
        "Respond in JSON format: {{\"match\": \"YES\"|\"NO\"|\"UNCERTAIN\", \"reason\": \"one sentence reason\"}}"
    )

    for p in payloads:
        try:
            prompt = prompt_template.format(
                target_level=target_level, title=p.title, snippet=p.snippet
            )
            response = invoke_llm_messages(
                [HumanMessage(content=prompt)],
                user=user,
                usage_query_kind=USAGE_QUERY_FIT_CHECK,
                prefer_local=True,
                only_local=True,
                allow_local=True,
            )
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
