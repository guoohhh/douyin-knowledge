# Douyin Knowledge

A local-first knowledge app for saved short-form content. It keeps source material, evidence, source-attributed claims, personal state, and a rebuildable Wiki separate.

## V1 status

A runnable **text-first vertical slice with optional speech transcription** is implemented. JSON capture and a fixture work without credentials. A configured OpenAI-compatible endpoint can extract structured claims, create embeddings, and synthesize answers; without it, deterministic local extraction and retrieval keep the demo usable. The UI supports Ask My Saves, library/source evidence, entity and personal state, Wiki, intent-based resurfacing, search, task status, policy rules, and JSON import.

The sidecar adapter reads Douyin bookmark folders and their posts from [`Douyin_TikTok_Download_API` 5.1+](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/releases/tag/v5.1.0) using an imported identity. This path has a deterministic HTTP contract test but has **not been tested against a live Douyin account** in this environment. No Douyin reverse-engineering code or cookies are stored here. Speech transcription is available for short or empty captions with an API key; OCR and vision remain outstanding. Continuous sync requires running the separate sync loop.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js 20+ and pnpm for the frontend
- SQLite with FTS5 (standard Python build on supported platforms)

## Start from a clean checkout

In one terminal:

```bash
cd backend
uv venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/uvicorn douyin_knowledge.api:app --host 127.0.0.1 --port 8001
```

In a second terminal:

```bash
cd backend
.venv/bin/dk worker
```

In a third terminal:

```bash
cd frontend
pnpm install
pnpm dev
```

Open `http://127.0.0.1:5173`. The default database is `backend/data/douyin_knowledge.db`; local data and secrets are ignored by Git.

## Demo flow

The fixture is synthetic and contains no private saves.

```bash
cd backend
.venv/bin/dk sync-file ../fixtures/saves.json
.venv/bin/dk worker --once
.venv/bin/dk worker --once
.venv/bin/dk worker --once
```

Ask “我收藏的旺角日料人均多少？” in the UI. Open the citation to inspect the exact evidence and source. Add a rule for collection `待看影视` in Settings **before importing** to see the `metadata_only` path. Rules can also be added later; exclusion immediately removes processed sources from normal knowledge retrieval without erasing previous runs.

Questions without an explicit personal cue use general scope and require a configured AI provider for a general answer. A successful sidecar sync marks previously synced saves absent from its latest listing as `removed`; they leave current answers while history remains available.

API endpoints: `/health`, `/dashboard`, `/sync`, `/sync/sidecar`, `/sync/status`, `/sources`, `/rules`, `/jobs`, `/search`, `/ask`, `/entities`, `/resurface`, `/wiki`. Interactive API docs: `http://127.0.0.1:8001/docs`.

## Configuration

Copy `.env.example` if useful and export variables in the backend shell. The app does not read `.env` implicitly.

- `DK_DATABASE_URL`: SQLite URL.
- `DK_OPENAI_API_KEY`: enables remote extraction, embedding, and answer synthesis.
- `DK_OPENAI_BASE_URL`: optional OpenAI-compatible base URL.
- `DK_OPENAI_MODEL`: chat/extraction model.
- `DK_OPENAI_EMBEDDING_MODEL`: embedding model. Rebuild the index after changing it.
- `DK_OPENAI_ASR_MODEL`: audio transcription model (default `whisper-1`); the configured endpoint must support `/audio/transcriptions`.
- `DK_ASR_CAPTION_THRESHOLD`: transcribe media when its caption has fewer characters than this threshold (default 80) and no supplied transcript exists. Media downloads and redirects require HTTPS, are capped at 100 MiB, and are deleted after audio transcription. Expired media URLs are reported in the processing run; caption evidence remains usable.
- `DK_SIDECAR_URL`, `DK_SIDECAR_API_KEY`, `DK_SIDECAR_IDENTITY`: URL, API key, and imported Douyin identity ID for a 5.1+ sidecar. The API key needs Douyin read and identity management permissions. Run `dk sync-sidecar` after configuring them. The sidecar owns session cookies.

To poll for new saves while the app is running, keep `cd backend && .venv/bin/dk sync-loop --interval-seconds 300` running alongside the worker. This process needs an available sidecar and stops when you stop it.

The sidecar key is sent only to the sidecar and is never written to SQLite. Keep all keys out of Git.

## Maintenance and checks

```bash
cd backend
.venv/bin/pytest -q
.venv/bin/ruff check --select E,F,I --ignore E501 douyin_knowledge tests
.venv/bin/dk rebuild-index
.venv/bin/dk wiki-lint
.venv/bin/dk wiki-fix
.venv/bin/dk wiki-rebuild
```

```bash
cd frontend
pnpm build
```

Sync history records successful imports and provider failures; the Settings page shows the latest events. The Wiki and indexes are projections. Claims and evidence remain in SQLite. Reprocessing creates a new `ProcessingRun`; older runs are retained. Failed jobs record an error and retry with backoff up to three attempts. Processing runs record provider, model, evidence/claim counts, and a summary excerpt. Wiki lint records open and resolved quality events; wiki-fix recompiles affected pages into new revisions.

## Design and implementation notes

The original specifications remain in `docs/`. [Current architecture and review guide](docs/CURRENT_ARCHITECTURE.md) explains the implemented data flow, invariants, operations, verification, and known gaps. [Implementation decisions](docs/IMPLEMENTATION.md) records differences from the aspirational physical schema. In particular, the current local vector projection uses SQLite JSON instead of LanceDB and a lightweight token hash fallback instead of a semantic model. With an embedding API configured, the same projection stores real model embeddings and searches by cosine similarity.
