# Detection Engine & Hybrid AI/Deterministic Mechanics

## 1. Architectural Philosophy: Deterministic vs. Probabilistic

The sensitive data detection subsystem (`src/detection/`) applies a strict hybrid design principle:
- **Deterministic Checksums & Regex**: Never trust an LLM to reliably locate or validate structured PII. High-stakes identifiers (Aadhaar, Credit Cards, PAN, IFSC, API Keys) follow mathematical invariants or standard syntaxes. Validating them with algorithms eliminates false positives and ensures 100% reproducible precision.
- **Structural Heuristics**: Detect person names from document geometry, field labels (`Name:`, `Account Holder:`), kinship relations (`S/O`, `D/O`), and salutations without incurring transformer inference overhead.
- **Statistical Named Entity Recognition (NER)**: Leverage a lightweight English NLP pipeline (spaCy `en_core_web_sm`) for prose-level entities.
- **Probabilistic LLM Verification**: Deploy Gemini (or local Ollama) strictly where regex and static NER fail: fuzzy confidential business information (NDAs, M&A, intellectual property, financial projections) and **multilingual / Indic script entities** (such as names and locations written in Tamil, Hindi, or Bengali).

```mermaid
flowchart TD
    RawDoc[Document Text & Segments] --> Stage1[Stage 1: Deterministic Patterns]
    RawDoc --> Stage2[Stage 2: Structural Names]
    RawDoc --> Stage3[Stage 3: spaCy NER]
    RawDoc --> Stage4[Stage 4: LLM Contextual Sweep]

    subgraph Deterministic["Deterministic Validators"]
        Stage1 --> Verhoeff[Aadhaar: Verhoeff D5 Checksum]
        Stage1 --> Luhn[Cards: Luhn Mod-10 Checksum]
        Stage1 --> RegexPats[PAN / IFSC / Phone / API Keys / Passwords]
        Stage2 --> Cues[Labels / S/O Relations / Salutations / Addressees]
    end

    subgraph Statistical["Statistical NLP"]
        Stage3 --> SpacyClean[spaCy en_core_web_sm + Span Hygiene Filters]
    end

    subgraph Probabilistic["Probabilistic AI"]
        Stage4 --> Gemini[Gemini Multi-lingual Pass at Temp=0]
        Gemini --> HallucinationGuard[Whitespace-Tolerant Snippet Verifier]
    end

    Stage1 & Stage2 & Stage3 & Stage4 --> PostPos[Positional Address Number Detector]
    PostPos --> Dedupe[Deduplication Engine]
    
    subgraph EngineDedupe["Trust-Ranked Resolution"]
        Dedupe --> RankSort[Sort: Trust Rank > Span Length > Confidence]
        Dedupe --> CoordinateMap[Map Char Offsets to Page / Line / Column]
    end

    RankSort --> OutputFindings[Validated Finding[] Collection]
```

---

## 2. Mathematical Checksum & Pattern Validators (`patterns.py`)

### 2.1 Verhoeff Dihedral Group Checksum (Aadhaar)
The Indian 12-digit UID (Aadhaar) uses the **Verhoeff algorithm**, based on permutations in the dihedral group $D_5$ (symmetries of a regular pentagon).
- Prevents single-digit transcription errors and adjacent transposition errors.
- Implemented via lookup matrices:
  - Multiplication table `_VERHOEFF_D[10][10]`
  - Permutation table `_VERHOEFF_P[8][10]`
```python
def verhoeff_check(number: str) -> bool:
    digits = [c for c in number if c.isdigit()]
    if len(digits) != 12:
        return False
    check = 0
    for i, d in enumerate(reversed([int(x) for x in digits])):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][d]]
    return check == 0
```
*Golden Benchmark Verification*: An intentionally corrupted Aadhaar in `data/samples/golden.txt` fails `verhoeff_check` and is deterministically dropped.

