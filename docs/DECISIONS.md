# Implementation Decisions

This document records where the implementation differs from, or resolves ambiguity in, the design docs. It is the authoritative reference for how the system was actually built.

**Status**: Living document
**Last updated**: 2026-09-18

A note on how to read this file. Every entry states a claim about current code and names the file that proves it. An earlier version of this log described the state of the codebase at the end of the first implementation pass and was never revised as the work continued, so ten of its eleven entries had become false — it asserted that the policy repository was a stub, that entity aliases were unused, that citations were not rendered, and that extractors hardcoded `gpt-4o-mini`, none of which was still true. A stale decision log is worse than no decision log: it is read as a description of the system and it lies. So each entry below carries the symbol you can check it against, and an entry that no longer matches the code should be rewritten or deleted rather than left standing.

---

## Resolved during implementation

### DEC-001: Processing policy is persisted and auditable

**Decision**: `PolicyRepository` performs real SQL against `processing_rules` and `policy_decisions`. Every evaluation writes a decision row, including the ones that resolve to the default action.

**Rationale**: POL-004 requires that any processing decision be explainable from the record. Recording only the exceptions would make "why was this skipped?" answerable but "why was this processed?" a matter of inference.

**Evidence**: `policy/repository.py:PolicyRepository` (`list_rules`, `save_rule`, `record_decision`, `get_latest_decision`, `delete_rule`, `set_rule_enabled`, `list_decisions`, `purge_decisions`); exposed by `api/routes/admin.py` under `/api/admin/policy/`, and by `dk policy list|why|exclude`.

**Note**: rules match on internal ids (`creator_id`, `source_id`, `collection_id`), not on display names. A creator can rename themselves; the rule must survive that.

---

### DEC-002: Entity merge trusts exact normalized identity only

**Decision**: Automatic merge happens on exact match of the normalized form, including registered aliases. Fuzzy similarity is computed and surfaced but never merges on its own.

**Rationale**: ENT-002. A wrong merge is close to unrecoverable from the user's point of view — two distinct restaurants become one entity with a page that mixes their prices — while a missed merge is a visible, fixable duplicate.

**Evidence**: `extraction/entity_resolver.py:EntityResolver._exact_match` joins `EntityAlias.normalized_alias`; `_ensure_alias` registers each new surface form so the next mention of it matches exactly rather than fuzzily.

**Open**: no test covers alias-driven resolution specifically. The path is exercised incidentally by the pipeline tests.

---

### DEC-003: Citations are inline markers resolved to a citation set

**Decision**: The answer text carries `[n]` markers; the turn returns a parallel list of citations keyed by ordinal. Markers the model emits without a matching citation are rejected at generation time.

**Rationale**: DEC-006 in PRODUCT_SPEC terms — an answer must be traceable. A citation list detached from the sentences it supports lets a reader see that sources exist without being able to tell which sentence came from which.

**Evidence**: `conversation/citation_builder.py:Citation.marker`, `CitationSet.by_chunk`/`by_claim`; `conversation/answer_generator.py:_render_claims`/`_render_excerpts` and `_validate_citation_markers`. Rendered as focusable controls in `frontend/src/pages/AskPage.tsx:AnswerText`; a marker with no citation stays inert text rather than becoming a control that leads nowhere.

---

### DEC-004: Indexes are rebuilt by background jobs, incrementally on success

**Decision**: Vector and FTS indexes are compiled views maintained by jobs. `handle_process_source` enqueues `REBUILD_VECTORS` for the source it just processed; `/api/admin/reindex` and `dk reindex` enqueue a full rebuild.

**Rationale**: The indexes are derivable from the spine, so they can always be dropped and rebuilt. Making that the normal path rather than the recovery path means a schema or embedding change is a rebuild, not a migration.

**Evidence**: `jobs/handlers.py:handle_rebuild_vectors` → `search/indexer.py:sync_vectors`; index counts in `api/routes/admin.py:get_stats` under `index`.

---

