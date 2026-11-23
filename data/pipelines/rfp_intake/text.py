"""Text rules the deterministic guards share: verbatim quotes and supported figures.

The agents quote the document and state figures; these functions decide,
without a model, whether a quote really is in the document and whether every
figure in a sentence comes from it. They tolerate what PDF conversion does to
text (wrapped lines, curly quotes, dashes) and the two number conventions the
RFPs use: "15.000" (Spain) and "15,000" (US) are the same figure.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Final

# pdfminer writes a glyph it cannot map as "(cid:127)"; in these RFPs it is
# always a list bullet.
_CID_GLYPH: Final = re.compile(r"\(cid:\d+\)\s*")
_BLANK_RUNS: Final = re.compile(r"\n{3,}")
_SPACES: Final = re.compile(r"\s+")
_ELLIPSIS: Final = re.compile(r"\s*(?:\.\.\.|…)\s*")
_NUMBER: Final = re.compile(r"\d(?:[\d.,]*\d)?")
# A list marker an agent copies from how extracts were numbered for it ("2. ").
_LIST_MARKER: Final = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*•])\s+")
_TRANSLATE: Final = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "«": '"',
        "»": '"',
        "—": "-",
        "–": "-",
        "‐": "-",
        "−": "-",
        " ": " ",
    }
)

# Single-digit figures are not checked: they are mostly structural ("section 2",
# "phase 1"), and nothing an RFP prices or counts is that small.
MIN_CHECKED_FIGURE: Final = 10
# A quote shorter than this proves nothing about the document.
MIN_QUOTE_CHARS: Final = 4


def clean_markdown(text: str) -> str:
    """Conversion output tidied: unmapped bullet glyphs as "- ", no blank-line runs."""
    cleaned = _CID_GLYPH.sub("- ", text)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip() + "\n"


def comparable(text: str) -> str:
    """Case-, quote-, dash- and spacing-insensitive form, for containment checks."""
    folded = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).casefold()
    return _SPACES.sub(" ", folded).strip(" .,;:!?¡¿\"'()-")


def squash(text: str) -> str:
    """`text` on one line: the line breaks PDF conversion leaves inside sentences removed."""
    return _SPACES.sub(" ", text).strip()


def strip_list_marker(quote: str) -> str:
    return _LIST_MARKER.sub("", quote, count=1)


def clean_quote(quote: str) -> str:
    """A quote as stored and shown: one line, without a copied list marker."""
    return squash(strip_list_marker(quote))


def appears_in(quote: str | None, document: str) -> bool:
    """True when every piece of `quote` (split at "...") is in `document`.

    A leading list marker ("2. ", "- ") is ignored: agents copy the numbering
    their extracts were given in, which is not part of the document.
    """
    if not quote:
        return False
    pieces = [comparable(piece) for piece in _ELLIPSIS.split(strip_list_marker(quote))]
    pieces = [piece for piece in pieces if piece]
    if not pieces or sum(len(piece) for piece in pieces) < MIN_QUOTE_CHARS:
        return False
    haystack = comparable(document)
    return all(piece in haystack for piece in pieces)


def canonical_figures(text: str | None) -> set[str]:
    """Every figure in `text`, digits only: "15.000", "15,000", "15000" -> "15000"."""
    if not text:
        return set()
    return {re.sub(r"\D", "", match) for match in _NUMBER.findall(text)}


def checked_figures(text: str | None) -> set[str]:
    """The figures in `text` that the guards check (single digits are ignored)."""
    return {figure for figure in canonical_figures(text) if int(figure) >= MIN_CHECKED_FIGURE}


def unsupported_figures(text: str | None, sources: Iterable[str | None]) -> list[str]:
    """Figures in `text` that appear in none of `sources`, sorted."""
    known: set[str] = set()
    for source in sources:
        known |= canonical_figures(source)
    return sorted(checked_figures(text) - known, key=int)


def first_line(text: str, limit: int = 160) -> str:
    """One line of `text`, at most `limit` characters, for notes and logs."""
    line = _SPACES.sub(" ", text).strip()
    return line if len(line) <= limit else line[: limit - 1] + "…"
