# AGENTS.md — Douyin Knowledge

This file defines repository-level working rules for Codex and other coding agents.

The project is specification-first. Do not infer product semantics from filenames or implementation convenience when the design docs already define them.

---

## 1. Mission

Build a local-first personal knowledge system that turns selected Douyin saves into traceable, reusable, compounding knowledge.

The core loop is:

```text
Capture
→ Policy
→ Understand
→ Integrate
→ Retrieve
→ Synthesize
→ Resurface
```

The system is **not** primarily a downloader, media archive, generic vector-RAG chatbot, or social product.

---

## 2. Read Before Coding

Before making substantive changes, read these files in this order:

1. `docs/PRODUCT_SPEC.md`
2. `docs/DATA_SCHEMA.md`
3. `docs/PROCESSING_POLICY.md`
4. `docs/AI_PIPELINE.md`
5. `docs/WIKI.md`
6. `docs/RETRIEVAL.md`
7. `docs/ARCHITECTURE.md`
8. `docs/PHYSICAL_SCHEMA.md`
9. `docs/TASKS.md`

For a narrow task, re-read the relevant document before editing the corresponding subsystem.

---

## 3. Scope Discipline

Implement only the requested phase/task unless a small adjacent change is strictly necessary to make it correct.

Do not:

- implement future phases "while here";
- introduce cloud/multi-user architecture for hypothetical scale;
- replace documented architecture with a preferred framework pattern;
- silently simplify away provenance, policy, or revision history;
- create a second source of truth.

If the specification appears internally inconsistent, surface the conflict in the final report. Do not resolve material product conflicts by guesswork.

---

## 4. Non-Negotiable Data Invariants

### 4.1 Source is not knowledge

Platform/source metadata and AI-derived interpretation are separate.

Never put generated summaries, inferred facts, or user opinions into `sources` as if they were source metadata.

### 4.2 Evidence is first-class

Important extracted claims must be traceable to `EvidenceUnit` records where feasible.

Citation chain:

```text
Claim → EvidenceUnit → Source
```

### 4.3 Claims are attributed assertions, not global truth

A creator saying "人均 80" does not mean:

```text
Entity.average_price = 80
```

It means a source-attributed Claim exists.

Conflicting claims are valid data and must not be silently averaged/overwritten.

### 4.4 User experience is separate

User ratings, visits, notes, usage state, and corrections must remain separate from creator/source claims.

### 4.5 Reprocessing is versioned

Do not mutate old derived processing output in place when a new model/schema processes the source.

Create a new `ProcessingRun` and move the current pointer.

Evidence may be reused across runs.

### 4.6 Wiki is derived state

The Compounding Wiki is a persistent compiled view, but it is not the canonical truth store.

A Wiki page must never become the only place where an important source-derived fact exists.

Wiki Markdown exports must be rebuildable.

### 4.7 Search indexes are rebuildable

FTS/LanceDB content must be reproducible from SQLite-backed objects.

Never write unique knowledge only into an index.

---

## 5. Processing Policy Comes Before Expensive AI

Not every saved video becomes knowledge.

Policy must be evaluated before ASR/OCR/Vision/LLM processing whenever possible.

`metadata_only` means:

- retain lightweight Source metadata;
- retain why it was excluded;
- do not run normal knowledge processing;
- do not include it as understood knowledge in normal retrieval;
- allow later reversal/reprocessing.

Specific user overrides have priority over broad inferred rules according to `PROCESSING_POLICY.md`.

Never silently create a persistent exclusion rule from observed behavior; suggestions require user acceptance.

---

## 6. Douyin Integration Boundary

Do not copy Douyin reverse-engineering implementation into this repository.

Core code talks to a `CaptureProvider` abstraction.

V1 real implementation:

```text
DouyinCaptureProvider
→ local HTTP sidecar
→ external Douyin_TikTok_Download_API service
```

Keep volatile platform logic outside the knowledge domain.

Do not store raw Douyin cookies in the core SQLite database.

Tests must not require live Douyin access. Use fixtures/recorded mock responses.

---

## 7. AI Provider Boundary

Domain/application code must not depend directly on one model vendor.

Use internal capability interfaces such as:

```text
LLMProvider
EmbeddingProvider
ASRProvider
OCRProvider
VisionProvider
```

Provider-specific request/response objects must be normalized at the adapter boundary.

Model choice is configuration, not domain logic.

Structured AI output must be schema-validated before persistence.

Unknown/missing values are preferable to fabricated values.

---

## 8. Adaptive Processing

Do not blindly run the maximum AI pipeline on every source.

Use information sufficiency and processing levels defined in `AI_PIPELINE.md`.

Expected progression:

```text
metadata
→ existing text
→ ASR if needed
→ OCR/keyframes if needed
→ deep multimodal only if needed
```

On-demand enrichment may deepen a source when an active user query needs missing evidence.

For policy-excluded sources, respect the explicit override behavior in the spec.

---

## 9. Compounding Wiki Rules

Wiki integration should follow a two-stage model:

```text
Route/select likely pages
→ load selected context
→ integrate/revise
```

Do not provide the full Wiki to every integration prompt.

Never allow the model to overwrite an existing page whose content it was not allowed to inspect.

Persist revisions rather than destructive replacement.

Where feasible, important Wiki statements should have explicit `wiki_supports` links to Claim/Evidence/Source records.