### 2.2 Luhn Modulo-10 Checksum (Payment Cards)
Card numbers (12 to 19 digits) are validated using the **Luhn algorithm**:
- Double every second digit from the right; if $2d > 9$, subtract 9; sum all digits; valid if $\sum \equiv 0 \pmod{10}$.
- **Card Network Identification (`card_network`)**: Inspects Issuer Identification Number (IIN) prefixes:
  - Visa: `4...`
  - American Express: `34...`, `37...`
  - Mastercard: `51-55...`, `2221-2720...`
  - Discover: `60...`, `65...`, `6011...`

### 2.3 Other Deterministic Patterns in `PATTERN_SPECS`
- **PAN**: `(?i)[A-Z]{5}[0-9]{4}[A-Z]` (Indian Permanent Account Number, case-insensitive).
- **IFSC**: `(?i)^[A-Z]{4}0[A-Z0-9]{6}$` (Indian Financial System Code).
- **Phone Numbers**:
  - Primary (India): `(?:\+91[\-\s]?)?[6-9]\d{9}`
  - International: `\+\d{1,3}(?:[\s-]?\d){6,12}` with mandatory `+` country prefix to avoid digit-run false alarms.
- **PIN Codes (Postal Index Number)**:
  - `pincode-keyword`: Labels like `PIN:`, `Pincode:`, `PIN Code:` followed by 6 digits.
  - `pincode-suffix`: Indian address format ending in `"<State> - NNNNNN"`.
- **API Keys & Secrets**:
  - AWS Access Key: `\bAKIA[0-9A-Z]{16}\b`
  - GitHub Personal Access Token: `\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,255}\b`
  - OpenAI Key: `\bsk-[A-Za-z0-9]{32,}\b`
  - JWT Tokens: `\beyJ[A-Za-z0-9-_]+\.eyJ[A-Za-z0-9-_]+\.[A-Za-z0-9-_+/=]+\b`
  - Assigned Secrets & Passwords: Key-value patterns detecting `api_key = "..."`, `password: '...'`, etc.

---

## 3. Structural Person Name Extraction (`names.py`)

English NER models frequently fail on Indian names (missing `"Makilesh M"` or `"Pranav R"`). Rather than loading heavy multi-gigabyte models, `names.py` exploits structural document cues:

1. **Labeled Fields (`_LABELED`)**:
   Matches headers like `Full Name:`, `Account Holder:`, `Card Holder:`, `Employee Name:`, `Beneficiary Name:`, `Father's Name:` followed by capitalized word tokens.
2. **Kinship & Relationship Prefixes (`_RELATION`)**:
   Captures patronymic/matronymic lines ubiquitous in Indian IDs, tax forms, and land registries:
   `\b(?:s/o|d/o|w/o|c/o)[ \t]*[:\-]?[ \t]*(<NAME>)` (Son of, Daughter of, Wife of, Care of).
3. **Honorific Salutations (`_SALUTATION`)**:
   Captures names preceded by `Mr.`, `Mrs.`, `Ms.`, `Dr.`, `Shri`, `Smt.`, `Sri`, `Kum`, `Prof.`.
4. **Addressee Blocks (`_ADDRESSEE_LINE`, `_ADDRESSEE_INLINE`)**:
   Identifies recipients on lines following `To:`, `Bill To:`, or `Ship To:`.
5. **Rejection Vocabulary (`NOT_A_NAME`)**:
   A shared lexicon of administrative terms (`enrolment`, `signature`, `authority`, `aadhaar`, `district`, `tamil`, `nadu`) prevents document scaffolding from being classified as person names.

---

## 4. spaCy NER & Span Hygiene Filters (`ner.py`)

`src/detection/ner.py` runs `en_core_web_sm` lazily to extract `PERSON`, `ORG`, and `LOCATION` (`GPE`, `LOC`). Because standard NER is trained on news prose, it tends to degrade on structured forms.

