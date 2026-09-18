# Douyin Knowledge — Implementation Plan

Status: Living handoff document
Phase: Phases 0–13 delivered in demo mode; 10, 14, 15, 16 not started
Version: 0.2
Last updated: 2026-09-18

---

## 0. Delivered state

This section is the answer to "what actually runs?", kept separate from the plan below so the plan can stay a plan. Everything here was verified by running it, not by reading the code.

**Delivered and exercised end to end.** Phases 0–9 and 11–13. From an empty data directory: `dk db upgrade` → `dk doctor` → `dk sync --process --wait` produces 3 collections, 8 sources processed to L2, 14 entities, 4 claims, 2 wiki pages, 0 failed jobs across 25 jobs; `dk search` and `dk ask` return cited answers; `dk policy exclude` then `dk process --force` is blocked, and `dk policy why` explains the block from the recorded decision. The API serves all of it, and the React frontend calls every route through its own client module (`frontend/npm run probe` asserts this against a live server, including that every wiki support resolves to a source and that `/api/admin/settings` returns `openai_api_key` as a boolean rather than a value).

**Runs in demo mode by default.** `DK_AI_PROVIDER=mock` and `DK_CAPTURE_PROVIDER=fixture` are the shipped defaults, so the whole product works with no key and no Douyin session. Phase 6's real adapters (OpenAI chat/embedding/vision) are implemented and selected by `DK_AI_PROVIDER=openai`, but have not been run against the live API. Phase 4's Douyin sidecar adapter is likewise implemented against the documented contract and unexercised: there is no sidecar to point it at.

**Not started.** Phase 10 (on-demand enrichment), 14 (resurfacing), 15 (export/backup/rebuild — `exports/` is an empty package), 16 (evaluation suite). The corresponding job types `ENRICH_SOURCE`, `CLEANUP_CACHE` and `EXPORT_MARKDOWN` are declared in `jobs/types.py` but no handler is registered, so enqueueing one raises `ConfigurationError: no handler registered for job type ...` rather than failing quietly — deliberate, but it means the enum overstates what the queue can do.

**Phase 12 partially.** Wiki lint runs and writes `wiki_lint_findings`; findings surface in the API and the UI. The governance loop around them — triage, suppression, close criteria beyond `wiki_quality_close_threshold` — is not built.

**Known dead code.** `retrieval/query_planner.py` is exported and never instantiated; `Settings.enable_query_enrichment` and `query_planner_model` are read by nothing. Follow-up resolution happens in `conversation/conversation_manager.py:_resolve_followup` from conversation state instead. See DECISIONS.md "Known gaps".

**Verification baseline.** 187 passed, 4 skipped; `ruff check` clean. The 4 skips are the tests that require real provider credentials.

---

## 1. Purpose

This document turns the product/architecture specifications into an implementation sequence for Codex or another coding agent.

The project should be built as **small, testable vertical slices**. Do not attempt to implement the entire architecture in one pass.

Primary references:

```text
docs/PRODUCT_SPEC.md
docs/DATA_SCHEMA.md
docs/AI_PIPELINE.md
docs/PROCESSING_POLICY.md
docs/WIKI.md
docs/RETRIEVAL.md
docs/ARCHITECTURE.md
docs/PHYSICAL_SCHEMA.md
AGENTS.md
```

---

## 2. Global Definition of Done

A phase is not complete merely because code exists.

Every phase must include:

- migrations/configuration needed for the feature;
- typed domain interfaces;
- unit tests for core business logic;
- integration tests for persistence/API boundaries;
- deterministic fixtures where external services would otherwise be required;
- basic observability/error reporting;
- documentation updates when implementation decisions materially differ from the spec.

Do not silently change product semantics to make implementation easier.

---

# Phase 0 — Repository Bootstrap

## Goal

Create a runnable development skeleton with no external AI or Douyin dependency.

## Tasks

### Backend

