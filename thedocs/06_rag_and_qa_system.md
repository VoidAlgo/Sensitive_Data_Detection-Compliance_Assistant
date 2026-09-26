# Retrieval-Augmented Generation (RAG) & Semantic Q&A System

## 1. Architectural Philosophy & Privacy Invariants

The RAG subsystem (`src/rag/`) enables users to interrogate documents through grounded, cited natural language queries. 

Unlike conventional enterprise RAG architectures, this system enforces **Privacy-by-Design Vector Invariants**:
1. **Zero Raw PII in Vector Space**: Document chunks are **masked before embedding**. Raw passwords, payment cards, or identity numbers never enter the FAISS index or embedding vectors.
2. **Deterministic Query Dispatch**: Counting queries (*"How many PAN cards are present?"*) and inventory queries (*"What PII is in this document?"*) are **short-circuited directly to deterministic detector findings**. The LLM is never permitted to guess or hallucinate numerical counts.
3. **Strict Grounding & Refusal Floor**: Queries with weak semantic similarity are rejected at a configurable cosine floor ($\text{score} < 0.30$). The assistant explicitly refuses rather than generating plausible hallucinations.

```mermaid
flowchart TD
    UserQ[User Question] --> Classifier{Is Question Counting or Inventory?}
    
    subgraph DeterministicDispatch["Deterministic Short-Circuit"]
        Classifier -->|Yes: Count / Inventory| FindingsLookup[Lookup Counts from Finding[] Engine]
        FindingsLookup --> DirectAnswer[Return Exact Deterministic Answer]
    end

    subgraph HybridRetrieval["Hybrid Retrieval Pipeline"]
        Classifier -->|No: Semantic Query| QueryEmbed[Embed Question via Local MiniLM]
        QueryEmbed --> DenseSearch[FAISS Dense Search IndexFlatIP]
        UserQ --> SparseSearch[BM25 Lexical Inverted Index]
        DenseSearch & SparseSearch --> RRF[Reciprocal Rank Fusion - RRF k=60]
        RRF --> CandidatePool[Top Candidate Pool]
        CandidatePool --> RerankerCheck{Is Cross-Encoder Enabled?}
        RerankerCheck -->|Yes| Reranker[CrossEncoder: MiniLM on CPU / BGE-M3 on GPU]
        RerankerCheck -->|No| ThresholdGate
        Reranker --> ThresholdGate{Cosine Similarity ≥ rag_min_score 0.30?}
    end

    subgraph AnswerSynthesis["Grounded Synthesis & Provenance"]
        ThresholdGate -->|No & Large Doc| Refuse[Refuse: Not enough information in document]
        ThresholdGate -->|Yes or Small Doc| Synth[Gemini / Ollama Synthesis with Page/Line Context]
        Synth --> Citations[Generate Page/Line Citations]
        Synth --> FallbackCheck{LLM Quota Exhausted?}
        FallbackCheck -->|Yes| RawContext[Degrade Gracefully: Display Masked Chunks]
    end

    DirectAnswer --> FinalResult[QAResult Object]
    Citations --> FinalResult
    Refuse --> FinalResult
    RawContext --> FinalResult
```

---

## 2. Pre-Redacted Sentence Chunking (`chunker.py`)

Chunking must satisfy two conflicting requirements: preserve sentence semantics without breaking line/page coordinates, and redact all sensitive items.

1. **Sentence Boundary Preservation**:
   Splits text along standard sentence boundaries:
   ```python
   _SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
   ```
2. **Greedy Token Packing**:
   - Sentences are packed into chunks up to `chunk_size` (~500 tokens).
   - Adjacent chunks maintain a `chunk_overlap` (~50 tokens, ~10%) to prevent context fragmentation at boundaries.
3. **Immediate Pre-Redaction**:
   Before a chunk is emitted, it is sanitized via `redact_text(chunk_text, findings, style="mask")`:
   ```python
   Chunk(
       chunk_id=f"c{i + 1}",
       text=redact_text(raw_chunk_text, relevant_findings),
       page=seg.page,
       line=seg.line,
   )
   ```
*Guarantee*: If the FAISS vector index `.faiss` or metadata `.json` is inspected on disk, no unmasked sensitive values exist.

---

## 3. Hybrid Retrieval: Dense Embeddings + BM25 Lexical + RRF

Standard dense embeddings struggle with exact token matches (e.g. acronyms like `"IFSC"`, `"PAN"`, or internal employee codes like `"EMP10042"`), while sparse search fails on semantic paraphrasing. The system implements a fused hybrid retrieval model.

