# Redaction & Sanitized Export System

## 1. Overview & Security Mandate

The redaction subsystem (`src/redaction/masker.py` and `src/redaction/export.py`) is responsible for sanitizing documents prior to distribution, logging, or embedding into vector indexes.

In security and privacy engineering, **redaction failure** typically arises from three flaws:
1. **Shallow Vector Coverage**: Drawing a black rectangle on a PDF while leaving the underlying font glyphs in the PDF content stream, allowing copy-paste extraction.
2. **Span-Only Omission**: Masking only the specific substring identified by a detector, leaving duplicate occurrences in headers, footers, or identity blocks unredacted.
3. **Recursive Re-redaction Artifacts**: Sequential find-and-replace passes that corrupt previously inserted redaction labels, producing malformed tokens like `[REDACTED:[REDACTED:PERSON]]`.

The platform eliminates these failure modes by enforcing centralized masking primitives, occurrence-complete single-pass regex replacement, and true binary vector glyph deletion.

---

## 2. Masking Primitives & Styles (`masker.py`)

All masking logic is centralized in `mask_value()` and `replacement_for()`, ensuring uniform obfuscation across UI tables, RAG chunks, and exported files.

```
                              [Raw Sensitive Value]
                                        │
                                        ▼
                                [mask_value() Gate]
                                        │
        ┌───────────────────────┬───────┴───────────────┬────────────────────────┐
        ▼                       ▼                       ▼                        ▼
  [Fully Masked]          [Email Mask]            [Keep-Last-4]           [Boundary Mask]
  API_KEY, PASSWORD      Preserve domain       AADHAAR, PAN, CARD,      PERSON, ORG, LOC,
  -> "********"          -> "j***@c***.com"    PHONE, IFSC, BANK_ACC    CONFIDENTIAL_INFO
                                               -> "********1234"        -> "J***e"
```

### 2.1 Entity Masking Rules
| Entity Category | Strategy | Example Transformation |
|---|---|---|
| `API_KEY`, `PASSWORD` | **Full Obfuscation** | `sk-ant-api03-...` $\rightarrow$ `********` |
| `DOB` (Date of Birth) | **Digit Masking** | `15/08/1990` $\rightarrow$ `**/**/****` |
| `EMAIL` | **Domain Preservation** | `alice.smith@acme.org` $\rightarrow$ `a***@a***.org` |
| `AADHAAR`, `VID` | **Last 4 Digits** | `9876 5432 1098` $\rightarrow$ `********1098` |
| `CREDIT_CARD` | **Last 4 Digits** | `4111 1111 1111 1234` $\rightarrow$ `************1234` |
| `PAN` | **Last 4 Chars** | `ABCDE1234F` $\rightarrow$ `******234F` |
| `BANK_ACCOUNT` | **Last 4 Digits** | `100234567890` $\rightarrow$ `********7890` |
| `IFSC` | **Last 4 Chars** | `SBIN0001234` $\rightarrow$ `*******1234` |
| `PHONE` | **Last 4 Digits** | `+91 98765 43210` $\rightarrow$ `**********3210` |
| `PERSON`, `ORG`, `LOCATION` | **Edge Preservation** | `John Doe` $\rightarrow$ `J******e` |

### 2.2 Redaction Styles (`replacement_for`)
- **`"mask"`**: Employs partial masking (e.g. `****1234`). Default for RAG chunking because it maintains grammatical and numerical context for LLM comprehension without exposing private data.
- **`"placeholder"`**: Injects semantic tokens: `[REDACTED:ENTITY_TYPE]` (e.g. `[REDACTED:AADHAAR]`, `[REDACTED:PAN]`). Preferred for enterprise compliance audits.

---

## 3. Occurrence-Complete Single-Pass Sanitization

### 3.1 The Value-Completeness Problem
Documents such as ID cards and employment contracts frequently print the same identity element across multiple sections (e.g. employee name on page 1 and the signature block on page 5). If a model-driven detector flags only one occurrence, exporting based solely on detector span offsets leaks all subsequent instances.

### 3.2 Single-Pass Alternation (`redact_all_occurrences`)
To resolve this without nested corruption:
1. All distinct detected raw values are extracted and sorted in descending order of string length.
2. A single composite regular expression is constructed using non-capturing word boundaries:
   ```python
   pattern = re.compile(rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])")
   ```
3. A lambda callback replaces matches in a **single linear pass**:
   ```python
   pattern.sub(lambda m: mapping[m.group(0)], text)
   ```
*Guarantee*: Longer values are matched before substrings, alphanumeric boundary guards prevent accidental replacement inside unrelated words, and newly inserted replacement tokens are never re-evaluated.

---

## 4. Multi-Format Exporters (`export.py`)

### 4.1 Plain Text Sanitization (`redact_txt`)
Runs `redact_all_occurrences` over `Document.text` using the configured style (`mask` or `placeholder`). Output is encoded as UTF-8 text.

### 4.2 Tabular CSV Sanitization (`redact_csv`)
- Bypasses raw string replacement to prevent breaking comma-delimited structure.
- Accesses the underlying `DataFrame` from `Document.metadata['dataframe']`.
- Iterates across each column and cell using `Series.map()`, applying string substitution only to matching cell values while preserving column headers intact:
  ```python
  def _clean(cell: object) -> object:
      text = str(cell)
      for raw, repl in replacements:
          if raw and raw in text:
              text = text.replace(raw, repl)
      return text
  ```
- Serializes the clean DataFrame back to CSV bytes via an in-memory `StringIO` buffer.

### 4.3 True Vector PDF Redaction (`redact_pdf`)
Many web redaction tools draw black boxes onto a canvas over text. This is a severe security vulnerability because the underlying glyph stream remains present in the PDF object tree.

PyMuPDF (`fitz`) provides true cryptographic-grade redaction:
```python
with fitz.open(stream=raw_bytes, filetype="pdf") as pdf:
    for page in pdf:
        for value in raw_values:
            for rect in page.search_for(value):
                page.add_redact_annot(rect, fill=(0, 0, 0))
        page.apply_redactions()  # Permanently purges underlying glyphs
    pdf.save(out)
```
- `page.search_for(value)` locates bounding rectangles (`fitz.Rect`) for every occurrence.
- `page.add_redact_annot()` marks the bounding box with solid black fill.
- `page.apply_redactions()` **deletes the underlying text glyphs and drawing commands from the PDF structure**.
*Verification*: The test suite (`tests/test_redaction.py`) extracts text from the redacted PDF and asserts that zero occurrences of sensitive strings exist.

---

## 5. Cache Invalidation Fingerprinting (`findings_fingerprint`)

### Decision D53: Fingerprinted Index Invalidation
Vector indices and RAG chunks are persisted on disk keyed by `doc_id` (`sha256(raw_bytes)[:16]`). However, if a developer upgrades a detector or fixes a PII leak, the file's raw bytes do not change. Loading the pre-existing index from disk would serve chunks redacted under outdated rules, leaking newly supported PII.

To prevent this:
```python
def findings_fingerprint(findings: list[Finding]) -> str:
    payload = sorted((f.start, f.end, f.entity_type.value) for f in findings)
    digest = hashlib.sha256(repr(payload).encode("utf-8"))
    return digest.hexdigest()[:16]
```
`FaissStore.matches()` verifies both the `doc_id` and the `findings_fingerprint`. If a detection improvement changes any finding spans or categories, the cache is recognized as stale and automatically rebuilt.