- Initialize Python 3.12+ project using `uv`.
- Create package structure defined in `ARCHITECTURE.md`.
- Add FastAPI application.
- Add Pydantic settings/config layer.
- Add SQLAlchemy 2 + Alembic.
- Add Typer CLI entrypoint.
- Configure pytest, ruff, mypy/pyright-equivalent static checking.
- Add structured logging.

### Frontend

- Initialize TypeScript + React + Vite.
- Add React Router.
- Add TanStack Query.
- Add a minimal application shell.

### Local runtime

- Add `.env.example` with non-secret configuration.
- Add data-root configuration.
- Add dev commands for API, worker, and frontend. (`scripts/dev.sh` runs all three; `--seed` syncs the fixture corpus first, `--no-frontend` skips Vite.)
- Add ffmpeg availability check, but do not make full media processing mandatory yet.

## Acceptance tests

```text
backend starts
GET /health returns OK
worker process starts and idles
frontend starts and reaches backend
Alembic upgrade works on an empty SQLite DB
```

---

# Phase 1 — Core Database and Job Queue

## Goal

Establish the local source-of-truth database and durable background-job mechanism before adding external integrations.

## Tasks

Implement Physical Schema migrations 001 first:

```text
creators
collections
sources
source_snapshots
source_collection_memberships
source_assets
processing_rules
policy_decisions
source_processing_state
jobs
job_events
app_settings
```

Implement:

- SQLAlchemy models;
- repository/data-access layer;
- transaction boundaries;
- SQLite WAL/busy timeout setup;
- job enqueue/claim/heartbeat/finish/fail semantics;
- dedupe-key behavior;
- retry/backoff behavior;
- stale lock recovery;
- job priority.

## Acceptance tests

- create/update a source without duplicating `(platform, external_id)`;
- preserve multiple source snapshots;
- preserve historical collection membership;
- enqueue a deduplicated job twice and receive one active job;
- worker safely recovers a stale locked job;
- failed job records diagnostics without corrupting source rows.

---

# Phase 2 — Capture Provider Contract + Mock Provider

## Goal

Implement the capture boundary without depending on live Douyin behavior yet.

## Tasks

Define typed domain contracts:

```text
CaptureProvider
ProviderHealth
ExternalCollection
ExternalSource
MediaHandle
```

Implement `MockCaptureProvider` backed by fixture JSON files.

Fixtures should include:

- two creators;
- several collections;
- one source in multiple collections;
- a deleted/unavailable source;
- changing source metadata across two snapshots;
- video and image-album-like sources.

Implement collection sync service using only the provider interface.

## Acceptance tests

- full metadata inventory sync works from fixtures;
- second sync is idempotent;
- changed metadata creates a new snapshot;
- missing source marks availability/membership state rather than destructively deleting history.

---

# Phase 3 — Processing Policy

## Goal

Make user-controlled exclusion work before any expensive AI/media processing exists.

## Tasks

Implement:

- rule CRUD;
- deterministic Policy Pass A;
- rule precedence;
- per-source override;
- creator rule;
- collection rule;
- keyword/metadata rule;
- `process`, `metadata_only`, `always_process` actions;
- policy decision audit log;
- source processing state update.

Add a semantic-classifier interface but use a deterministic/mock implementation first.

## Required behavior

A source marked `metadata_only` must:

- stay searchable by metadata;
- not enqueue AI/media processing jobs;
- retain the reason/rule that caused exclusion;
- be reversible when the rule changes.

## Acceptance tests

- creator exclusion skips all matching sources;
- source-level `always_process` overrides creator exclusion;
- changing a rule re-evaluates affected sources without deleting history;
- metadata-only source never enters the processing queue unless explicitly re-enabled.

---

# Phase 4 — Douyin Sidecar Adapter

## Goal

Connect real Douyin metadata sync through an isolated adapter.

## Preconditions

Phases 1–3 must work completely with fixtures first.

## Tasks

Implement `DouyinCaptureProvider` as an HTTP client to the external/self-hosted Douyin sidecar.

Required capabilities:

```text
health
list_collections
list/iterate collection sources
fetch source detail
acquire media when needed
```