### Decision D51: Structural Span Hygiene
To eliminate false alarms without maintaining brittle stop-lists, four structural filters are enforced:
1. **Newline Clipping (`_trim_to_first_line`)**: Drops trailing lines when spaCy greedily swallows `John Doe\nEmail: ...`.
2. **Colon & Label Rejection (`_FIELD_LABEL`)**: Any candidate immediately followed by a colon (e.g. `Credit Card:`, `PAN:`) is rejected—it is a document field label, not an entity.
3. **Lowercase Rejection**: Real entities are capitalized. All-lowercase spans (`checksum`, `password`) are dropped.
4. **Latin-Script Requirement (`_HAS_LATIN`)**: Strips non-Latin characters from spaCy, delegating Indic scripts cleanly to Gemini.

---

## 5. Multilingual LLM Verification Pass (`llm_contextual.py`)

The final detection stage passes the document text to Gemini (or Ollama) at `temperature=0.0` with token-efficient prompt engineering.

### 5.1 Scope Optimization (Decision D52)
The prompt explicitly instructs the LLM:
> *"DO NOT list structured identifiers — Aadhaar/PAN/VID numbers, credit cards, bank accounts, emails, phone numbers, API keys... Those are already matched deterministically."*
This prevents the LLM from burning output tokens re-discovering known regex findings, cutting contextual pass latency from **8.5s to 4.9s (~42% reduction)**.

### 5.2 Anti-Hallucination Verbatim Guard (`_locate_snippet`)
An LLM may invent plausible text. The engine guarantees that **every finding exists verbatim in the source document**:
1. Performs exact search: `text.find(snippet)`.
2. If exact search fails due to model-induced newline normalization, applies **whitespace-tolerant character normalization**:
   - Normalizes runs of whitespace while maintaining an index map `norm_to_orig` back to original source offsets.
   - Any snippet that cannot be grounded in the text is immediately discarded.

### 5.3 JSON Truncation Recovery (Decision D36)
- Model outputs are parsed with `strict=False` so literal unescaped newlines in multi-line NDA quotes do not throw `JSONDecodeError`.
- If the token limit truncates the JSON array mid-response, a salvage regex (`\{[^{}]*\}`) parses all complete flat objects, rescuing findings that would otherwise be lost.

---

## 6. Orchestration, Ranking & Deduplication (`engine.py`)

When multiple detectors fire on overlapping text spans, `engine.py` orchestrates resolution.

### 6.1 Trust-Rank vs. Span Length (Critical Fix D35)
Earlier implementations sorted overlaps primarily by span length. This introduced a critical vulnerability: a broad, low-confidence spaCy span (`email=alice@example.com` classified as `ORG`) would swallow and delete the precise inner `EMAIL` regex finding.

The deduplication engine sorts candidates by **Trust Rank First**:
$$\text{Sort Key} = (\text{Trust Rank}, \text{Span Length}, \text{Confidence})$$

```python
_DETECTOR_RANK = {
    "verhoeff": 5, "luhn": 5, "aws-key": 5, "github-token": 5,
    "regex": 4, "keyword-proximity": 4, "vid-keyword": 4, "dob-keyword": 4,
    "pincode-keyword": 4, "pincode-suffix": 4, "openai-key": 4, "jwt": 4,
    "assigned-secret": 4, "assigned-password": 4,
    "name-label": 3, "name-relation": 3, "name-salutation": 3,
    "name-addressee": 3, "address-number": 3,
    "spacy": 2,
    "llm": 1,
}
```
*Outcome*: A deterministic regex (Rank 4) strictly defeats a generic spaCy match (Rank 2). Equal-rank conflicts (e.g. Credit Card vs Aadhaar) fall back to span length, correctly allowing a 16-digit card to take precedence over an embedded 12-digit number.

### 6.2 Positional Trailing Address Numbers (Decision D49)
Indian ID cards frequently place the age or house door number immediately following the father's name: `S/O: Marimuthu, 57, ...`.
`_detect_trailing_address_numbers` inspects the text slice immediately following any verified `PERSON` entity (regardless of whether found by regex, spaCy, or Tamil LLM extraction) and captures trailing 1-to-3 digit numbers as `LOCATION` entities.