### DEC-005 / DEC-006: Sync and process are asynchronous, and return job ids

**Decision**: `POST /api/sources/sync` and `POST /api/sources/{id}/process` enqueue work and return `202` with a job id. Neither does work in the request.

**Rationale**: Processing a source involves ASR, OCR and extraction; doing it inside a request would hold a connection for minutes and lose the work if the client disconnected. The job row is also the audit record of the attempt.

**Evidence**: `api/routes/sources.py:sync_sources`, `process_source`; dedupe keys prevent a double-click from queueing twice; `api/routes/admin.py:get_job` is the polling endpoint.

---

### DEC-007: Conflicting claims are kept and marked, never silently resolved

**Decision**: When two sources disagree on the same statement, the wiki page keeps both values and marks the statement as conflicting. Nothing picks a winner.

**Rationale**: The disagreement is information the user wants. Choosing the most recent value would produce a page that looks authoritative and hides that two sources it cites say different things.

**Evidence**: `wiki/composer.py:_compose_statement` joins distinct values and sets `Statement.conflicting`; `as_markdown` renders `⚠️ 存在分歧`.

**Open**: no confidence weighting and no user override. Both are post-V1.

---

### DEC-008: Aliases are searchable because they are indexed, not queried at search time

**Decision**: `search/indexer.py:collect_entity_candidates` writes the canonical name and every alias into the FTS document for the entity. Keyword search is plain FTS5 over `search_documents` with no name-matching special case.

**Rationale**: The alternative considered was matching `preferred_name` with a SQL `LIKE` alongside the FTS query, which would have meant two ranking systems producing one result list, and no way to rank a name hit against a transcript hit.

**Evidence**: `retrieval/keyword_search.py:KeywordSearcher` contains no name matching; `search/indexer.py:collect_entity_candidates`.

---

### DEC-009: Every model choice resolves through settings by role

**Decision**: No call site names a model. Each role — triage, extraction, answer, wiki router, wiki integration, embedding, ASR, OCR — resolves through `Settings.model_for_role`, and the resolved name is recorded on the run or message.

**Rationale**: Extraction and answering have different cost and quality profiles, and a user swapping models must be able to tell which answers came from which. A hardcoded name makes both impossible, and makes the recorded provenance a lie when it disagrees with what actually ran.

**Evidence**: `config/settings.py:Settings.model_for_role` and `resolved_models`; `extraction/entity_extractor.py` and `claim_extractor.py` take a `StructuredModel` and never name one; `conversation/answer_generator.py:GeneratedAnswer.model_name` comes from `response.model` and is persisted in `Message.response_meta_json`. Visible at `/api/admin/settings` under `resolved_models`.

---

### DEC-010: `/api/conversations/ask` rejects unknown fields

**Decision**: `AskRequest` sets `extra="forbid"`.

**Rationale**: The frontend sent `scope` where the model declares `scope_override`. Pydantic's default dropped it, so the request succeeded, the override was ignored, and the answer crossed the knowledge boundary the caller had explicitly restricted — a wrong answer that looked correct, with nothing logged. For a field that controls the personal/general boundary (RET-002), a 422 is the only honest response.

**Evidence**: `api/routes/conversations.py:AskRequest`; `backend/tests/test_api.py:TestAskContract`.

---

### DEC-011: The API serves the built frontend when one is present

**Decision**: With `DK_SERVE_FRONTEND` on and `frontend/dist/index.html` present, the API serves the bundle at `/`, serves `/assets` as files, and falls back to the shell for unmatched non-API paths so client routes deep-link. Unmatched `/api/*` stays a JSON 404.

**Rationale**: One process is the deployment this product wants — it is a local app, not a service. The setting was documented in `settings.py` and in `.env.example` while nothing read it, so the deployment the docs described did not exist.

**Evidence**: `api/__init__.py:_mount_frontend`, `_serves_frontend`; `backend/tests/test_api.py:TestFrontendMount`.

---

### DEC-012: The default vector index is numpy, not LanceDB