Requirements:

- no reverse-engineering code copied into this repository;
- no raw Douyin cookies stored in the core DB;
- sidecar base URL/API key configurable;
- upstream async task semantics hidden behind provider adapter;
- retryable vs non-retryable errors mapped to internal error types;
- rate/backoff behavior conservative;
- sidecar unavailability must not break local browsing/search.

## Acceptance tests

Use recorded/mock HTTP responses for CI.

A live-sidecar smoke test may be optional/manual.

---

# Phase 5 — Evidence Acquisition and Processing Runs

## Goal

Implement traceable source processing without yet requiring sophisticated entity resolution or Wiki integration.

## Tasks

Implement Physical Schema migration 002:

```text
processing_runs
evidence_units
processing_run_evidence
retrieval_chunks
retrieval_chunk_evidence
knowledge_items
knowledge_item_labels
topics
knowledge_item_topics
```

Implement provider interfaces:

```text
ASRProvider
OCRProvider
VisionProvider
LLMProvider
EmbeddingProvider
```

Start with mock providers.

Implement processing levels:

```text
Level 0 metadata
Level 1 text-first
Level 2 ASR
Level 3 visual enhancement
Level 4 deep multimodal
```

Implement evidence reuse across runs.

## Required behavior

- evidence rows are citation-sized;
- retrieval chunks are larger semantic units;
- one chunk can reference multiple evidence units;
- reprocessing may reuse existing evidence;
- processing coverage is explicit;
- failure at an expensive stage preserves already acquired evidence.

## Acceptance tests

- mock transcript produces timestamped evidence;
- retrieval chunk links back to exact evidence units;
- second processing run can reuse transcript evidence;
- failed vision step does not erase ASR evidence;
- KnowledgeItem shows correct processing coverage.

---

# Phase 6 — Real ASR/OCR/LLM Adapters

## Goal

Introduce replaceable real providers without coupling business logic to a vendor.

## Tasks

Implement at least one working provider for each V1-required capability:

- LLM structured output;
- embeddings;
- ASR;
- OCR;
- optional multimodal vision.

The first implementation may use cloud and/or local models, but domain code must call internal provider interfaces only.

Implement:

- provider capability checks;
- retry/backoff;
- structured-output validation;
- token/cost/latency metrics where available;
- model configuration by role (`triage`, `extract`, `plan`, `answer`, etc.).

## Acceptance tests

Provider contract tests must run against mocks in CI; live provider tests are opt-in.

---

# Phase 7 — Structured Extraction: Entities and Claims

## Goal

Move from source summaries to reusable cross-source structured knowledge.

## Tasks

Implement Physical Schema migration 003:

```text
entities
entity_aliases
entity_external_ids
entity_mentions
entity_mention_evidence
claims
claim_evidence
entity_user_states
knowledge_item_user_states
user_annotations
```

Implement extraction profiles for a minimal V1 set:

```text
restaurant/place recommendation
general travel guide
tool/software review
learning/tutorial
opinion/explanation
```

Implement structured-output schemas.

Implement conservative entity resolution:

```text
strong IDs first
exact normalized identity second
fuzzy/semantic only as supporting signal
```

Do not auto-merge medium-confidence candidates.

## Acceptance tests

- one video can create multiple entity mentions;
- two sources can resolve to one canonical entity;
- ambiguous same-name entities remain separate;
- creator price claim remains a Claim, not an entity truth field;
- every important extracted claim can link to evidence;
- user note/rating never overwrites creator claims.

---

# Phase 8 — Full-Text and Vector Indexing

## Goal

Create rebuildable search projections over structured/derived data.

## Tasks

Implement Physical Schema migration 004 search subset:

```text
search_documents
search_fts
vector_documents
```

Implement index builders for:

```text
source metadata
KnowledgeItems
retrieval chunks
entities
claims
Wiki pages later
```

Implement LanceDB adapter behind a `VectorIndex` protocol.

Requirements:

- vector index rebuildable from SQLite;
- model/content hash used for stale-vector detection;
- deletion/reprocessing updates projections safely;
- benchmark Chinese FTS tokenizer behavior before freezing configuration.

## Acceptance tests

- rebuild FTS from scratch;
- rebuild vectors from scratch;
- semantic lookup returns fixture concepts with wording variation;
- changing embedding model marks/rebuilds stale vectors.

---

# Phase 9 — Retrieval Planner and AI Chat MVP

## Goal

Deliver the first true product loop: ask natural-language questions about saved knowledge and receive traceable answers.

## Tasks

Implement remaining migration 004 tables:

```text
conversations
messages
message_citations
conversation_state
```

Implement:

- query scope classifier: personal/general/hybrid;
- query parser;
- QueryPlan object;
- structured retrieval;
- FTS retrieval;
- vector retrieval;
- entity retrieval;
- claim/evidence loading;
- simple candidate fusion/reranking;
- evidence packet builder;
- grounded answer generation;
- structured message citations;
- follow-up reference resolution.

## Required behavior

For `personal_required` questions:

- do not silently fill gaps using general model knowledge;
- distinguish no-result vs shallow-processing cases;
- cite supporting saved sources;
- prefer Claim → Evidence over generated summaries for factual-style statements.

## Acceptance benchmark queries

Include fixtures for at least:

```text
exact structured query
remembered phrase/name
fuzzy semantic query
cross-source synthesis
conflicting claims
follow-up reference
metadata-only excluded item
no-result query
```

---

# Phase 10 — On-Demand Enrichment

## Goal

Let retrieval deepen only the sources that become valuable during active use.

## Tasks

When retrieval finds a relevant source with insufficient coverage:

```text
coverage check
→ enqueue high-priority enrich_source
→ acquire missing evidence
→ update derived knowledge/index
→ resume or refresh answer
```

For policy-excluded sources, require an explicit one-time override according to Processing Policy.

## Acceptance tests

- Level-1 source can be promoted to Level-2/3 due to a query;
- broad exclusion rule remains unchanged after one-time processing;
- enriched result gains evidence/citations without duplicate source records.

---

# Phase 11 — Compounding Wiki

## Goal

Add persistent synthesized knowledge without making Wiki the source of truth.

## Tasks

Implement migration 005:

```text
wiki_integration_runs
wiki_pages
wiki_revisions
wiki_supports
wiki_links
wiki_lint_findings
quality_rules
quality_rule_samples
quality_ledger
```

Implement the two-stage integration model:

### Stage 1 — Route/select

- determine whether source adds durable knowledge;
- select likely pages to create/update;
- select related entity/topic pages;
- avoid loading the full Wiki.

### Stage 2 — Integrate

- load only selected pages + new structured knowledge;
- create revisions;
- add statement-level support mappings;
- preserve old revisions;
- never overwrite pages the model was not allowed to inspect.

Implement deterministic index generation and Markdown export.

## Context pruning

Implement metrics and progressively optimized context selection inspired by the prior LLM Wiki experience:

```text
relevant page/section whitelist
alias/detail expansion only for likely entities
compact page map/index representation
```

Benchmark token reduction rather than hard-coding a claimed target.

## Acceptance tests

- repeated sources update one page rather than spawning duplicates;
- old revision remains inspectable;
- Wiki statement links to Claim/Evidence;
- rebuild Wiki from structured knowledge/source state;
- Markdown export can be deleted and regenerated.

---

# Phase 12 — Wiki Lint and Quality Governance

## Goal

Make long-lived generated knowledge self-auditing.

## Tasks

Implement deterministic lint checks first:

```text
broken link
index mismatch
duplicate canonical key
missing required metadata
unsupported statement
orphan page
invalid entity link
stale/rebuild mismatch
```

Implement code-level auto-fixes where safe.

Implement quality-rule lifecycle:

```text
finding
→ sample
→ rule/instruction
→ prompt injection
→ subsequent verification
→ pass_count / fail_count
→ close when stable
```

Implement repair ledger/report.

