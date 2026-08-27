"""Regression tests: a cached vector index must not outlive the findings that masked it.

Indexes are persisted keyed by document hash so re-uploads are instant. But the
hash only covers the document *bytes* — not the detection results that decided
what got masked before embedding. An end-to-end evaluation caught the consequence:
after a detector fix, the on-disk index for an already-seen document kept serving
chunk text masked under the OLD rules. In the dangerous direction, a fix that
detects a *new* kind of PII would leave that PII exposed in every existing index.
"""

from __future__ import annotations

from src.config import Settings
from src.models import Document, EntityType, Finding, Segment
from src.rag import qa
from src.redaction.masker import findings_fingerprint


def _doc() -> Document:
    text = "Contact alice@example.com about the payment card on file.\nRef 12345."
    return Document(
        doc_id="staletest01",
        filename="d.txt",
        file_type="txt",
        text=text,
        segments=[Segment(text=text, page=1, line=1)],
        page_count=1,
    )


def _email_finding(doc: Document) -> Finding:
    value = "alice@example.com"
    start = doc.text.index(value)
    return Finding(
        entity_type=EntityType.EMAIL,
        value_masked="a****@example.com",
        value_raw=value,
        start=start,
        end=start + len(value),
        detector="regex",
        confidence=1.0,
    )


# --- fingerprint semantics ---------------------------------------------------
def test_fingerprint_is_stable_for_same_findings() -> None:
    doc = _doc()
    a, b = _email_finding(doc), _email_finding(doc)
    assert findings_fingerprint([a]) == findings_fingerprint([b])


def test_fingerprint_is_order_independent() -> None:
    doc = _doc()
    f1 = _email_finding(doc)
    f2 = Finding(EntityType.PHONE, "***", "12345", 62, 67, "regex", 1.0)
    assert findings_fingerprint([f1, f2]) == findings_fingerprint([f2, f1])


def test_fingerprint_changes_when_a_new_finding_appears() -> None:
    doc = _doc()
    f1 = _email_finding(doc)
    f2 = Finding(EntityType.PHONE, "***", "12345", 62, 67, "regex", 1.0)
    assert findings_fingerprint([f1]) != findings_fingerprint([f1, f2])


# --- cache invalidation ------------------------------------------------------
def test_improved_detection_rebuilds_the_index(tmp_path) -> None:
    """The critical case: newly-detected PII must not survive in a cached index."""
    doc = _doc()
    settings = Settings(index_dir=str(tmp_path / "idx"), _env_file=None)

    # Round 1: detector only knows about the email. "12345" stays in the clear.
    store = qa.build_index(doc, [_email_finding(doc)], settings=settings)
    assert any("12345" in c.text for c in store._chunks)

    # Round 2: an improved detector now also finds the reference number.
    better = [_email_finding(doc), Finding(EntityType.PHONE, "***", "12345", 62, 67, "regex", 1.0)]
    store2 = qa.build_index(doc, better, settings=settings)
    assert not any("12345" in c.text for c in store2._chunks), (
        "stale index served text masked under the old findings — PII left exposed"
    )


def test_unchanged_findings_still_reuse_the_cache(tmp_path) -> None:
    """Invalidation must not defeat the point of caching."""
    doc = _doc()
    settings = Settings(index_dir=str(tmp_path / "idx"), _env_file=None)
    findings = [_email_finding(doc)]

    qa.build_index(doc, findings, settings=settings)
    meta = (tmp_path / "idx" / f"{doc.doc_id}.json").read_text(encoding="utf-8")
    mtime = (tmp_path / "idx" / f"{doc.doc_id}.faiss").stat().st_mtime

    qa.build_index(doc, findings, settings=settings)
    assert (tmp_path / "idx" / f"{doc.doc_id}.json").read_text(encoding="utf-8") == meta
    assert (tmp_path / "idx" / f"{doc.doc_id}.faiss").stat().st_mtime == mtime


def test_legacy_index_without_fingerprint_is_rebuilt(tmp_path) -> None:
    """Indexes written before fingerprinting must be treated as stale, not trusted."""
    import json

    doc = _doc()
    settings = Settings(index_dir=str(tmp_path / "idx"), _env_file=None)
    findings = [_email_finding(doc)]
    qa.build_index(doc, findings, settings=settings)

    # Rewrite metadata in the old bare-list format.
    meta_path = tmp_path / "idx" / f"{doc.doc_id}.json"
    current = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_path.write_text(json.dumps(current["chunks"]), encoding="utf-8")

    from src.rag.store import FaissStore

    stale = FaissStore(doc.doc_id, str(tmp_path / "idx"))
    assert stale.exists()
    assert not stale.matches(findings_fingerprint(findings))
