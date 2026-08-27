"""Regression tests for the two defects an end-to-end evaluation surfaced.

1. A scanned/image-only PDF extracted 0 characters, produced 0 findings, and the
   UI reported "No sensitive data detected" at Low risk — a false all-clear on a
   document the tool never actually read.
2. spaCy drifted badly on form-like "Label: value" documents: it tagged field
   labels ("PAN", "Credit Card"), ran spans across line breaks ("John Doe\\nEmail"),
   and flagged ordinary lowercase words ("checksum") as ORG. Redacting those turned
   "Name:" into "[REDACTED:ORG]:" and inflated the risk score.
"""

from __future__ import annotations

from pathlib import Path

from src.config import Settings
from src.detection.ner import _is_plausible_entity, _trim_to_first_line
from src.ingestion.loaders import extraction_warning, load_document
from src.models import Document, Segment

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


# --- 1. Unreadable documents must be flagged, never reported as clean ---------
def test_scanned_pdf_is_flagged_as_unreadable() -> None:
    settings = Settings(enable_ocr=False, _env_file=None)
    doc = load_document(
        "scanned_sample.pdf", (SAMPLES / "scanned_sample.pdf").read_bytes(), settings
    )
    assert doc.text.strip() == ""
    warning = extraction_warning(doc, settings)
    assert warning is not None
    assert "do NOT mean the file is clean" in warning.lower() or "not mean" in warning.lower()
    assert "OCR" in warning


def test_partially_unreadable_pdf_reports_page_count() -> None:
    settings = Settings(enable_ocr=False, _env_file=None)
    doc = Document(
        doc_id="x",
        filename="mixed.pdf",
        file_type="pdf",
        text="Readable page text that is long enough to pass the threshold.",
        segments=[Segment(text="Readable page text", page=1)],
        page_count=3,
        empty_pages=2,
    )
    warning = extraction_warning(doc, settings)
    assert warning is not None
    assert "2 of 3 pages" in warning


def test_readable_document_has_no_warning() -> None:
    settings = Settings(enable_ocr=False, _env_file=None)
    doc = load_document("golden.txt", (SAMPLES / "golden.txt").read_bytes(), settings)
    assert extraction_warning(doc, settings) is None


def test_empty_pages_counted_for_scanned_pdf() -> None:
    settings = Settings(enable_ocr=False, _env_file=None)
    doc = load_document(
        "scanned_sample.pdf", (SAMPLES / "scanned_sample.pdf").read_bytes(), settings
    )
    assert doc.empty_pages == doc.page_count > 0


# --- 2. spaCy span hygiene ---------------------------------------------------
def test_span_trimmed_at_line_break() -> None:
    # spaCy merged a name with the next line's field label.
    value, start, end = _trim_to_first_line("John Doe\nEmail", 10)
    assert value == "John Doe"
    assert (start, end) == (10, 18)


def test_field_label_followed_by_colon_is_rejected() -> None:
    text = "PAN: ABCDE1234F"
    assert not _is_plausible_entity("PAN", text, 3)


def test_span_containing_colon_is_rejected() -> None:
    text = "AWS Access Key: AKIAIOSFODNN7EXAMPLE"
    assert not _is_plausible_entity("AWS Access Key: AKIAIOSFODNN7EXAMPLE", text, len(text))


def test_lowercase_prose_word_is_rejected() -> None:
    text = "rejected by checksum rules"
    assert not _is_plausible_entity("checksum", text, 20)


def test_known_non_entity_word_is_rejected() -> None:
    text = "Tamil Nadu is a state"
    assert not _is_plausible_entity("Tamil Nadu", text, 10)


def test_genuine_entity_still_accepted() -> None:
    text = "Acme Corporation signed the agreement"
    assert _is_plausible_entity("Acme Corporation", text, 16)


def test_genuine_person_name_still_accepted() -> None:
    text = "Rajesh Kumar attended the meeting"
    assert _is_plausible_entity("Rajesh Kumar", text, 12)


# --- End-to-end: field labels survive redaction ------------------------------
def test_field_labels_are_not_redacted() -> None:
    """The sanitized copy must stay readable — labels are not sensitive."""
    from src.detection.engine import run_detection
    from src.redaction.masker import redact_all_occurrences

    settings = Settings(_env_file=None)
    doc = load_document("golden.txt", (SAMPLES / "golden.txt").read_bytes(), settings)
    findings = run_detection(doc, None, settings)  # no LLM → deterministic + spaCy
    redacted = redact_all_occurrences(doc.text, findings, style="placeholder")

    for label in ["Name:", "PAN:", "Credit Card:", "Email:", "Aadhaar:"]:
        assert label in redacted, f"field label {label!r} was wrongly redacted"
    assert "checksum" in redacted, "ordinary prose word was wrongly redacted"