Index/navigation files should be deterministic/code-maintained where possible, not generated free-form by the LLM.

---

## 10. Quality Governance

Prefer deterministic code checks/fixes over LLM repair.

Expected quality loop:

```text
lint finding
→ safe code fix if possible
→ otherwise targeted LLM repair
→ record quality sample/rule
→ inject active rule into relevant future prompts
→ verify in future runs
→ close when stable
```

Do not convert one bad output into an ever-growing global prompt without evidence that the rule generalizes.

Maintain repair history in the quality ledger.

---

## 11. Retrieval Rules

The system has multiple retrieval surfaces:

```text
structured SQLite
FTS
vectors
entities
claims/evidence
Compounding Wiki
```

Do not reduce all questions to nearest-neighbor vector search.

Use the question type to select primary objects.

Examples:

```text
exact price/location/status filters → structured DB
remembered wording → FTS
fuzzy concept → vector retrieval
real object → Entity
what creator said → Claim + Evidence
mature recurring concept → Wiki
```

For personal/collection-grounded questions, do not silently fill missing evidence using general model knowledge.

General vs personal vs hybrid scope must remain distinguishable.

No fake collection citations.

---

## 12. Database and Migration Rules

Use SQLite as the authoritative local database.

Use SQLAlchemy 2 and Alembic.

All schema changes require migrations.

Do not edit an already-applied migration to change history; add a new migration.

Use foreign keys and explicit deletion behavior.

Keep frequently filtered values in typed/indexed columns rather than burying them in JSON.

JSON is for evolving/sparse/provider-specific nested data.

Enable and test:

```text
foreign_keys
WAL
busy_timeout
```

Do not add PostgreSQL/Redis/Celery/Kafka/Neo4j unless the user explicitly approves a documented architecture change.

---

## 13. Background Jobs

V1 uses a SQLite-backed durable job queue and a small worker model.

Jobs must be:

- idempotent where practical;
- safely retryable;
- observable;
- deduplicatable where duplicate work is harmful;
- recoverable after worker crash/stale lock.

Interactive/query-triggered enrichment should be able to receive higher priority than historical backlog work.

Do not hide long blocking processing inside normal HTTP request handlers.

---

## 14. File/Media Storage

Store machine-independent relative `storage_key` values in the DB.

Do not persist arbitrary absolute developer-machine paths.

Default media philosophy:

- full video/audio: cache unless user pins/configures retention;
- transcript/OCR/evidence: retain;
- small representative keyframes: retain when useful;
- generated indexes/exports: rebuildable.

Never delete retained source evidence merely because a derived model output succeeded.

---

## 15. Frontend Rules

V1 frontend is React + TypeScript + Vite.

The UI must make processing/provenance state visible rather than pretending all sources are equally understood.

Important UI distinctions:

```text
metadata-only vs processed
processing coverage level
creator/source claim vs user experience
personal answer vs general answer
current Wiki synthesis vs original source evidence
```

Citations should be navigable to source/evidence details.

Avoid making folders/tags the primary access interface; AI conversation is primary, browsing is secondary.

---

## 16. Security / Privacy

Default bind is localhost.

Do not log secrets, raw cookies, API keys, or provider credentials.

Do not commit `.env`, credentials, downloaded private media, or local database files.

Add/update `.gitignore` when new local runtime artifacts appear.

Use environment variables or OS/sidecar secret storage for credentials.

---

## 17. Testing Requirements

Every subsystem that touches external services must have deterministic local fixtures/mocks.

Tests should cover invariants, not only happy-path API status codes.

High-value invariant tests include:

- no duplicate Source identity;
- no policy-excluded source enters expensive pipeline accidentally;
- Claim citations resolve to existing Evidence/Source;
- reprocessing does not destroy old run history;
- entity merge preserves provenance;
- user state does not overwrite source claims;
- Wiki rebuild is possible;
- FTS/vector rebuild is possible;
- personal-grounded answer does not use uncited general knowledge;
- no-result behavior does not fabricate matches.

---

## 18. Evaluation

Prompt/model changes can alter system behavior without code failures.

Maintain a versioned benchmark suite covering:

```text
retrieval relevance
citation correctness
claim extraction
entity resolution
Wiki integration
no-result correctness
latency
model cost
agent/tool loop count
```

Do not evaluate only tool-level latency. End-to-end query latency matters.

---

## 19. Documentation Discipline

If implementation reveals a design decision that materially changes the architecture or product contract:

1. do not silently encode it only in code;
2. update the relevant design document;
3. explain the reason in the commit/PR summary.

Small implementation details do not require spec changes.

---

## 20. Git / Change Hygiene

Keep changes focused.

Do not rewrite unrelated files.

Do not force-push, rewrite shared history, or delete user work unless explicitly instructed.

Do not commit generated local data, caches, model downloads, media archives, databases, or secrets.

Prefer understandable commits aligned to implementation phases.

---

## 21. Working Procedure for Codex

For each requested phase/task:

1. inspect repository state;
2. read relevant specs;
3. state the implementation plan briefly;
4. implement the smallest complete slice;
5. run tests/static checks;
6. fix failures rather than reporting untested success;
7. summarize changed files, tests, and any unresolved spec conflicts.

When asked to start implementation without a phase number, begin from the earliest incomplete phase in `docs/TASKS.md`.
