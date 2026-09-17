# Douyin Knowledge

A local-first knowledge app for saved short-form content. It keeps source material, evidence, source-attributed claims, personal state, and a rebuildable Wiki separate.

## V1 status

A runnable **text-first vertical slice** is implemented. JSON capture and a fixture work without credentials. A configured OpenAI-compatible endpoint can extract structured claims, create embeddings, and synthesize answers; without it, deterministic local extraction and retrieval keep the demo usable. The UI supports Ask My Saves, library/source evidence, entity and personal state, Wiki, policy rules, and JSON import.

Automatic sync from a live Douyin account is **not yet available**. The current upstream `Douyin_TikTok_Download_API` v5 documentation does not expose a confirmed saved-collection listing endpoint. `SidecarCaptureProvider` accepts a separate normalized collection-export bridge endpoint when one is available. No Douyin reverse-engineering code or cookies are stored here. ASR/OCR/media enrichment and scheduled sync are also outstanding.

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

API endpoints: `/health`, `/dashboard`, `/sync`, `/sync/sidecar`, `/sources`, `/rules`, `/jobs`, `/search`, `/ask`, `/entities`, `/wiki`. Interactive API docs: `http://127.0.0.1:8001/docs`.

## Configuration

Copy `.env.example` if useful and export variables in the backend shell. The app does not read `.env` implicitly.

- `DK_DATABASE_URL`: SQLite URL.
- `DK_OPENAI_API_KEY`: enables remote extraction, embedding, and answer synthesis.
- `DK_OPENAI_BASE_URL`: optional OpenAI-compatible base URL.
- `DK_OPENAI_MODEL`: chat/extraction model.
- `DK_OPENAI_EMBEDDING_MODEL`: embedding model. Rebuild the index after changing it.
- `DK_SIDECAR_URL`, `DK_SIDECAR_API_KEY`, `DK_SIDECAR_SAVES_PATH`: normalized collection-export bridge. The endpoint must return an array in the same shape as `fixtures/saves.json`.

The sidecar key is sent only to the bridge and is never written to SQLite. Keep all keys out of Git.

## Maintenance and checks

```bash
cd backend
.venv/bin/pytest -q
.venv/bin/ruff check --select E,F,I --ignore E501 douyin_knowledge tests
.venv/bin/dk rebuild-index
.venv/bin/dk wiki-lint
.venv/bin/dk wiki-rebuild
```

```bash
cd frontend
pnpm build
```

The Wiki and indexes are projections. Claims and evidence remain in SQLite. Reprocessing creates a new `ProcessingRun`; older runs are retained. Failed jobs record an error and retry with backoff up to three attempts.

## Design and implementation notes

The original specifications remain in `docs/`. [Implementation decisions](docs/IMPLEMENTATION.md) records actual V1 scope and differences from the aspirational physical schema. In particular, the current local vector projection uses SQLite JSON instead of LanceDB and a lightweight token hash fallback instead of a semantic model. With an embedding API configured, the same projection stores real model embeddings and searches by cosine similarity.