Only use LLM repair when deterministic repair is insufficient.

## Acceptance tests

- code-fixable issues are repaired without LLM;
- repeated issue creates/updates one quality rule;
- prompt builder receives active quality rules;
- rule closes after configured successful verification threshold;
- repair history is queryable.

---

# Phase 13 — Product UI V1

## Goal

Expose the system through a coherent local web product rather than developer-only endpoints.

## Required surfaces

### Home

- Ask your saves input;
- recent processed items;
- processing counts/status;
- worth-revisiting placeholder/signal surface;
- basic categories/topics.

### Chat

- streaming answers;
- collection/general/hybrid scope display;
- clickable citations;
- evidence/source drill-down.

### Source detail

- source metadata;
- AI summary;
- processing coverage;
- transcript/OCR evidence;
- linked entities/claims;
- original source link;
- reprocess/enrich actions.

### Entity detail

- canonical identity;
- mentions across sources;
- source-attributed claims;
- personal state/note/rating.

### Processing Policy

- creator rules;
- collection rules;
- type/semantic rules;
- source overrides;
- skipped-source browser;
- explanation of why each source was skipped.

### Wiki

- Wiki map/index;
- page reader;
- source/evidence provenance;
- revision history (basic V1 view).

## Acceptance tests

A user can complete this flow without CLI:

```text
sync
→ see imported saves
→ exclude a creator/type
→ process a source
→ ask a question
→ open citation
→ see evidence
→ mark an entity visited/used
```

---

# Phase 14 — Resurfacing V1

## Goal

Solve "saved and forgotten" without noisy notifications.

## Initial signals

Implement conservative, inspectable signals:

- repeated topic concentration over a recent window;
- many unvisited/untried entities in an active topic;
- newly enriched older source relevant to recent queries;
- unfinished user-state items.

Do not implement opaque recommendation ML in V1.

Resurfacing suggestions should explain why they appeared.

---

# Phase 15 — Export, Backup, Rebuild, Recovery

## Goal

Prove local-first durability.

## Tasks

- Markdown export;
- database backup command;
- FTS rebuild;
- vector rebuild;
- Wiki rebuild;
- integrity check;
- cache cleanup;
- provider-independent restore procedure.

## Acceptance test

Starting from SQLite + retained source assets/evidence, delete all rebuildable projections and successfully regenerate:

```text
FTS
vectors
Wiki links
Wiki Markdown exports
```

---

# Phase 16 — Evaluation Suite

## Goal

Prevent architecture quality from regressing as prompts/models change.

Create a versioned benchmark corpus and query set.

Measure separately:

```text
retrieval recall
entity resolution accuracy
claim/evidence support accuracy
citation correctness
Wiki integration correctness
answer groundedness
no-result correctness
latency
LLM/model cost
agent/tool loop count
```

Important lesson from the prior LLM Wiki project:

> Tool-level retrieval speed can improve while end-to-end latency worsens because agent loop count increases.

Therefore benchmark the whole user query path, not only individual search calls.

---

## Implementation Order Summary

```text
0  Bootstrap
1  SQLite + jobs
2  Capture contract + fixtures
3  Processing Policy
4  Douyin adapter
5  Evidence pipeline
6  Real AI providers
7  Entity/Claim extraction
8  Search indexes
9  Retrieval + chat MVP
10 On-demand enrichment
11 Compounding Wiki
12 Wiki quality loop
13 Product UI
14 Resurfacing
15 Export/rebuild/recovery
16 Evaluation suite
```

The first meaningful end-to-end product milestone is Phase 9.

The first compounding-knowledge milestone is Phase 12.

---

## Codex Starting Instruction

When implementation begins, start with **Phase 0 only** unless the user explicitly requests a broader phase.

Before coding:

1. read `AGENTS.md`;
2. read the design documents listed at the top of this file;
3. inspect the existing repository state;
4. produce a concise implementation plan for the current phase;
5. implement and test only the requested phase;
6. report any spec conflict rather than silently resolving it by changing architecture.
