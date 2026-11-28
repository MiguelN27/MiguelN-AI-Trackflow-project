"""Document ingestion: PDF -> Markdown, the text rules the guards share, readability."""

from pathlib import Path

import pytest

from data.pipelines.rfp_intake import conversion, readability, text
from tests.pipelines.rfp_intake.support import PDF_DIR, SAMPLES, sample_markdown

# --- Conversion (real MarkItDown on the briefing's PDFs, offline) -------------------------


@pytest.mark.parametrize(
    ("kind", "phrases"),
    [
        ("formal", ["REQUEST FOR PROPOSAL (RFP)", "ModaViva, S.L.", "15.000 SKUs", "Zaragoza"]),
        ("informal", ["Luna Cosmetics", "5,000 orders a month", "carrier selection"]),
        ("invalid", ["RapidShip Carriers", "10,000 packages/month"]),
    ],
)
def test_each_sample_pdf_converts_to_markdown(kind, phrases):
    markdown = sample_markdown(kind)
    for phrase in phrases:
        assert phrase in markdown


def test_unmapped_bullet_glyphs_become_list_markers():
    markdown = sample_markdown("formal")
    assert "(cid:" not in markdown
    assert "- Gestión de devoluciones" in markdown


def test_a_corrupt_pdf_is_a_conversion_error_not_text(tmp_path):
    # MarkItDown.convert would fall back to plain text and return these bytes.
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\nthis is not a real PDF body, only its header")

    with pytest.raises(
        conversion.ConversionError, match=r"could not be converted \(PDFSyntaxError\)"
    ):
        conversion.pdf_to_markdown(broken)


def test_a_missing_file_is_a_conversion_error(tmp_path):
    with pytest.raises(conversion.ConversionError, match="missing from storage"):
        conversion.pdf_to_markdown(tmp_path / "gone.pdf")


def test_a_pdf_without_text_is_refused_not_classified(tmp_path, monkeypatch):
    class EmptyResult:
        markdown = "   \n\n  "

    class EmptyConverter:
        def convert(self, _stream, _stream_info):
            return EmptyResult()

    monkeypatch.setattr(conversion, "_converter", lambda: EmptyConverter())
    scanned = tmp_path / "scan.pdf"
    scanned.write_bytes(b"%PDF-1.4")

    with pytest.raises(conversion.ConversionError, match="OCR is not supported"):
        conversion.pdf_to_markdown(scanned)


def test_truncation_is_visible_and_flagged():
    cut, truncated = conversion.truncate("x" * 50, 20)
    assert truncated
    assert cut.startswith("x" * 20)
    assert "truncated by TrackFlow" in cut
    assert conversion.truncate("short", 20) == ("short", False)


# --- Text rules -------------------------------------------------------------------------------


def test_quotes_survive_wrapped_lines_curly_quotes_and_dashes():
    document = "We need someone to handle\nwarehousing — ideally someone who can't wait"
    assert text.appears_in("handle warehousing - ideally someone who can’t wait", document)
    assert text.appears_in("WE NEED SOMEONE", document)
    assert text.appears_in("We need someone ... who can't wait", document)
    assert not text.appears_in("We need two warehouses", document)
    assert not text.appears_in("", document)
    assert not text.appears_in("We", document)  # too short to prove anything


def test_spanish_and_us_thousand_separators_are_the_same_figure():
    assert text.canonical_figures("15.000 SKUs") == {"15000"}
    assert text.canonical_figures("15,000 SKUs") == {"15000"}
    assert text.unsupported_figures("about 15,000 SKUs", ["aproximadamente 15.000 SKUs"]) == []


def test_figures_not_in_the_sources_are_reported():
    sources = ["we just crossed 5,000 orders a month"]
    assert text.unsupported_figures("5,000 orders and 2,000 pallets, 48 hours", sources) == [
        "48",
        "2000",
    ]
    # Single digits are structural ("phase 1"), not checked.
    assert text.unsupported_figures("phase 1 of 3", sources) == []


def test_clean_quote_is_one_line_without_a_copied_marker():
    assert text.clean_quote("2. Buscamos un\npartner logístico") == "Buscamos un partner logístico"


# --- Readability -----------------------------------------------------------------------------


def test_readability_of_the_formal_rfp():
    metrics = readability.measure(sample_markdown("formal"), language="mixed")

    assert metrics.word_count > 150
    assert metrics.sentence_count > 5
    assert metrics.estimated_tokens == -(-metrics.char_count // readability.CHARS_PER_TOKEN)
    assert metrics.processing_cost == "low"
    assert metrics.flesch_kincaid_grade is not None
    assert metrics.flesch_reading_ease is not None
    assert metrics.gunning_fog is not None
    assert "calibrated for English" in (metrics.note or "")


def test_a_short_document_keeps_its_counts_without_scores():
    metrics = readability.measure(sample_markdown("invalid"), language="en")

    assert 0 < metrics.word_count < readability.MIN_WORDS
    assert metrics.flesch_reading_ease is None
    assert metrics.flesch_kincaid_grade is None
    assert (
        metrics.note == f"Scores need at least 100 words; this document has {metrics.word_count}."
    )


def test_missing_nltk_data_leaves_the_scores_empty_with_the_fix(monkeypatch):
    import readability as readability_metrics

    def no_tokenizer(_text):
        raise LookupError("punkt_tab")

    monkeypatch.setattr(readability_metrics, "Readability", no_tokenizer)
    metrics = readability.measure(sample_markdown("informal"))

    assert metrics.word_count > 100
    assert metrics.flesch_reading_ease is None
    assert "npm run rfp:setup" in (metrics.note or "")


@pytest.mark.parametrize(
    ("tokens", "tier"),
    [(0, "low"), (1_999, "low"), (2_000, "medium"), (7_999, "medium"), (8_000, "high")],
)
def test_processing_cost_tiers(tokens, tier):
    assert readability.cost_tier(tokens) == tier


def test_the_sample_pdfs_are_where_the_tests_expect_them():
    assert all((PDF_DIR / name).is_file() for name in SAMPLES.values()), Path(PDF_DIR)