**Decision**: `Settings.vector_backend` defaults to `"numpy"`. Vectors live in one `.npz` file and search is a brute-force cosine scan. `"lancedb"` remains a selectable backend.

**Rationale**: This overrides ARCH-006, which named LanceDB the V1 default. One person's saved videos is thousands of chunks, not millions; a full scan over a float32 matrix that size costs well under a millisecond, and it removes a dependency from an app whose whole premise is that you `pip install` it and run it with no daemon. The bookkeeping that would justify a real vector database — which object a vector belongs to, which model produced it, whether it is stale — lives in `vector_documents` either way, so it participates in the same transaction as the rest of the write.

**Consequence**: ARCH-006 and the architecture diagrams in ARCHITECTURE.md still say LanceDB. They describe the intended shape, not the default. `README.md` reflects the numpy default.

**Evidence**: `retrieval/vector_store.py` module docstring and `VectorStore`; `config/settings.py:vector_backend`.

---

### DEC-013: The sidecar contract is read off the sidecar, and pruning requires a complete walk

**Decision**: The `douyin` provider's contract is verified against `Evil0ctal/Douyin_TikTok_Download_API`'s own published OpenAPI document and REST guide, not inferred from our fixtures. Membership pruning is gated on a single boolean — did the page walk finish? — isolated in `capture/pagination.py` and defaulting to "no".

**Rationale**: A fixture that agrees with the client proves only that both were written by the same person. Four contract bugs were live simultaneously, none of which any existing test could fail on, because the fixture provider paginates the way the client expected rather than the way the sidecar does:

1. The next cursor was read from `meta["cursor"]`, which is an *object* (`{"next", "has_more"}`); the flat cursor is on `data`. So a dict was assigned where a string belonged and **the walk never advanced past page 1** — a user with more than 20 saved items in a folder would silently see only the first page, and the rest would then be marked as removed from the collection.
2. `_poll_task` matched states `pending`/`succeeded`. The sidecar emits `queued|running|done|failed`. Every successfully finished task therefore fell through to the error branch, so **the async path could never succeed**.
3. `health()` probed `/api/v1/health`, which does not exist. That path is served by the console's SPA catch-all and answers `200` with HTML regardless of service state — a health check that cannot fail. Readiness is `/readyz`, which also reports per-component status; `/healthz` is liveness.
4. `_map_source` wrote `None` into `statistics`, typed `dict[str, int]`, so **any item arriving without a `stats` block failed validation** and took the whole page down with it.

Separately, `list_collections` read only its first page, capping a user at their most recent folders and making every item inside the remainder invisible to the entire system — which presents as an empty library, not an error.

**Consequence**: The pruning rule is now explicit: an incomplete walk never prunes, and "complete" means the provider stated there was no more, not that we stopped asking. A non-advancing cursor, a `has_more` with no cursor, and exceeding the page cap are all hard failures (`ValidationError`, not retryable) rather than silent truncation — the previous code logged a warning on page-cap exhaustion and then pruned against the partial listing. Partial pages are still persisted, because upserts are non-destructive and re-fetching costs an identity; only the destructive step is skipped. `Collection.last_synced_at_ms` is stamped only on a complete walk, since the API and UI present it as when the folder was last fully read.

**Evidence**: `capture/pagination.py`; `capture/douyin_provider.py` module docstring; `tests/unit/test_collection_pagination.py`, `tests/unit/test_sync_pruning.py`, `tests/unit/test_douyin_provider_transport.py`.

---

## Known gaps

These are true limitations, not deferred decisions. Each is either invisible in normal use or visible and harmless; none is load-bearing for V1 acceptance.

**`QueryPlanner` is dead code.** `retrieval/query_planner.py` implements entity extraction with regex place-name patterns and carries its own `TODO: use an NER model`. It is exported from `retrieval/__init__.py` and instantiated nowhere. The live path resolves follow-ups in `conversation/conversation_manager.py:_resolve_followup` using the previous turn's entities from conversation state, which is what the product actually needed. `Settings.enable_query_enrichment` and `query_planner_model` are likewise read by nothing. Either wire it or delete it; leaving it exported invites someone to assume retrieval does NER.

