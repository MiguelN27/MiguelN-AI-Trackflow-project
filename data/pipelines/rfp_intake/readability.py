"""Readability metrics, so Sales can anticipate what a document costs to process.

Two kinds of figure:

- Always available: characters, words, sentences, and `estimated_tokens`
  (characters / 4, the usual approximation for English and Spanish text; no
  tokenizer dependency). `processing_cost` is a tier read from the tokens.
- From `py-readability-metrics`: Flesch reading ease, Flesch-Kincaid grade and
  Gunning Fog. The library refuses texts under 100 words, and its tokenizer
  needs NLTK's `punkt` / `punkt_tab` data (`npm run rfp:setup`). Either way the
  scores are left empty with a `note`, never a failed ticket. The formulas are
  calibrated for English: on a Spanish or bilingual RFP they are indicative.
"""

import math
import re
import warnings
from dataclasses import asdict, dataclass
from typing import Any, Final

CHARS_PER_TOKEN: Final = 4
MIN_WORDS: Final = 100  # py-readability-metrics' own minimum
# Tiers by estimated tokens of the converted document.
MEDIUM_FROM_TOKENS: Final = 2_000
HIGH_FROM_TOKENS: Final = 8_000

_WORD: Final = re.compile(r"\b\w+\b", re.UNICODE)
_SENTENCE_END: Final = re.compile(r"[.!?]+(?:\s|$)")


@dataclass(frozen=True)
class ReadabilityMetrics:
    char_count: int
    word_count: int
    sentence_count: int
    estimated_tokens: int
    processing_cost: str
    flesch_reading_ease: float | None
    flesch_kincaid_grade: float | None
    gunning_fog: float | None
    note: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def cost_tier(estimated_tokens: int) -> str:
    if estimated_tokens >= HIGH_FROM_TOKENS:
        return "high"
    if estimated_tokens >= MEDIUM_FROM_TOKENS:
        return "medium"
    return "low"


def _rounded(value: Any) -> float | None:
    return round(float(value), 2) if value is not None else None


def measure(markdown: str, *, language: str | None = None) -> ReadabilityMetrics:
    char_count = len(markdown)
    estimated_tokens = math.ceil(char_count / CHARS_PER_TOKEN)
    notes: list[str] = []
    words = len(_WORD.findall(markdown))
    sentences = max(1, len(_SENTENCE_END.findall(markdown))) if words else 0
    scores: dict[str, float | None] = {
        "flesch_reading_ease": None,
        "flesch_kincaid_grade": None,
        "gunning_fog": None,
    }

    try:
        with warnings.catch_warnings():
            # The package has an invalid escape sequence in a regex literal.
            warnings.simplefilter("ignore", SyntaxWarning)
            from readability import Readability
            from readability.exceptions import ReadabilityException

        analysis = Readability(markdown)
        statistics = analysis.statistics()
        words = int(statistics["num_words"])
        sentences = int(statistics["num_sentences"])
        if words < MIN_WORDS:
            notes.append(f"Scores need at least {MIN_WORDS} words; this document has {words}.")
        else:
            try:
                scores["flesch_reading_ease"] = _rounded(analysis.flesch().score)
                scores["flesch_kincaid_grade"] = _rounded(analysis.flesch_kincaid().score)
                scores["gunning_fog"] = _rounded(analysis.gunning_fog().score)
            except ReadabilityException as error:
                notes.append(f"Scores unavailable: {error}")
    except LookupError:
        notes.append(
            "Scores unavailable: NLTK tokenizer data is not installed (run `npm run rfp:setup`)."
        )

    if language and language not in {"en", "other"} and scores["flesch_reading_ease"] is not None:
        notes.append("The scores are calibrated for English; this document is not only English.")

    return ReadabilityMetrics(
        char_count=char_count,
        word_count=words,
        sentence_count=sentences,
        estimated_tokens=estimated_tokens,
        processing_cost=cost_tier(estimated_tokens),
        note=" ".join(notes) or None,
        **scores,
    )
