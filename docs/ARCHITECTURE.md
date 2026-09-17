# Douyin Knowledge — Technical Architecture V1

Status: Living document  
Phase: Technical architecture design  
Version: 0.1  
Last updated: 2026-09-17

---

## 1. Architecture Goal

V1 should be a **local-first, single-user, specification-driven application** that can:

1. synchronize Douyin collection metadata;
2. apply Processing Policy before expensive AI work;
3. process selected sources adaptively;
4. preserve evidence/provenance;
5. support structured + full-text + vector retrieval;
6. provide an AI conversation interface;
7. remain simple enough for one developer / Codex to build and maintain.

The architecture should optimize for correctness, inspectability, and replaceable components rather than premature scale.

Core principle:

> Keep the durable knowledge model simple and local. Treat expensive AI outputs and search indexes as rebuildable derived state.

---

## 2. V1 Runtime Overview

```text
┌──────────────────────────────┐
│        React Frontend        │
│      local browser UI        │
└──────────────┬───────────────┘
               │ HTTP / SSE
               ▼
┌──────────────────────────────┐
│       FastAPI Backend        │
│                              │
│ API / Chat / Search / Admin  │
└───────┬───────────┬──────────┘
        │           │
        │           └──────────────┐
        ▼                          ▼
┌───────────────┐           ┌───────────────┐
│    SQLite     │           │    LanceDB    │
│ source truth  │           │ vector index  │
│ + FTS5        │           │ rebuildable   │
└───────┬───────┘           └───────────────┘
        │
        ▼
┌──────────────────────────────┐
│      Background Worker       │
│ SQLite-backed job queue      │
│                              │
│ sync / process / enrich /    │
│ reindex / export             │
└───────┬───────────┬──────────┘
        │           │
        ▼           ▼
┌──────────────┐  ┌─────────────────────┐
│ AI Providers │  │ Douyin Capture      │
│ LLM/ASR/OCR  │  │ Adapter             │
│ Embeddings   │  │ HTTP sidecar        │
└──────────────┘  └─────────┬───────────┘
                            ▼
                  ┌─────────────────────┐
                  │ Douyin_TikTok_      │
                  │ Download_API        │
                  │ self-hosted sidecar │
                  └─────────────────────┘
```

V1 is intentionally **not** a microservice architecture.

The API server and worker share the same Python package, domain model, database, and configuration.

---

## 3. Recommended Technology Stack

### Backend

```text
Python 3.12+
FastAPI
Pydantic v2
SQLAlchemy 2
Alembic
Typer (CLI)
uv (Python environment/package workflow)
```

### Frontend

```text
TypeScript
React
Vite
TanStack Query
React Router
```

No SSR framework is required for V1 because the product is primarily local-first and single-user.

### Persistence

```text
SQLite
SQLite FTS5
local filesystem
LanceDB for vector indexing
```

### Media / AI utilities

```text
ffmpeg
pluggable ASR adapter
pluggable OCR adapter
pluggable multimodal/LLM adapter
pluggable embedding adapter
```

The architecture must not hard-code one AI vendor into the domain layer.

---

## 4. Why Python Is the Backend Language

The difficult parts of this project are media processing and AI orchestration rather than high-concurrency web serving.

Python provides the strongest ecosystem for:

- ASR;
- OCR;
- ffmpeg/media tooling;
- embedding models;
- vector databases;
- multimodal model SDKs;
- structured AI extraction;
- data processing.

Using TypeScript for the entire backend would force unnecessary wrappers around many AI/media libraries.

The frontend remains TypeScript/React.

---

## 5. Local-First Deployment Model

V1 should run primarily on one machine.

Recommended user-facing deployment model:

```text
Douyin Knowledge
├── API process
├── Worker process
├── local SQLite database
├── local vector index
├── local evidence/media files
└── Douyin capture sidecar
```

Default network binding should be localhost only.

A typical local runtime may use:

```text
Douyin sidecar: 127.0.0.1:8000
Douyin Knowledge backend: 127.0.0.1:8787
Frontend dev server: 127.0.0.1:5173
```

Exact ports should be configurable.

### Production-style local mode

For a packaged/local release, build the React app and serve the static bundle from the FastAPI process so the user opens one local URL.

