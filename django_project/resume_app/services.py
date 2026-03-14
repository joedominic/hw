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