**Alias resolution has no dedicated test.** See DEC-002.

**Source comments cite a `DEC-C*` numbering that this file does not use.** `db/models/ops.py` (DEC-C1), `db/models/wiki.py` (DEC-C7), `jobs/types.py` (DEC-C8), `search/tokenizer.py` and `retrieval/keyword_search.py` (DEC-C10), and `capture/douyin_provider.py` (DEC-C11) reference decision ids from an earlier scheme; this file numbers DEC-001 onward, and the two sets do not correspond. The comments are still individually accurate about *what* was decided — only the cross-reference dangles. Not renumbered here because it touches unrelated modules during a behavioral pass; new references use the live scheme.

**Wiki conflict handling has no confidence weighting or user override.** See DEC-007.

**Media retention is configured but coarse.** `media_retention` chooses a policy for all sources; per-collection retention is not implemented.

**`storage_key` holds the provider's absolute URL, so ASR never runs on real media.** `capture/sync.py` writes the remote URL into `SourceAsset.storage_key` instead of a path under `DK_DATA_DIR/media`. Level 2 on the `douyin` provider therefore has nothing local to transcribe. Demo mode is unaffected and reaches level 2, because the fixtures carry an inline `native_subtitle` and never need ASR — which is exactly why the whole test suite stays green while the real-provider path is broken. Fixing it means deciding where download belongs (its own job, most likely) rather than renaming a field.

**A failed run loses its partial output, and only says so in a count.** The worker's `session_scope` rolls back on any exception, so evidence, mentions, claims and chunks produced before a failure are destroyed and must be re-derived — at the cost of re-running paid ASR and extraction — on retry. The *audit* now survives (see below), and records how much was discarded in `processing_runs.config_json.discarded_partial_output`, but the work itself does not. Making a retry cheap needs checkpoint/resume: per-level completion tracking, idempotent re-entry, and a staleness rule for when cached evidence must be discarded anyway because `processor_version` or the model set changed. That is a design change, not a bug fix, and it is not in V1.

Two smaller notes on the fix that did land: `discarded_partial_output` is written into `config_json`, which is the wrong column semantically — that field describes a run's configuration, not its outcome — and it was chosen to avoid a migration late in the cycle. And `AuditReplay.apply` has an idempotent update branch for callers that commit rather than roll back (the API route path); only the worker path is covered by a test.

**`KnowledgeItem` has a table and no writer.** Nothing in the codebase inserts into it. It is unreferenced by retrieval, wiki and the API, so it is inert rather than wrong, but its presence in the schema implies a feature that does not exist.

**The uncited-wiki-page rule is structurally unreachable.** WIKI-002 says an uncited page is a defect, and the builder only ever composes statements from `WikiSupport` rows, so the invalid state cannot currently be constructed. The check that would catch it therefore proves nothing. It is kept because a future writer that composes prose from a model rather than from supports would need it.

**Wiki pages are indexed but not retrieved.** `search/indexer.py` writes wiki documents into the FTS index, while `retrieval/retriever.py` filters to chunk document types, so a query never surfaces a wiki page even though the index contains one. The effect is a missed surface, not a wrong answer.

**`JsonText` has NUMERIC affinity on SQLite.** `JSON()` contains none of SQLite's affinity keywords, so columns declared with it get NUMERIC rather than TEXT, contrary to what the type's own comment claims. Measured impact is narrow: dicts, lists, strings and booleans round-trip exactly, because JSON serialization quotes them. Only a *bare top-level numeric scalar* drifts — `1.0` returns as `1`, and integers beyond float precision lose digits. The exposed columns are the `value_json` fields that can hold a scalar claim value. Not changed here because altering the column type at freeze time would rewrite 44 columns across both migrations for a case no current writer is known to hit; the honest statement is that the comment is wrong and the risk is latent.