### Future desktop packaging

Tauri can wrap the local web application later if a native desktop distribution becomes valuable.

Tauri is **not required for V1** and should not block initial implementation.

---

## 6. Douyin Integration Boundary

V1 should **not fork or copy Douyin reverse-engineering code into this repository**.

Instead, Douyin access is represented by a capture-provider abstraction.

```python
class CaptureProvider(Protocol):
    async def health(self) -> ProviderHealth: ...
    async def list_collections(self) -> list[ExternalCollection]: ...
    async def iter_collection_sources(self, collection_id: str): ...
    async def fetch_source(self, external_id: str) -> ExternalSource: ...
    async def acquire_media(self, external_id: str) -> MediaHandle: ...
```

V1 implementation:

```text
CaptureProvider
└── DouyinCaptureProvider
    └── REST client
        └── Douyin_TikTok_Download_API sidecar
```

### Why sidecar instead of library import

1. isolates volatile Douyin reverse-engineering logic from the knowledge application;
2. lets the upstream project update independently;
3. isolates cookies/session identity handling;
4. keeps this repository focused on knowledge management;
5. allows future capture providers without modifying the core pipeline.

### Authentication boundary

Douyin credentials/cookies should remain inside the Douyin sidecar where possible.

Douyin Knowledge stores only the sidecar connection configuration/API key required to call the local service.

---

## 7. Domain Architecture

The backend should be divided by domain capability rather than by framework layer alone.

Recommended Python modules:

```text
douyin_knowledge/
├── api/
├── cli/
├── config/
├── domain/
├── db/
├── capture/
├── policy/
├── processing/
├── evidence/
├── entities/
├── retrieval/
├── conversation/
├── ai/
├── jobs/
├── storage/
├── exports/
└── observability/
```

### Domain layer

Contains platform-neutral objects and rules such as:

```text
Source
SourceCollection
EvidenceUnit
ProcessingRun
KnowledgeItem
EntityMention
Entity
Claim
Topic
UserState
ProcessingPolicy
QueryPlan
```

The domain layer should not depend on FastAPI or React.

---

## 8. SQLite as the Source of Truth

SQLite is the authoritative persistent store for V1.

It should contain all durable semantic state, including:

- sources;
- original collection membership;
- processing decisions;
- processing runs;
- evidence metadata/text;
- knowledge items;
- entity mentions;
- canonical entities;
- claims;
- topics;
- user state/annotations;
- policy rules;
- job state;
- conversation metadata if persisted;
- index/version metadata.

### SQLite configuration

Recommended defaults:

```text
WAL mode
foreign_keys = ON
busy_timeout configured
migrations via Alembic
```

### Important rule

The application must be recoverable from:

```text
SQLite + retained local evidence files
```

Derived vector indexes and generated Markdown should be rebuildable.

---

## 9. Full-Text Search

Use SQLite FTS5 for lexical retrieval.

Recommended indexed content:

- source title;
- source caption;
- transcript text;
- OCR text;
- KnowledgeItem title/summary/key points;
- normalized claim text;
- canonical entity name + aliases.

FTS rows should contain references back to canonical database object IDs rather than duplicate the full domain model.

FTS indexes are derived state and should be rebuildable.

---

## 10. Vector Storage

V1 recommendation: **LanceDB as a local embedded vector index**.

The vector store should be treated as a search accelerator, not the authoritative database.

Recommended initial vector collections:

```text
knowledge_items
retrieval_chunks
entity_profiles
```

Each vector row should retain:

```text
object_id
object_type
embedding_model
embedding_version
text_hash
metadata needed for filtering
```

### Why not put the entire data model in the vector database

Vector search is not the canonical knowledge representation.

SQLite remains responsible for:

- identity;
- provenance;
- relationships;
- exact values;
- user state;
- processing status;
- policy decisions.

### Why not require sqlite-vec in V1

The vector backend should sit behind an interface. A SQLite vector extension may become attractive later, but V1 should not tie core durability to a young extension.

Interface:

```python
class VectorIndex(Protocol):
    async def upsert(...): ...
    async def search(...): ...
    async def delete(...): ...
    async def rebuild(...): ...
```

LanceDB is the default implementation, not a domain dependency.

---

