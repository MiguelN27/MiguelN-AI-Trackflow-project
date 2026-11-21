"""Text normalisation for the guardrails: every detector matches on the same folded form.

An attacker rarely writes "ignore your instructions" plainly twice. The usual
disguises are a different case, accents, full-width or other compatibility
characters, zero-width or bidi characters inside a word, leetspeak
("1gn0r3"), and letters spaced out ("i g n o r e"). `fold()` undoes all of
them, so one rule written in plain lowercase catches every variant, and
`visible()` keeps the original case for identifiers (tracking numbers).
"""

import re
import unicodedata
from typing import Final

# Leetspeak digits and symbols, read as the letters they stand for. Only used on
# the copy the keyword detectors read: identifiers keep their digits.
_LEET: Final = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)

# Four or more single letters separated by one space, dot, dash, underscore or
# asterisk: "i g n o r e", "i.g.n.o.r.e", "r-u-l-e-s".
_SPACED_LETTERS: Final = re.compile(r"\b(?:[a-z][ .\-_*]){3,}[a-z]\b")
_SEPARATORS: Final = re.compile(r"[ .\-_*]")
_WHITESPACE: Final = re.compile(r"\s+")


def visible(text: str) -> str:
    """NFKC, without invisible characters: format characters (zero-width, bidi
    controls) are dropped, control characters become spaces, line breaks stay."""
    normalised = unicodedata.normalize("NFKC", text)
    kept: list[str] = []
    for char in normalised:
        category = unicodedata.category(char)
        if category == "Cf":
            continue
        if category == "Cc" and char not in "\n\t":
            kept.append(" ")
            continue
        kept.append(char)
    return "".join(kept)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def fold(text: str, *, leet: bool = True) -> str:
    """The form detectors read: visible, accent-free, case-folded, de-leeted, unspaced.

    Typographic apostrophes become "'", so "don’t" and "don't" read alike.
    Whitespace collapses to single spaces; line breaks are kept as sentence ends.
    """
    folded = strip_accents(visible(text)).casefold()
    folded = folded.replace("’", "'").replace("‘", "'").replace("`", "'")
    if leet:
        folded = folded.translate(_LEET)
    folded = _SPACED_LETTERS.sub(lambda match: _SEPARATORS.sub("", match.group(0)), folded)
    lines = (_WHITESPACE.sub(" ", line).strip() for line in folded.splitlines())
    return "\n".join(line for line in lines if line)
