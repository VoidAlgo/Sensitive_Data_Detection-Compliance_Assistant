# Ingestion & OCR Processing Pipeline

## 1. Overview & Objectives

The ingestion layer (`src/ingestion/loaders.py` and `src/ingestion/ocr.py`) serves as the foundational data boundary of the application. It transforms raw file payloads into a normalized domain representation (`Document`) while preserving physical spatial coordinates (page numbers, line indices, tabular column associations, and absolute character offsets).

A mission-critical principle of this module is **truthful ingestion**: the system actively prevents "silent extraction failures" where an unreadable or scanned document produces an empty string, which downstream compliance engines could mistakenly interpret as a "clean" document with zero risk.

---

## 2. Supported Formats & Extraction Strategies

```
               ┌────────────────────────────────────────────────────────┐
               │              Raw File Bytes + Filename                 │
               └──────────────────────────┬─────────────────────────────┘
                                          │
                                          ▼
                             [load_document() Dispatch]
                                          │
            ┌─────────────────────────────┼─────────────────────────────┐
            ▼                             ▼                             ▼
    [PDF Processing]              [TXT Processing]              [CSV Processing]
    - PyMuPDF (fitz)              - charset-normalizer          - pandas DataFrame
    - Page-by-page scan           - Line-by-line chunking       - Row serialization
    - Sparsity analysis           - Absolute line indexing      - Column metadata
            │                                                           │
     [needs_ocr?]                                                       │
      YES / NO                                                          │
      ├─ Yes & OCR enabled ──▶ [Tesseract OCR Fallback]                 │
      └─ No / OCR disabled ──▶ [Native Text Stream]                     │
            │                             │                             │
            └─────────────────────────────┼─────────────────────────────┘
                                          │
                                          ▼
                         [_assemble() Coordinate Sync]
                    - Compute doc_id: sha256(bytes)[:16]
                    - Calculate cumulative char_offset
                    - Populate Segment[] and Document
                                          │
                                          ▼
                       [extraction_warning() Evaluation]
                    - Check if empty_pages > 0 or text == ""
```

### 2.1 PDF Ingestion (`_load_pdf`)
- Powered by **PyMuPDF (`fitz`)**, selected for its high performance and native vector text extraction.
- Ingestion occurs iteratively page by page (`page.get_text("text")`), producing a `Segment` per page with 1-based page indexing.
- Native PDF text streams preserve character glyphs, line breaks, and font layouts.

### 2.2 Plain Text Ingestion (`_load_txt`)
- Built on **`charset-normalizer`** (`from_bytes(raw_bytes).best()`), eliminating character encoding corruption across Windows-1252, ISO-8859, UTF-8, and UTF-16 documents.
- Falls back to `utf-8` with replacement character handling on non-standard encodings.
- Segments are created per line (`content.splitlines()`), maintaining 1-based line counters for exact source referencing.
- Invariant guard: empty text files emit a single empty segment (`Segment(text="", line=1)`) to preserve non-null contract requirements.

### 2.3 CSV Tabular Ingestion (`_load_csv`)
- Ingested via **Pandas** using `dtype=str, keep_default_na=False` to ensure numeric codes (such as Aadhaar, Credit Cards, PIN codes) do not suffer from floating-point conversion, truncation, or string conversion to `"NaN"`.
- Emits a schema header segment with column names marked as `column="__header__"`.
- Each subsequent record row is serialized as a delimited key-value string:
  ```text
  col1=val1 | col2=val2 | col3=val3
  ```
- The original DataFrame is retained inside `Document.metadata['dataframe']`, providing downstream redaction exporters the structured table necessary for surgical in-memory cell replacement.

---

## 3. OCR Architecture & Scanned Document Fallback

### 3.1 Heuristic Sparsity Trigger (`needs_ocr`)
OCR is computationally expensive and introduces character recognition noise. The system uses a pure, deterministically tested heuristic to decide when OCR is necessary:
```python
def needs_ocr(extracted_text: str, min_chars: int) -> bool:
    return len(extracted_text.strip()) < min_chars
```
The threshold defaults to `ocr_min_chars_per_page = 50` (configurable via `SDA_OCR_MIN_CHARS_PER_PAGE`). If a PDF page yields fewer than 50 non-whitespace characters, it is classified as a scanned image or embedded raster graphic.

### 3.2 Dynamic OCR Execution (`ocr_pdf_page`)
When OCR is triggered and enabled (`settings.enable_ocr = True`):
1. PyMuPDF renders the page to an in-memory pixmap at **200 DPI**:
   ```python
   pix = page.get_pixmap(dpi=dpi)
   ```
2. The pixmap bytes are loaded into a Pillow image (`PIL.Image.open(io.BytesIO(...))`).
3. `pytesseract.image_to_string` performs optical character recognition.
4. **Content Gain Validation**: OCR text is only adopted if it yields strictly more characters than the native extraction:
   ```python
   if len(ocr_text.strip()) > len(text.strip()):
       text = ocr_text
       used_ocr = True
   ```
5. **Graceful Fault Tolerance**: Dependencies (`pytesseract`, `PIL`) and the native Tesseract binary are lazily imported and encapsulated in `OcrUnavailableError`. If Tesseract is not installed on the host system, the process does not crash; it degrades gracefully by returning whatever native text was extracted.

---

## 4. Engineering Decisions & Compliance Safeguards

### 4.1 Decision D50: Never Report an Unread Document as Clean
- **Context**: In compliance scanning, the most catastrophic failure is a false negative—declaring a confidential file "safe" when it could not be read.
- **Problem**: When a user uploaded a scanned PDF without OCR enabled (or without Tesseract installed), native extraction returned zero characters. The detection engine found zero findings, resulting in a green "Low Risk: No sensitive data detected" badge.
- **Solution**:
  - `Document` tracks `empty_pages`: the count of pages that remained below `min_chars_per_page`.
  - `extraction_warning(document, settings)` computes a prominent warning banner when unreadable pages exist.
  - The UI downgrades "clean" notices to: *"No sensitive data was found in the text that could be read"*, explicitly preventing false compliance sign-offs.

### 4.2 Spatial Geometry Synchronization (`_assemble`)
Detectors operate across the concatenated document string (`Document.text`) to find entities that span line or segment breaks. To locate matches in the original document, `_assemble` calculates cumulative character offsets:
```python
offset = 0
for seg in segments:
    seg.char_offset = offset
    parts.append(seg.text)
    offset += len(seg.text) + 1  # accounts for '\n' delimiter
```
This enables `src/detection/engine.py` to map any character interval `[start, end]` back to the specific physical page, line, or CSV column in $O(\log N)$ or fast linear scan time.
