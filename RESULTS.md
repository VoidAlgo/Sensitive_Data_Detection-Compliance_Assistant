# RESULTS — Honest Evaluation

This document reports measured behavior on the bundled samples and states known
limitations candidly.

## Test Suite
- **148 tests, all green** (`pytest`), `ruff check .` clean.
- Coverage spans: config/models, ingestion (+OCR trigger), rate limiter &
  rotation, detection (checksums, golden counts, dedupe, NER, contextual),
  risk classification, RAG (chunking/store/QA/corpus), compliance summary,
  redaction export, and audit logging.

## Detection Accuracy — Golden File
`data/samples/golden.txt` contains 10 planted valid entities (one per required
deterministic category) plus **one intentionally invalid Aadhaar** (fails
Verhoeff) that must be rejected.

| Metric | Value |
|--------|-------|
| Entities planted (valid) | 10 |
| True positives | 10 |
| False positives | 0 |
| False negatives | 0 |
| Invalid Aadhaar correctly rejected | ✅ |
| **Precision** | **1.00** |
| **Recall** | **1.00** |

All nine required categories are detected; deterministic PII is validated by
checksum (Verhoeff/Luhn). These numbers reflect a small, curated golden file —
they demonstrate correctness of the detectors, not population-level accuracy.

## Ingestion
- Text PDF (2 pages), scanned image-only PDF, and a CSV with fake PII all load
  with page/line/column metadata.
- The scanned PDF yields 0 extractable characters. OCR is **off by default**
  (`SDA_ENABLE_OCR=false`, since it needs the native Tesseract binary), so with
  the default settings that document is *not* read. The OCR code path itself is
  verified via a mocked Tesseract call; a real binary is installed in Docker.
- **Unreadable input is surfaced, not silently passed.** When a page yields no
  usable text, `extraction_warning()` produces a caveat that the UI shows, and the
  "no findings" result is reported as "no sensitive data **in the text that could
  be read**" rather than an all-clear. Measured: `scanned_sample.pdf` →
  `empty_pages 1/1` → warning shown.

## RAG Q&A
- Counting questions match the deterministic detector exactly.
- Out-of-scope questions are refused (no hallucination) below the cosine floor.
- Grounded answers carry page/line citations; chunks contain **no raw PII**
  (asserted on an AWS key and an email).

## Rate-Limit Rotation
- Simulated 429s rotate to the next model; RPM/TPM/RPD windows block and free
  correctly under a fake clock; `AllModelsExhausted` is raised when all capped.

## Redaction
- TXT/CSV/PDF exports contain zero raw sensitive values; PDF redaction removes the
  underlying glyphs (verified by re-extracting text from the redacted PDF).

## Docker / Deployment
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`, and `packages.txt` are
  provided. `docker compose config` validates successfully.
- **Not verified on the build machine:** a full `docker build` / `docker compose
  up` was not executed here because the Docker daemon was not running in the
  build environment. Run `docker compose up --build` locally to confirm.
- Streamlit Community Cloud deployment steps are documented in the README; the
  live URL must be filled in after deployment.

## Detection Latency (measured, `golden.txt`)

| Stage | Time | Share |
|---|---|---|
| Regex + checksum patterns | 0.00s | ~0% |
| Deterministic name cues | 0.00s | ~0% |
| spaCy NER (incl. one-time model load) | 1.46s | ~23% |
| **LLM contextual pass** | **4.91s** | **~77%** |
| Total | 6.37s | |

Deterministic detection is effectively free; latency is almost entirely the LLM
verification pass, which is the price of multilingual and confidential-content
recall. Scoping that prompt away from already-matched structured identifiers cut
it from 8.5s to 4.9s. Worst observed case is far higher (**53.7s**) when the
free-tier quota forces rotation through rate-limited models.

## Known Limitations
- **Model registry values are placeholders.** Free-tier RPM/TPM/RPD change often;
  verify at the Google rate-limits page before relying on them.
- **Phone/name detection is heuristic.** International phone formats and
  NER-derived names can miss or over-match; treat NER findings as low confidence.
- **spaCy still over-tags document titles.** After the span-hygiene filters, an
  all-caps heading ("CONFIDENTIAL EMPLOYEE & PAYMENT RECORD") is still labelled
  ORG and redacted. This is over-redaction — the safe direction for a compliance
  tool — but it is noise, not a true finding.
- **OCR is disabled by default,** so scanned PDFs are not read unless it is
  enabled and Tesseract is installed. The app warns rather than implying the file
  is clean, but recall on such documents is zero until OCR is turned on.
- **Contextual (LLM) detection depends on quota.** When all models are exhausted
  or no key is set, the contextual pass and LLM answers degrade gracefully but are
  skipped/limited.
- **Golden-file metrics are illustrative**, not a benchmark over diverse
  real-world documents.
- **OCR quality** depends on scan resolution; low-DPI scans reduce recall.
