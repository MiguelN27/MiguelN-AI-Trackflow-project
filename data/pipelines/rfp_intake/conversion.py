"""PDF -> Markdown, before any agent reads the document (ticket: "PDF RFPs are heavy on tokens").

MarkItDown's own PDF converter (`markitdown[pdf]`, pdfminer underneath), called
directly rather than through `MarkItDown.convert`: when the PDF converter fails,
`convert` falls back to its plain-text converter and returns a truncated or
corrupt PDF's raw bytes as "Markdown". The output is tidied by
`text.clean_markdown`. Known limits, documented in
`docs/rfp/rfp-intake.md`: tables come out column by column (labels, then
values), and a scanned PDF has no text layer (no OCR), which is refused here
rather than handed to the classifier as an empty document.
"""

import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Final

from markitdown import StreamInfo
from markitdown.converters import PdfConverter

from data.pipelines.rfp_intake.text import clean_markdown

# Fewer visible characters than this and there is nothing to classify.
MIN_TEXT_CHARS: Final = 20
TRUNCATION_NOTICE: Final = "\n\n[... document truncated by TrackFlow before analysis ...]\n"

_LOCK: Final = threading.Lock()
_PDF_STREAM: Final = StreamInfo(mimetype="application/pdf", extension=".pdf")


class ConversionError(RuntimeError):
    """The PDF could not be turned into text. The message is safe to show."""


@lru_cache(maxsize=1)
def _converter() -> PdfConverter:
    return PdfConverter()


def pdf_to_markdown(path: Path) -> str:
    if not path.is_file():
        raise ConversionError("the uploaded PDF is missing from storage")
    try:
        # One conversion at a time: MarkItDown's converters are not documented
        # as thread-safe, and a conversion takes milliseconds.
        with _LOCK, path.open("rb") as stream:
            result = _converter().convert(stream, _PDF_STREAM)
    except Exception as error:  # noqa: BLE001 - every converter failure means "unreadable"
        raise ConversionError(f"the PDF could not be converted ({type(error).__name__})") from error
    markdown = clean_markdown(result.markdown or "")
    if len(re.sub(r"\s", "", markdown)) < MIN_TEXT_CHARS:
        raise ConversionError(
            "the PDF has no extractable text (a scanned image? OCR is not supported)"
        )
    return markdown


def truncate(markdown: str, limit: int) -> tuple[str, bool]:
    """(`markdown` cut to `limit` characters with a visible notice, whether it was cut)."""
    if len(markdown) <= limit:
        return markdown, False
    return markdown[:limit].rstrip() + TRUNCATION_NOTICE, True
