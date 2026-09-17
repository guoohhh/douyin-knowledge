# Douyin Knowledge

> Turn saved short-form content into a searchable, explainable, reusable personal knowledge system.

Douyin Knowledge is a local-first personal knowledge project that starts from Douyin collections. The goal is not to build another downloader or another folder of AI summaries, but to transform content you once considered worth saving into knowledge that can be searched, synthesized, acted on, compounded over time, and resurfaced when it becomes relevant again.

## Current status

The project has completed the first **product + architecture specification pass** and is ready to begin phased implementation. Implementation should follow `docs/TASKS.md` and the repository guardrails in `AGENTS.md` rather than attempting the full system in one pass.

The first version is anchored on Douyin collections, while the long-term architecture should allow other capture channels such as Xiaohongshu, YouTube, web pages, screenshots, and articles.

## Core product loop

```text
Capture
  ↓
Policy
  ↓
Understand
  ↓
Integrate
  ↓
Retrieve
  ↓
Synthesize
  ↓
Resurface
```

The user should still use Douyin normally: see something useful → tap **收藏** → continue scrolling. Everything after that should be handled by the system as automatically as possible.

Not every save needs to become knowledge. A Processing Policy layer lets the user keep entertainment/watch-later content metadata-only and exclude specific creators, collections, content types, or individual sources from expensive AI processing.

`Integrate` is the compounding-knowledge step: processed sources update a persistent Wiki view of entities, concepts, topics, and recurring syntheses instead of forcing every future question to reconstruct understanding from raw chunks.

## Documentation

Design and implementation handoff documents:

- [`docs/PRODUCT_SPEC.md`](docs/PRODUCT_SPEC.md) — product vision, principles, user journey, V1 scope, decisions, and open questions.
- [`docs/DATA_SCHEMA.md`](docs/DATA_SCHEMA.md) — conceptual knowledge model: Source, Evidence, Claim, Entity, KnowledgeItem, User State, and provenance.
- [`docs/AI_PIPELINE.md`](docs/AI_PIPELINE.md) — adaptive ingestion and AI processing pipeline, evidence acquisition, enrichment, extraction, and indexing.
- [`docs/PROCESSING_POLICY.md`](docs/PROCESSING_POLICY.md) — user-controlled rules for deciding which saved sources should or should not enter knowledge processing.
- [`docs/WIKI.md`](docs/WIKI.md) — compounding Wiki layer: compiled knowledge, two-stage integration, context pruning, revisions, lint, quality ledger, and rebuildability.
- [`docs/RETRIEVAL.md`](docs/RETRIEVAL.md) — hybrid retrieval, AI conversation planning, collection-vs-general scope, evidence grounding, and citations.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — V1 technical architecture, stack choices, storage, jobs, capture-provider boundary, AI adapters, repo structure, and deployment model.
- [`docs/PHYSICAL_SCHEMA.md`](docs/PHYSICAL_SCHEMA.md) — concrete SQLite V1 tables, foreign keys, provenance links, Wiki revisions, search/index projections, conversation citations, and migration order.
- [`docs/TASKS.md`](docs/TASKS.md) — phased implementation plan, acceptance tests, and milestone order.
- [`AGENTS.md`](AGENTS.md) — repository-level coding-agent rules and non-negotiable architecture invariants.

## Knowledge architecture at a glance

```text
                    AI Conversation
                          ↓
                    Query Planner
             ┌────────────┼────────────┐
             ↓            ↓            ↓
      Structured DB   Compounding Wiki  FTS / Vector
             \            |            /
              \           |           /
                   Claim / Evidence
                          ↓
                       Source
```

The key trust principle is:

```text
Source / Evidence
    ↓ most trustworthy
Claims / Entities / UserState
    ↓ structured interpretation
Compounding Wiki
    ↓ persistent synthesis
Conversation
    ↓ user-facing reasoning
```

The Wiki is a rebuildable compiled view, not the canonical truth store.

## V1 technical architecture at a glance

```text
React / TypeScript
       ↓
FastAPI
       ↓
SQLite + FTS5  ← authoritative local data
       +
LanceDB         ← rebuildable vector index
       +
SQLite worker queue
       ↓
Adaptive AI/media pipeline
       ↓
Douyin capture sidecar
```

The system is intentionally local-first and avoids unnecessary V1 infrastructure such as Redis, Celery, PostgreSQL, Kafka, graph databases, or microservices.

## Implementation entry point

Codex should begin with **Phase 0** in `docs/TASKS.md` unless explicitly instructed otherwise.

Before coding, it must read `AGENTS.md` and the relevant design docs. The first implementation milestone is deliberately small: repository bootstrap, health checks, development tooling, and empty-database migration plumbing.

## Guiding idea

The system should answer a different question from the public web:

- Search engines / public AI: **What exists on the internet?**
- Douyin Knowledge: **What did the past version of me think was worth saving, what has that knowledge become over time, and how can it help me now?**

The repository remains specification-first: implementation should make the documented model real rather than silently redefining it.