## 11. Local Filesystem Layout

Recommended runtime data layout:

```text
<data_dir>/
├── app.db
├── vectors/
│   └── lancedb/
├── media/
│   ├── cache/
│   └── pinned/
├── evidence/
│   ├── frames/
│   └── artifacts/
├── exports/
│   └── markdown/
├── logs/
└── tmp/
```

The data directory must be configurable and gitignored.

### Media cache

`media/cache/` is disposable after successful processing according to retention policy.

### Pinned media

`media/pinned/` is only for future/user-explicit retention, not the default.

### Evidence artifacts

Retain only evidence needed for traceability or future use, for example selected keyframes.

---

## 12. Markdown / Obsidian Export

Markdown should be a **generated projection**, not the canonical database.

This satisfies the user's desire for a readable local folder while avoiding two competing sources of truth.

Conceptually:

```text
SQLite knowledge model
↓
Markdown exporter
↓
<data_dir>/exports/markdown/
```

Possible structure:

```text
markdown/
├── sources/
├── entities/
├── topics/
└── index.md
```

Files may include YAML frontmatter with stable object IDs.

If a user later wants bidirectional Obsidian editing, that should be designed explicitly rather than inferred from generated files.

---

## 13. Background Job Architecture

V1 should not require Redis, Celery, Kafka, or another external broker.

Use a SQLite-backed job table and a dedicated worker process.

### Job table conceptual fields

```text
job_id
job_type
payload_json
priority
status
attempt_count
max_attempts
run_after
locked_by
lease_expires_at
created_at
started_at
finished_at
last_error
unique_key
```

### Primary job types

```text
sync_collections
sync_collection
sync_source
process_source
upgrade_processing_level
enrich_source
resolve_entities
reindex_source
rebuild_fts
rebuild_vectors
export_markdown
cleanup_cache
```

### Job priorities

Suggested priority classes:

```text
interactive
recent
normal
historical
maintenance
```

A query-triggered enrichment should outrank historical bulk processing.

### Job claiming

V1 assumes one normal worker process.

The worker should atomically claim one job with a short lease, process it, and update status.

The design may support multiple workers later, but multi-worker scaling should not complicate V1.

---

## 14. Processing State Machine

Keep source synchronization state separate from knowledge-processing state.

Recommended conceptual statuses:

### Sync state

```text
new
synced
unavailable
```

### Policy state

```text
undecided
process
metadata_only
always_process
```

### Processing state

```text
pending
processing
ready
partial
failed
stale
```

### Processing level

```text
0 metadata
1 text-first
2 ASR
3 visual-enhanced
4 deep multimodal
```

The app should never infer “fully understood” purely from `processing_state=ready`; coverage fields must state what evidence was actually acquired.

---

## 15. AI Provider Architecture

The domain layer should use capability interfaces instead of vendor SDKs directly.

Recommended interfaces:

```python
class ChatModel: ...
class StructuredModel: ...
class VisionModel: ...
class EmbeddingModel: ...
class ASRProvider: ...
class OCRProvider: ...
```

A provider registry maps configured model names to implementations.

### Default integration strategy

A thin internal gateway should be the application-facing API.

LiteLLM may be used as an adapter for broad model-provider compatibility, but domain code should depend on the internal interfaces rather than importing LiteLLM everywhere.

This preserves the ability to later use:

- OpenAI-compatible APIs;
- other cloud providers;
- LM Studio / local inference;
- local embedding models;
- local ASR.

### Structured output

Classification/extraction/query-planning outputs should use explicit Pydantic schemas.

Do not parse arbitrary prose when a structured result is expected.

---

## 16. AI Capability Profiles

Configuration should distinguish task roles instead of assuming one model handles everything.

Example logical roles:

```text
triage_model
extraction_model
query_planner_model
answer_model
vision_model
embedding_model
asr_provider
ocr_provider
```

A single provider/model may fill multiple roles in a simple configuration.

This role-based design allows later cost/quality tuning without changing application logic.

---

## 17. Processing Pipeline Integration

The worker orchestrates the adaptive pipeline described in `AI_PIPELINE.md`.

Conceptual code boundary:

```text
process_source(source_id)
    ↓
load Source
    ↓
ProcessingPolicy.evaluate()
    ↓
ProcessingPlanner.plan()
    ↓
EvidenceAcquirers
    ├── subtitle
    ├── ASR
    ├── keyframe/OCR
    └── vision
    ↓
Classifier
    ↓
ProfileExtractor
    ↓
Claim/Entity persistence
    ↓
EntityResolver
    ↓
SearchIndexer
```

Each stage should be idempotent where practical.

Processing failures should preserve prior successful stage outputs.

---

## 18. Processing Policy Architecture

Policy evaluation must happen before expensive AI work.

Recommended components:

```text
PolicyRepository
PolicyEvaluator
PolicyExplanation
```

`PolicyEvaluator` receives lightweight source metadata and optional cheap triage classification.

Result:

```yaml
action: metadata_only
matched_rule_id: rule_123
reason: creator_excluded
```

Rule precedence should favor specificity.

Recommended order:

```text
per-source always_process
>
per-source exclude
>
creator / collection explicit rules
>
semantic/domain rules
>
keyword rules
>
default process
```

Exact precedence must be deterministic and testable.

---

## 19. Retrieval Architecture

The conversation layer should call typed retrieval services rather than constructing arbitrary SQL.

Recommended internal tools:

```text
search_sources(filters, text)
search_entities(filters, text)
search_fts(query, filters)
search_semantic(query, object_types, filters)
load_entity_context(entity_ids)
load_claim_evidence(claim_ids)
load_source_context(source_ids)
update_user_state(...)
request_enrichment(source_id, level)
```

### Query planner

The LLM outputs a typed `QueryPlan`.

The executor validates the plan against allowed operations.

The model should **not receive raw database credentials and should not generate arbitrary SQL for execution**.

### Candidate fusion

V1 can use deterministic weighted ranking / reciprocal-rank-style fusion.

Do not add a learned reranker until real query benchmarks show a need.

---

## 20. Conversation Execution Flow

```text
POST /chat
↓
Conversation context resolution
↓
Query planner
↓
Validated QueryPlan
↓
Retrieval executor
├── SQLite structured
├── FTS5
├── LanceDB
└── evidence loader
↓
Coverage / sufficiency check
↓
optional high-priority enrichment job
↓
Evidence packet
↓
Answer model
↓
Citation assembler
↓
stream answer via SSE
```

### SSE rather than WebSocket for V1

Chat response streaming is primarily server → client.

Server-Sent Events are simpler than maintaining a bidirectional WebSocket protocol and are sufficient for V1.

---

## 21. API Surface V1

Exact endpoint design will evolve, but logical groups should include:

```text
/api/health
/api/settings
/api/capture/*
/api/sync/*
/api/sources/*
/api/entities/*
/api/policies/*
/api/jobs/*
/api/search/*
/api/chat/*
/api/user-state/*
/api/exports/*
```

The OpenAPI schema generated by FastAPI should be treated as the frontend/backend contract.

Frontend types may be generated from OpenAPI later if useful.

---

## 22. Frontend Architecture

V1 frontend should have a small number of primary surfaces.

### 22.1 Home / Ask

Primary UI:

```text
Ask your collection
```

Plus:

- recent processing;
- useful resurfacing cards;
- processing status;
- quick entry to browsing.

### 22.2 Chat

Must support:

- streaming answers;
- citations;
- source/entity cards;
- follow-up context;
- processing/enrichment state.

### 22.3 Library

Browsable views for:

- sources;
- entities;
- topics;
- collections;
- processing status.

### 22.4 Item detail

Show:

- source metadata;
- AI interpretation;
- entities/claims;
- evidence/timestamps;
- keyframes;
- transcript;
- user state.

### 22.5 Processing Policy settings

Allow management of:

- creator rules;
- collection rules;
- type/domain rules;
- keyword rules;
- per-source overrides.

### 22.6 System / processing dashboard

Show:

- sync status;
- pending jobs;
- failures;
- processing levels;
- storage usage;
- model/provider configuration.

---

## 23. Configuration

Configuration is divided into three classes.

### Non-secret runtime config

Examples:

```text
data directory
ports
retention settings
processing defaults
sidecar URL
```

Stored in app config / settings.

### Secrets

Examples:

```text
AI provider API keys
Douyin sidecar API key
```

V1 may load these from environment variables or a local secret file outside version control.

They must never be written into Git or logs.

A platform keychain integration may be added later.

### User product settings

Examples:

```text
processing policy rules
resurfacing preferences
model-role selection
```

Persist these in SQLite.

---

## 24. Security and Privacy Defaults

V1 defaults:

1. bind services to localhost;
2. no public cloud server required;
3. no analytics/telemetry required;
4. keep raw collection database local;
5. send only the minimum evidence needed to configured cloud AI providers;
6. never log API keys/cookies;
7. keep Douyin cookies isolated in the capture sidecar where possible;
8. `.env`, runtime DBs, media, vectors, and logs are gitignored.

The UI must make it clear when a configured cloud model causes source content to leave the local machine.

---

## 25. Observability

V1 should have structured local logs with correlation IDs.

Useful context:

```text
request_id
job_id
source_id
processing_run_id
provider/model
stage
duration
cost/usage when available
error category
```

Do not log full source text by default when metadata is enough for diagnostics.

A lightweight `events` or job history table can power the UI processing dashboard.

---

## 26. Error Handling

Errors should be typed by subsystem.

Examples:

```text
CaptureUnavailable
AuthenticationRequired
SourceUnavailable
MediaDownloadFailed
ASRFailed
OCRFailed
ModelRateLimited
StructuredOutputInvalid
EntityResolutionAmbiguous
VectorIndexUnavailable
```

Retryable infrastructure/provider failures should be retried by the job worker with backoff.

Semantic/data problems should not be blindly retried.

A source may end in `partial` rather than `failed` if useful evidence was obtained before one optional stage failed.

---

## 27. Index Rebuildability

The system must support commands/jobs such as:

```text
rebuild FTS
rebuild vectors
re-embed all stale objects
regenerate markdown exports
reprocess selected sources
```

This is essential because AI models, schemas, and embedding models will change.

Derived indexes should track version metadata so stale rows can be detected.

---

## 28. CLI

The backend package should expose a small administrative CLI, likely via Typer.

Suggested commands:

```text
dk doctor
dk api
dk worker
dk sync
dk process <source_id>
dk reindex
dk export markdown
dk cleanup
dk db migrate
```

The web UI remains the normal user interface; CLI exists for development, recovery, automation, and debugging.

---

## 29. Repository Structure

Recommended monorepo structure:

```text
douyin-knowledge/
├── README.md
├── AGENTS.md                    # later: Codex instructions
├── .env.example
├── .gitignore
├── docker-compose.yml           # primarily sidecar/local services
│
├── docs/
│   ├── PRODUCT_SPEC.md
│   ├── DATA_SCHEMA.md
│   ├── AI_PIPELINE.md
│   ├── PROCESSING_POLICY.md
│   ├── RETRIEVAL.md
│   ├── ARCHITECTURE.md
│   └── TASKS.md                 # later
│
├── backend/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── alembic.ini
│   ├── migrations/
│   ├── src/
│   │   └── douyin_knowledge/
│   │       ├── api/
│   │       ├── cli/
│   │       ├── config/
│   │       ├── domain/
│   │       ├── db/
│   │       ├── capture/
│   │       ├── policy/
│   │       ├── processing/
│   │       ├── evidence/
│   │       ├── entities/
│   │       ├── retrieval/
│   │       ├── conversation/
│   │       ├── ai/
│   │       ├── jobs/
│   │       ├── storage/
│   │       ├── exports/
│   │       └── observability/
│   └── tests/
│
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   └── tests/
│
├── scripts/
└── data/                        # gitignored runtime default
```

---

## 30. Docker Strategy

Docker should help run dependencies but should not force the entire developer workflow into containers.

Recommended V1:

```text
docker-compose.yml
└── douyin-capture-sidecar
```

Optionally add the application itself to Compose later for one-command deployment.

For development, running FastAPI/worker locally provides easier access to ffmpeg, local models, GPU/Metal acceleration, and debuggers.

---

## 31. Testing Strategy

The architecture must be testable without a live Douyin account or paid AI calls.

### Required test doubles

```text
FakeCaptureProvider
FakeChatModel
FakeStructuredModel
FakeEmbeddingModel
FakeASRProvider
FakeOCRProvider
InMemory/temporary VectorIndex
```