### 3.1 Local Dense Embeddings (`embeddings.py`)
- Employs **`sentence-transformers/all-MiniLM-L6-v2`** running locally.
- Produces 384-dimensional dense vectors.
- Embeddings are unit-normalized ($L_2$ norm = 1.0), so FAISS `IndexFlatIP` (Inner Product) calculates exact **Cosine Similarity**:
  $$\text{Cosine}(u, v) = u \cdot v$$
- Embeddings are generated locally, consuming **zero cloud Gemini API tokens**.

### 3.2 In-House BM25 Sparse Search (`lexical.py`)
A self-contained Okapi BM25 implementation written using Python standard library primitives:
- Lowercase alphanumeric tokenizer (`[a-z0-9]+`).
- Term frequency (TF) and inverse document frequency (IDF) with Robertson-Spärck Jones smoothing:
  $$\text{IDF}(t) = \ln \left( 1 + \frac{N - n(t) + 0.5}{n(t) + 0.5} \right)$$
- Length-penalized score:
  $$\text{Score}(D, Q) = \sum_{t \in Q} \text{IDF}(t) \cdot \frac{f(t, D) \cdot (k_1 + 1)}{f(t, D) + k_1 \cdot \left( 1 - b + b \cdot \frac{|D|}{\text{avgdl}} \right)}$$
  *(Parameters: $k_1 = 1.5, b = 0.75$)*.

### 3.3 Reciprocal Rank Fusion (RRF)
Combines the ranked outputs of dense and sparse search without requiring score calibration or normalization:
$$\text{RRF Score}(d) = \sum_{m \in \{\text{dense}, \text{sparse}\}} \frac{1}{k + \text{rank}_m(d)}$$
Where $k = 60$ (configurable via `rrf_k`). The final rank order determines candidate selection, while retaining the dense cosine similarity for grounding threshold checks.

---

## 4. Hardware-Aware Cross-Encoder Reranker (`reranker.py`)

For large, complex, or noisy documents, the bi-encoder candidate pool can be re-ranked with full cross-attention.

- **Opt-In Architecture**: Default is `enable_reranker = False` to conserve memory on free-tier deployments.
- **Dynamic Hardware Model Selection**:
  - **CPU Deployment**: Loads `cross-encoder/ms-marco-MiniLM-L-6-v2` (lightweight, ~80MB).
  - **CUDA GPU Auto-Upgrade**: Detects `torch.cuda.is_available()` and upgrades to `BAAI/bge-reranker-v2-m3` (multilingual, state-of-the-art accuracy).
- **Graceful Fault-Tolerance**: If PyTorch or model weights fail to load, the system falls back to bi-encoder order without crashing.

---

## 5. Grounded Q&A Execution & Hallucination Guardrails (`qa.py`)

### 5.1 Deterministic Short-Circuiting
- **Counting Questions (`_try_counting`)**: Questions matching `how many`, `count of`, or `number of` inspect `summarize_counts(findings)`. For example, *"How many credit cards are in this file?"* directly returns:
  > *"There is 1 credit card finding in this document."*
- **Inventory Questions (`_try_inventory`)**: Questions asking *"What sensitive data is stored here?"* return the aggregated findings inventory directly.

### 5.2 Cosine Grounding Floor & Small-Document Fallback
- Candidates must achieve a cosine similarity $\ge \text{rag\_min\_score}$ (default `0.30`).
- If no retrieved chunk clears the floor, the system refuses:
  > *"I don't have enough information in this document to answer that."*
- **Small-Document Exception**: When a document contains few total chunks ($\le \text{top\_k}$), semantic cosine similarity can falsely reject valid high-level questions. If total chunks $\le \text{top\_k}$ and an LLM is active, the engine provides the complete masked document context to the model, allowing the LLM's system prompt to govern refusal.

### 5.3 Provenance & Citations
Every grounded answer is accompanied by structured `Citation` metadata:
```python
Citation(
    chunk_id="c2",
    page=1,
    line=14,
    snippet="Employee records for department..."
)
```
The Streamlit UI displays clickable expanders showing the exact physical page and line number where evidence was retrieved.

### 5.4 Multi-Document Corpus Mode (`answer_corpus`)
When multiple documents are loaded in a session, `answer_corpus()` gathers candidates across all active document stores, performs cross-document rank aggregation, and produces a unified grounded synthesis.
