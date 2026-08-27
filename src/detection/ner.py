"""spaCy named-entity pass for PERSON / ORG / LOCATION.

Provides contextual entities (names, organizations, places) that help the risk
model and the confidential-info pass. The spaCy model is loaded lazily and cached;
if it is not installed, detection degrades gracefully to an empty result so the
pipeline never hard-fails.
"""

from __future__ import annotations

import re

from src.detection.names import NOT_A_NAME
from src.models import EntityType, Finding
from src.redaction.masker import mask_value

_MODEL_NAME = "en_core_web_sm"
# The model is English; it can only meaningfully label Latin-script text. Skip
# entities without an ASCII letter (e.g. Tamil/Hindi), which it otherwise
# mislabels — the multilingual Gemini pass handles those instead.
_HAS_LATIN = re.compile(r"[A-Za-z]")

# spaCy is trained on prose, so on form-like "Label: value" documents it drifts:
# it swallows field labels ("PAN", "Credit Card"), runs spans across line breaks
# ("John Doe\nEmail"), and tags ordinary lowercase words ("checksum") as ORG.
# Redacting those wrecks the sanitized output — "Name:" becomes "[REDACTED:ORG]:"
# — and inflates the risk score, so each pattern is filtered out below.
_FIELD_LABEL = re.compile(r"[ \t]*:")  # entity immediately followed by a colon
_LABEL_MAP = {
    "PERSON": EntityType.PERSON,
    "ORG": EntityType.ORG,
    "GPE": EntityType.LOCATION,
    "LOC": EntityType.LOCATION,
}

_nlp = None
_load_failed = False


def _get_nlp():
    """Lazily load and cache the spaCy pipeline; return None if unavailable."""
    global _nlp, _load_failed
    if _nlp is not None or _load_failed:
        return _nlp
    try:
        import spacy

        _nlp = spacy.load(_MODEL_NAME, disable=["lemmatizer"])
    except Exception:  # noqa: BLE001 - model missing or load error → degrade
        _load_failed = True
        _nlp = None
    return _nlp


def detect_ner(text: str, max_chars: int = 100_000) -> list[Finding]:
    """Return PERSON/ORG/LOCATION findings; empty if spaCy is unavailable."""
    nlp = _get_nlp()
    if nlp is None:
        return []

    doc = nlp(text[:max_chars])
    findings: list[Finding] = []
    for ent in doc.ents:
        entity_type = _LABEL_MAP.get(ent.label_)
        if entity_type is None:
            continue
        if not _HAS_LATIN.search(ent.text):
            continue  # non-Latin (e.g. Tamil) → let the Gemini pass classify it

        value, start, end = _trim_to_first_line(ent.text, ent.start_char)
        if not _is_plausible_entity(value, text, end):
            continue

        findings.append(
            Finding(
                entity_type=entity_type,
                value_masked=mask_value(entity_type, value),
                value_raw=value,
                start=start,
                end=end,
                detector="spacy",
                confidence=0.6,
            )
        )
    return findings


def _trim_to_first_line(value: str, start: int) -> tuple[str, int, int]:
    """Clip an entity at the first line break and drop surrounding whitespace.

    On form-like documents spaCy regularly merges a value with the *next* line's
    field label ("John Doe\\nEmail"). Keeping only the first line recovers the
    real entity and keeps the character span aligned with the source text.
    """
    newline = value.find("\n")
    if newline != -1:
        value = value[:newline]
    stripped = value.strip()
    start += len(value) - len(value.lstrip())
    return stripped, start, start + len(stripped)


def _is_plausible_entity(value: str, text: str, end: int) -> bool:
    """Reject spaCy spans that are document scaffolding rather than real entities."""
    if len(value) < 2:
        return False
    # "PAN:", "Credit Card:" — a span followed by a colon is a field label, and
    # whatever follows it is the actual value (already covered by the
    # deterministic detectors).
    if _FIELD_LABEL.match(text[end : end + 2]):
        return False
    # A span still holding a colon straddles a label/value boundary.
    if ":" in value:
        return False
    # Real names/orgs are capitalized; an all-lowercase span is ordinary prose
    # ("checksum", "login password").
    if value == value.lower():
        return False
    # Shared vocabulary of known non-entities (field labels, geography words).
    return value.split()[0].lower().strip(".,;") not in NOT_A_NAME
