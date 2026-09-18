# Douyin Knowledge

> Turn saved short-form content into a searchable, explainable, reusable personal knowledge system.

Douyin Knowledge is a local-first personal knowledge project that starts from Douyin collections. The goal is not to build another downloader or another folder of AI summaries, but to transform content you once considered worth saving into knowledge that can be searched, synthesized, acted on, compounded over time, and resurfaced when it becomes relevant again.

## Current status

The system runs end to end. In its default configuration it uses a fixture capture provider and a mock AI provider, so you can install it and watch the whole loop — sync, policy, processing, extraction, wiki integration, retrieval, cited answers — without a Douyin session or an API key. Real providers (OpenAI for chat/embedding/vision, a Douyin capture sidecar) are implemented and selected by configuration; neither has been exercised against a live endpoint.

Delivered: phases 0–9 and 11–13 of `docs/TASKS.md`, with 12 partial. Not started: on-demand enrichment, resurfacing, export/backup, and the evaluation suite. `docs/TASKS.md` section 0 is the honest inventory, and `docs/DECISIONS.md` records where the implementation departs from the design docs.

The first version is anchored on Douyin collections, while the long-term architecture should allow other capture channels such as Xiaohongshu, YouTube, web pages, screenshots, and articles.

## Quick start

```bash
cp .env.example .env          # defaults are demo mode: no key, no cookies, no real data

cd backend
pip install -e '.[dev]'
python -m douyin_knowledge.cli db upgrade
python -m douyin_knowledge.cli sync --process --wait   # builds the fixture corpus
python -m douyin_knowledge.cli ask "我收藏里有哪家茶餐厅"
```

That last command prints an answer with a citation table. If the citations are empty, something is wrong — an uncited answer is not a feature of this system.

For the UI:

```bash
cd frontend && npm install
cd .. && scripts/dev.sh --seed     # API + worker + Vite together
```

Then open http://127.0.0.1:5173. The dev script runs all three processes because the failure mode of forgetting the worker is silent: `sync` enqueues jobs, nothing runs them, and the library just looks empty.

To serve everything from one process instead, `npm run build` in `frontend/` and the API will serve the bundle at http://127.0.0.1:8787 (`DK_SERVE_FRONTEND`, on by default).

`dk doctor` checks the install — migrations, directories, provider configuration, ffmpeg — and never prints a key.

## Configuration and secrets

Configuration is environment variables with a `DK_` prefix, documented in `.env.example`; `backend/tests/test_env_example.py` fails if that file drifts from the real settings, because a wrong example is worse than no example — it breaks at the first step a new user takes.

Nothing secret belongs in this repository: no Douyin cookies or session tokens, no API keys, no credentials, and no real collection data. `.env` is gitignored, and `GET /api/admin/settings` returns `openai_api_key` as a boolean rather than a value.

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
- [`docs/TASKS.md`](docs/TASKS.md) — phased implementation plan, acceptance tests, and milestone order. Section 0 records what is actually delivered.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — where the implementation differs from the design docs, with the symbol proving each claim.
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
numpy .npz      ← rebuildable vector index (LanceDB optional; see DEC-012)
       +
SQLite worker queue
       ↓
Adaptive AI/media pipeline
       ↓
Douyin capture sidecar
```

The system is intentionally local-first and avoids unnecessary V1 infrastructure such as Redis, Celery, PostgreSQL, Kafka, graph databases, or microservices.

## Working on this

Read `AGENTS.md` first; it holds the invariants that must not be quietly renegotiated — the provenance spine, the rule that a Source carries no AI-derived fields, that a Claim is never a global fact, that an uncited wiki statement is a defect.

`docs/TASKS.md` section 0 says what exists. `docs/DECISIONS.md` says where the code and the design docs disagree, and it is the code that wins. If you find an entry in either file that no longer matches reality, fix the document in the same change: a stale status section is read as a description of the system, and it lies.

```bash
cd backend && python -m pytest -q && python -m ruff check . && python -m mypy src
cd frontend && npm run typecheck && npm run build
npm run probe                                 # client vs a live server on :8787
```

The probe exists because every payload-shape bug in this project came from a plausible guess about a response, not from a missing endpoint. Read the route before writing the type.

## Guiding idea

The system should answer a different question from the public web:

- Search engines / public AI: **What exists on the internet?**
- Douyin Knowledge: **What did the past version of me think was worth saving, what has that knowledge become over time, and how can it help me now?**

The repository remains specification-first: implementation should make the documented model real rather than silently redefining it.