### Test categories

```text
unit tests
DB repository tests
policy precedence tests
pipeline state-machine tests
retrieval planner tests
citation/provenance tests
API integration tests
fixture-based end-to-end tests
```

The most important regression tests should use small frozen fixture sources representing:

- restaurant recommendation;
- travel guide;
- learning/opinion video;
- excluded entertainment clip;
- duplicate entity across sources;
- conflicting claims.

No test suite should depend entirely on live Douyin endpoints.

---

## 32. Architectural Replaceability

The following components must be replaceable behind interfaces:

```text
CaptureProvider
Chat/Structured/Vision model provider
ASRProvider
OCRProvider
EmbeddingModel
VectorIndex
MediaStorage
```

The following should be stable core concepts:

```text
Source
EvidenceUnit
Claim
Entity
KnowledgeItem
UserState
ProcessingPolicy
QueryPlan
```

This boundary allows implementation technologies to change without redesigning the product.

---

## 33. Explicit V1 Non-Goals

Do not add these unless later specs explicitly require them:

```text
PostgreSQL
Redis
Celery
Kafka
Kubernetes
microservices
Neo4j / graph DB
cloud multi-user auth
multi-tenant SaaS architecture
Next.js SSR
native mobile apps
Tauri desktop packaging
fully distributed workers
custom ML ranking model
```

These may become useful later, but none are needed to validate the V1 product promise.

---

## 34. Confirmed Architecture Decisions

### ARCH-001 — Python backend, TypeScript/React frontend

Python owns AI/media/backend work; React owns user interface.

### ARCH-002 — Local-first monorepo

V1 is optimized for one local user and one repository.

### ARCH-003 — FastAPI is the application HTTP API

It exposes REST/OpenAPI and SSE chat streaming.

### ARCH-004 — SQLite is the authoritative source of truth

The durable knowledge model must not depend on a vector database.

### ARCH-005 — SQLite FTS5 handles lexical search

Full-text retrieval stays local and close to structured data.

### ARCH-006 — LanceDB is the default V1 vector index

It is derived/rebuildable and hidden behind `VectorIndex`.

### ARCH-007 — Background jobs use SQLite + a dedicated worker

No Redis/Celery dependency in V1.

### ARCH-008 — Douyin integration is an external capture sidecar

Our code calls a provider adapter instead of embedding volatile reverse-engineering logic.

### ARCH-009 — AI vendors are hidden behind application interfaces

No domain module should depend directly on one provider SDK.

### ARCH-010 — Markdown is an export/projection, not canonical storage

Readable local files remain available without creating a second source of truth.

### ARCH-011 — Frontend is a local web app first

Desktop packaging is deferred until the product loop is proven.

### ARCH-012 — Long-running work is resumable and idempotent where practical

A partial failure must not discard already-acquired evidence.

### ARCH-013 — Query planner cannot execute arbitrary generated SQL

Conversation uses typed, validated retrieval operations.

### ARCH-014 — Vector/full-text indexes are rebuildable derived state

Index technology or embedding models may change without data loss.

---

## 35. Open Technical Questions

These should be answered during implementation spikes or the TASKS phase rather than blocking the architecture now.

### AI providers

- Which cloud/local model combination becomes the default example config?
- Which local ASR implementation gives the best Chinese accuracy/performance on target machines?
- Which OCR implementation should be the default for Chinese on-screen text?

### Vector/index benchmarks

- LanceDB exact schema and index settings;
- chunk sizes and embedding surfaces;
- retrieval fusion weights;
- whether sqlite-vec becomes preferable after benchmarks/maturity changes.

### Media

- exact keyframe extraction thresholds;
- cache TTL / cleanup quota;
- ffmpeg installation UX.

### Douyin sidecar

- exact endpoint adapter mapping;
- first-run identity setup UX;
- error/backoff behavior during platform risk-control responses.

### Packaging

- whether V1 distribution should remain developer/local-web oriented or add a Tauri wrapper after the first working release.

### Secrets

- whether V1 should integrate OS keychain storage or initially use local environment/secret files.

These questions require empirical implementation/testing and should not force premature architectural complexity.
