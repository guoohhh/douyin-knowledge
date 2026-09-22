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

### DEC-014: A media asset's remote URL, its local path, and whether it was fetched are three different columns

**Decision**: `SourceAsset` separates `remote_url` (where the bytes came from), `storage_key` (a machine-independent relative path under `DK_DATA_DIR/media` naming where they live locally), and `download_state` (whether they are actually on disk). Acquisition is its own job type, `acquire_media`, placed between capture and processing. Asset identity across re-signing is a `remote_url_fingerprint` over **host and path only**.

**Rationale**: `capture/sync.py` wrote `storage_key = media.url`, i.e. the provider's absolute signed URL, into a field the orchestrator resolves as `media_dir / storage_key`. Joining a directory with a URL yields a path that cannot exist, so every real source silently recorded `asr_skipped_media_not_downloaded` — silently because a missing transcript is not a run failure. No test could catch it: fixtures carry an inline `native_subtitle`, so demo mode reaches level 2 without ever asking for media. One column cannot carry three meanings, which is why this is a schema change and not the field rename the old gap note contemplated.

Fingerprinting host and path only is the load-bearing subtlety. Douyin re-signs media URLs on every capture, so a fingerprint over the whole URL would report "content changed" on every sync and re-download everything forever. The cost is that two assets on the same source with the same `asset_type`, distinguished purely by query parameters, would collide — the narrow and unlikely case, whereas re-signing is the guaranteed one.

Acquisition is a separate job so that a sync of a thousand items does not block on a thousand downloads, each download carries its own retry budget, and the Processing Policy gets to veto the spend. Policy is re-evaluated inside the handler rather than trusted from whoever enqueued it: this is the most expensive step in the system, and a user who excludes a creator while the queue drains must not be billed for the bandwidth.

**Consequence**: A matched fingerprint means the same remote file was re-signed, so only `remote_url` moves; overwriting `download_state` there would discard a good local copy on every sync and make ASR re-run forever. A *changed* fingerprint means the upload was genuinely replaced, so the stale local file is deleted and its row marked `unavailable` — terminal, which keeps it out of the acquisition queue while preserving the historical record. Leaving it `ready` would let ASR transcribe superseded content and attach that evidence to the current source.

Downloads are HTTPS-only with the scheme re-checked at every redirect hop (httpx's own follower would happily follow `https → http`), redirects are walked manually under a hard hop limit, and the byte cap is enforced mid-stream rather than trusted from `Content-Length`. Bytes land in a `.part` file and are atomically replaced into position, because a half-written file that *looks* present would be handed to ASR and its garbage stored as evidence. `MediaDownloadFailed` is retryable; `MediaTooLarge` and `MediaAssetGone` subclass `ValidationError` and are not, so the queue's existing taxonomy decides retry policy rather than the downloader.

This safety envelope is the one clear win from the GPT implementation, reimplemented inside this data model rather than copied. Three adaptations were deliberate: GPT downloaded into a `TemporaryDirectory` inside the transcribe call, so every retry re-fetched and a crash left nothing resumable; it raised bare `ValueError`/`RuntimeError`, which this queue would classify non-retryable and fail permanently; and it wrote straight to the destination. Its `ffmpeg` transcode step and its caption-based `should_transcribe` heuristic were **not** adopted — `ffmpeg` is not a declared dependency, and cheap triage belongs to the content-type policy work.

**Evidence**: `media/store.py`, `media/downloader.py`, `media/service.py`; `migrations/versions/0003_media_acquisition_state.py`; `jobs/handlers.py:handle_acquire_media`; `tests/unit/test_media_store.py`, `test_media_downloader.py`, `test_media_acquisition.py`, `test_media_jobs.py`, `test_media_migration_backfill.py`, `test_media_asr_closes_loop.py`.

---

### DEC-015: Exclusion is a visibility change projected onto `SourceProcessingState`, not a deletion

**Decision**: The Processing Policy's verdict for a source is materialised on `source_processing_state.current_policy_action` (with `current_policy_decision_id` pointing at the decision that produced it), and the three read paths that assemble current knowledge — `retrieval/retriever.py`, `search/indexer.py`, `wiki/builder.py` — filter on it in addition to `current_processing_run_id`. A `PolicyReconciler` re-evaluates affected sources whenever a rule is created, patched, deleted, or applied at processing time. Nothing is deleted: runs, evidence, mentions and claims stay on disk and simply stop being reachable.

**Rationale**: both columns existed from the first migration with nothing reading or writing either one. Policy was purely a pre-processing gate, so excluding a source that had *already* been processed had no observable effect whatsoever — its chunks stayed in the index, its evidence stayed citable, and its claims kept feeding wiki statements. The gate is necessary but not sufficient, because a rule is a statement about a library, not about a moment in a queue.

Writing the verdict onto the state row rather than evaluating rules inside each read path is what makes exclusion take effect at query time instead of at the next reindex, which is what "immediately disappears from normal retrieval" requires. It also gives the gate and the reconciler a single shared answer; without the projection in `jobs/handlers.py`, a source excluded at processing time would still read `process` in its state while retrieval filtered on the state.

**Consequence**: `HIDDEN_ACTIONS` contains both `exclude` and `metadata_only`. Both actions prevent a source's knowledge from reaching normal retrieval, the wiki, or current answers. `metadata_only` stops before knowledge processing, so its content was never actually understood (PROCESSING_POLICY §12). `exclude` is a hard block. Both preserve historical runs/evidence/claims if they exist, so rule reversal restores eligibility without reprocessing, and the run/evidence/claim id sets are identical before and after. While either action is current, the source remains discoverable through explicit metadata search but not through knowledge retrieval.

`affected_source_ids` is computed from a rule's *target*, not its action, so disabling an exclude rule affects exactly the sources it used to name. It must be called **before** a delete, because afterwards there is no target to read and the call degrades to a whole-corpus walk — correct but needlessly expensive on a large library. Collection rules deliberately include past memberships, so an item pulled out of a folder also stops being governed by that folder's rule. A decision row is recorded only when the action actually changes, otherwise every rule edit would bury the "why is this hidden?" audit under "still eligible" noise.

The indexer refuses to *write* excluded sources rather than relying on the reader's filter. That is redundant by design: a single dropped filter in one read path should not be able to resurface excluded content through the keyword or vector route. Reindexing after a reversal puts the source back, since nothing was destroyed.

This is the GPT implementation's product semantics — a rule change reaches back over the existing library — adapted to this data model rather than copied. GPT enforced exclusion by not writing the data in the first place, which is unavailable here and undesirable: it makes reversal lossy and destroys the audit trail the brief requires to survive.

**Evidence**: `policy/reconciler.py`; the policy filter in `retrieval/retriever.py`, `search/indexer.py`, `wiki/builder.py`; `_apply_policy` in `jobs/handlers.py`; `api/routes/admin.py` (rule create/patch/delete plus `POST /policy/reconcile`); `cli/commands_query.py` (`policy exclude`, `policy reconcile`); `tests/unit/test_policy_retroactive.py`, `test_policy_visibility.py`, `test_policy_queued_and_api.py`, `tests/test_api.py:TestPolicyReconciliation`.

---

### DEC-016: Content type is derived, cached, cue-first state — and never enough on its own to skip a source

**Decision**: A source's semantic content type is computed by `policy/triage.py` from a single assembled `SourceSignal` (`policy/signal.py`), cached in its own `source_triage` table keyed on the signal's fingerprint, and consumed by `RuleType.SEMANTIC` rules during a second evaluation pass that runs **only** when the deterministic metadata pass reached no verdict *and* at least one enabled semantic rule exists. Classification is deterministic cue matching by default; one structured model call is attempted only when cues are inconclusive, a semantic rule needs the answer, `enable_triage_model_fallback` is on, and the provider is not `mock`.

**Rationale**: two independent defects made the documented policy surface a fiction. `RuleType.SEMANTIC` was accepted, stored, listed and enabled, but could never match anything, because no content-type label existed anywhere in the system. Separately, the hashtag rule documented in `PROCESSING_POLICY.md` §5.5 could never fire either: hashtags are captured into `CapturedSource.hashtags` and joined by a `text_signal()` method that nothing calls, surviving only inside `SourceSnapshot.raw_json`, while the matcher read `Source.caption_raw`. Both failed silently and expensively — the user believes they are skipping variety clips while paying to process every one of them.

Ordering is the whole point. An exclusion that only fires after ASR has run has already spent the money it was meant to save, so the shape is `metadata → deterministic policy → cheap triage when needed → semantic policy → expensive processing only if allowed`. The short-circuit when no semantic rule is enabled is the cost control, not an optimisation detail.

**Consequence**: confidence is asymmetric on purpose. Entertainment labels need a stronger cue showing than `knowledge`, and cue ties break toward `knowledge` — that is, toward processing — because wrongly skipping loses knowledge silently while wrongly processing only wastes one call. `ContentType.UNKNOWN` can never satisfy a semantic matcher, so a classifier miss cannot become data loss; for the same reason the admin API rejects `content_types: ["unknown"]` with a 422, alongside unknown labels, rather than storing a rule that would never match.

The cache is keyed on `SourceSignal.fingerprint` rather than a timestamp, so a recaption invalidates the label and a creator rename does not (creator name is deliberately excluded from the fingerprint). A cached `unknown` reached without a model is recomputed if a model later becomes available. `CheapTriage.model_calls` exists so a test can assert that **no** model call happened. `policy/factory.py` centralises the wiring because a rule's effect must not depend on which surface evaluated it — the job gate, the reconciler, the API and the CLI all get the same evaluator, and a whole-corpus reconcile stays model-less.

The label is derived state in its own table, not a column on the spine, and triage stays out of the capture path: `GET /api/sources` reads the cache table directly and never classifies on demand, so browsing cannot cost money. `null` and `"unknown"` are kept distinct through the API and the frontend types — never classified is not the same as classified and inconclusive.

Adapted from the GPT implementation rather than copied. GPT's `cheap_semantic_type` is a three-label keyword function called at ingest that writes a `semantic_type` column on `sources`, with rules matched by a flat `dimension`/`value` pair. The product semantics — skip entertainment before paying for it — were adopted. The implementation was not: this version covers all six entertainment labels plus `knowledge` and `unknown`, treats the label as invalidatable cached state rather than a permanent column, and adds the model escalation path and the cost short-circuit that GPT had no notion of.

**Evidence**: `policy/signal.py`, `policy/triage.py`, `policy/triage_service.py`, `policy/factory.py`; `migrations/versions/0004_source_triage.py`; `db/models/policy.py:SourceTriage`; the two-pass `evaluate` in `policy/evaluator.py`; `enable_triage_model_fallback` in `config/settings.py`; `_check_semantic_matcher` in `api/routes/admin.py`; the `triage` block and `content_type` filter in `api/routes/sources.py`; `tests/unit/test_policy_triage.py`, `test_policy_semantic_rules.py`.

---

### DEC-017: Claim grounding is validated before storage, and downgraded claims stay out of derived output

**Decision**: Every model-produced claim is validated by `extraction/grounding.py` before it is written to SQLite. The validator checks that the cited evidence belongs to the claim's source, that a quoted span occurs in the evidence text after normalization, and that literal numeric values are supported. Context mismatches, hallucinated spans missing from the evidence, and unsupported numbers are rejected outright. Claims with an invented span but a plausible assertion are downgraded: stored with `grounding_status="downgraded"` and excluded from the wiki and cited answers by `is_assertable()`, which the wiki builder and retriever both call.

**Rationale**: before this validator, a model that named a real `evidence_id` was trusted. A claim asserting "人均80块" citing evidence that says "人均800块" was stored, and its provenance chain `Claim → ClaimEvidence → EvidenceUnit → Source` made it citable — the wrong number would reach the wiki wearing a real citation. Separately, a model quoting a span that does not appear anywhere in the evidence had that invented text stored in `value_json["evidence_span"]`, which is the audit trail for why a claim is considered grounded but could later become a rendered quotation if a future UI surfaces it.

GPT's validator was a one-line filter: `quote in evidence.text and value in quote`. That shape is correct but the implementation is brittle for CJK: ASR punctuation and model quotes differ (full-width vs half-width commas, trailing periods added or dropped), whitespace collapses inconsistently, and spoken numbers are transcribed as Chinese numerals while the claim stores Arabic digits. Substring matching rejects all of those as hallucinations. This version normalizes (NFKC, punctuation stripped, whitespace collapsed, casefolded) before matching, and parses common Chinese numerals (八十 → 80) so a transcript saying "人均八十块" supports `value_number=80`.

**Consequence**: `grounding_status` has four rejection states and one valid state. `rejected_context` (evidence from another source), `rejected_span` (empty evidence text), and `rejected_value` (unsupported literal number) are hard rejections — the claim never reaches SQLite, the counter `rejected_ungrounded` increments, and a structured log is emitted. `downgraded` (missing or hallucinated span, but assertion may still be fair) stores the claim with `confidence` capped at 0.5 and the span stripped from `value_json`, increments `downgraded_ungrounded`, and records the verdict in `grounding_json`. `valid` passes and records where the span matched (`span_offset`).

A `downgraded` claim is kept as the audit trail for what the model produced — the per-run counters answer "how much did the model make up?" — but `is_assertable(claim.grounding_status)` returns `False`, so it is filtered out by `wiki/builder.py:_live_claims` and `retrieval/retriever.py:_claims_for_chunks`. That makes downgrade observable: a claim with an invented span does not render on a wiki page identically to a verified one. Capping confidence was the first attempt and was decorative — nothing downstream reads `confidence`, so the cap changed nothing the user could see.

`None` is treated as assertable on purpose: claims written before migration 0005 carry no verdict, and treating "not yet validated" as "failed validation" would silently empty the wiki on upgrade. Pre-existing claims are grandfathered until the source is reprocessed.

Value types `boolean`, `date`, `duration`, and `json` are exempt from the literal substring test. A `recommended=true` claim is a derived classification of "我推荐大家去试试", not a quotation of the English word "true", and demanding the latter appear in a Chinese transcript rejects every `recommended` claim in the demo corpus. This exemption was added after the first live demo run dropped 2 of 4 claims and took a wiki page with it — a defect caught by measurement rather than by tests, because the test corpus scripted its own claim payloads and never hit the real extraction prompt.

Adapted from the GPT implementation rather than copied. GPT's shape — validate before storing, and filter the ungrounded — was adopted. Its implementation was not: GPT filtered silently (dropped claims vanish from the extraction log with no counter, no per-claim reason, and no distinction between a bad span and a bad number), and its substring test could not handle the CJK normalization or numeral cases this validator was built to tolerate. The asymmetry between hard rejection and downgrade, the recorded verdicts, the `is_assertable` predicate, and the grandfathering of pre-0005 claims are all new.

**Evidence**: `extraction/grounding.py`, `extraction/claim_extractor.py`; `db/models/entities.py:Claim.grounding_status` and `.grounding_json`; `migrations/versions/0005_claim_grounding.py`; `wiki/builder.py:_live_claims`, `retrieval/retriever.py:_claims_for_chunks`; `tests/unit/test_claim_grounding.py`.

---

### DEC-018: Resurfacing stores user intention separately from creator claims, and re-checks support at read time

**Decision**: The resurfacing loop (`knowledge/resurface.py`) is built on `EntityUserState`, not `KnowledgeItem`. A user marks an entity `want_to_go` / `want_to_try` / `want_to_learn`; that row is the intention. The supporting knowledge shown on each card is fetched separately at read time and routed through `eligible_claims()` (DEC-017, DB-004), so a card can exist with zero eligible support. V1 ranks nothing: cards are ordered by `(-last_action_at_ms, canonical_name)`, which is deterministic and needs no scoring model.

**Rationale**: `KnowledgeItem` has a schema but no writer, so building on it would have meant inventing a second knowledge-production path to make one UI work — the table would have been populated only by the resurface feature and would not have matched anything the processing pipeline produces. `EntityUserState` already exists, is keyed on `entity_id`, and is exactly one row per intention.

Keeping the intention out of `Claim` is the load-bearing part. "I want to go here" is not something a creator said, and writing it as a `Claim` would put user-authored rows into the same table the wiki and retriever read from, making the provenance of every claim ambiguous (KM-003). It would also make the intention subject to policy filtering, which is wrong in the other direction: if the user excludes the source that introduced a restaurant, they have decided they do not want that creator's material — not that they no longer want to go.

**Consequence**: intention and support have different lifetimes on purpose. Excluding or downgrading every source behind an entity to `metadata_only` leaves the card present with its supports emptied, and the UI says so rather than silently rendering a bare name; reversing the policy restores the supports without the user re-saving anything. Where several sources support one entity and only some are excluded, the card shows the eligible remainder. Clearing an intention nulls `state` but keeps `note` and `rating`, so a user who toggles a mark off does not lose what they wrote about why. `first_action_at_ms` is written once and never moved, so "when did I first want this" survives edits.

Because support is recomputed per read rather than denormalized onto the state row, no invalidation path is needed when policy changes — the same property that made the shared eligibility rule worth extracting in the first place.

**Evidence**: `knowledge/resurface.py`, `knowledge/eligibility.py`; `api/routes/knowledge.py` (`/resurface`, `/entities/{id}/user-state`); `db/models/entities.py:EntityUserState`; `tests/test_resurface_api.py`.

---

### DEC-019: A Wiki page with no eligible support is archived, and policy reprojection follows the claim spine

**Decision**: Two changes to how a policy verdict reaches persisted Wiki output.

`WikiBuilder.build_entity_page()` / `build_topic_page()` no longer answer an empty eligible-claim set with `skipped_reason="no_current_claims"` alone. When the subject already has an *active* page, `_retire_page()` sets `WikiPage.status = "archived"` and clears `is_current` on the current `WikiRevision`. Nothing is deleted: every revision and every `WikiSupport` row stays. `WikiLink` rows are cleared, because links are a projection of current knowledge (WIKI-007) rather than provenance.

`PolicyReconciler._reproject_wiki()` derives the affected subjects from `Claim` — subject and object, entity and topic — instead of from `EntityMention` alone, and calls both `build_for_entities()` and the new `build_for_topics()`.

**Rationale**: the invariant belongs in the builder, not in the reconciler, because *any* targeted rebuild should behave correctly when its subject has stopped having current knowledge; special-casing the policy path would leave the same staleness reachable from every other caller. Archiving rather than deleting is the same rule as DEC-015 turned on the Wiki's own output: the page stops being present-tense knowledge, and its past stays auditable.

Reprojection followed mentions because mentions were how entity pages were found. But mentions only ever name entities, and `Topic` is a live page family with a real writer, so a topic page composed from a hidden source's claims was unreachable — it went on citing a source that had already vanished from Ask and from entity pages. Both live families are composed from `Claim`, so the claim spine is the projection that matches what the composer actually reads. Concept, Synthesis and SourceDigest have constants but no writer, so there is nothing persisted to go stale and they are deliberately not handled.

**Consequence**: reversal needs no new code. `_get_or_create_page` already flips an archived page back to `active`, and `commit_revision` finds no current revision and appends the next one, so un-hiding a source restores the page with its revision numbering continuing from history — without reprocessing the source. `wiki_pages_recompiled` now counts retired pages too, since "the rule reached the wiki" is what the number is for.

**Evidence**: `wiki/builder.py` (`_retire_page`, `build_for_topics`); `policy/reconciler.py` (`_reproject_wiki`); `tests/test_wiki_policy_currency.py`.

---

## Known gaps

These are true limitations, not deferred decisions. Each is either invisible in normal use or visible and harmless; none is load-bearing for V1 acceptance.

**Structured query planning is deferred, not missing by accident.** The old `retrieval/query_planner.py` was deleted rather than wired. It extracted entities with regex place-name patterns, carried its own `TODO: use an NER model`, was exported from `retrieval/__init__.py`, and was instantiated nowhere; `Settings.query_planner_model` and `Settings.enable_query_enrichment` were read by nothing and are gone with it. The live path resolves follow-ups in `conversation/conversation_manager.py:_resolve_followup` from the previous turn's entities in conversation state, which is what the product actually needed. Keeping a dead planner exported was worse than having none, because it invited the reader to assume retrieval does NER. Real query planning — parsing a question into filters over entities, attributes and relations — belongs to the Structured Retrieval phase, where it can be designed against the structured index instead of bolted onto keyword search.

**`KnowledgeItem` has a schema but no writer.** The `knowledge_items` table ships in migration 0001 and `KnowledgeItem` is modelled, but nothing creates rows and neither the wiki builder nor the retriever reads it. The table is kept because it is in the initial migration and dropping it buys nothing; it is out of `__all__` so that importing it is a deliberate act. This is why Resurface is built on `EntityUserState` instead (DEC-018) — a feature resting on a table with no writer would be a feature that cannot work.

**Wiki pages are indexed but are not a retrieval surface.** `collect_wiki_candidates` in the indexer writes real `search_documents` rows with `doc_type = 'wiki'`, and no retrieval path selects them: `HybridRetriever` fuses `chunk` documents only. The indexing is not removed because the documents are correct and a future structured/wiki retrieval path will want them; the honest statement is that wiki content today reaches the user through the wiki pages themselves, never through search. The retriever carries a comment saying so.

**Alias resolution has no dedicated test.** See DEC-002.

**Source comments cite a `DEC-C*` numbering that this file does not use.** `db/models/ops.py` (DEC-C1), `db/models/wiki.py` (DEC-C7), `jobs/types.py` (DEC-C8), `search/tokenizer.py` and `retrieval/keyword_search.py` (DEC-C10), and `capture/douyin_provider.py` (DEC-C11) reference decision ids from an earlier scheme; this file numbers DEC-001 onward, and the two sets do not correspond. The comments are still individually accurate about *what* was decided — only the cross-reference dangles. Not renumbered here because it touches unrelated modules during a behavioral pass; new references use the live scheme.

**Wiki conflict handling has no confidence weighting or user override.** See DEC-007.

**Media retention is configured but coarse.** `media_retention` chooses a policy for all sources; per-collection retention is not implemented.

**Audio is handed to ASR without transcoding.** The downloaded file goes to the provider as-is, so a container the ASR backend cannot read fails at transcription rather than being converted first. `ffmpeg` is not a declared dependency and making it one is a packaging decision, not a bug fix; see DEC-014.

**A failed run loses its partial output, and only says so in a count.** The worker's `session_scope` rolls back on any exception, so evidence, mentions, claims and chunks produced before a failure are destroyed and must be re-derived — at the cost of re-running paid ASR and extraction — on retry. The *audit* now survives (see below), and records how much was discarded in `processing_runs.config_json.discarded_partial_output`, but the work itself does not. Making a retry cheap needs checkpoint/resume: per-level completion tracking, idempotent re-entry, and a staleness rule for when cached evidence must be discarded anyway because `processor_version` or the model set changed. That is a design change, not a bug fix, and it is not in V1.

Two smaller notes on the fix that did land: `discarded_partial_output` is written into `config_json`, which is the wrong column semantically — that field describes a run's configuration, not its outcome — and it was chosen to avoid a migration late in the cycle. And `AuditReplay.apply` has an idempotent update branch for callers that commit rather than roll back (the API route path); only the worker path is covered by a test.

**`KnowledgeItem` has a table and no writer.** Nothing in the codebase inserts into it. It is unreferenced by retrieval, wiki and the API, so it is inert rather than wrong, but its presence in the schema implies a feature that does not exist.

**The uncited-wiki-page rule is structurally unreachable.** WIKI-002 says an uncited page is a defect, and the builder only ever composes statements from `WikiSupport` rows, so the invalid state cannot currently be constructed. The check that would catch it therefore proves nothing. It is kept because a future writer that composes prose from a model rather than from supports would need it.

**Wiki pages are indexed but not retrieved.** `search/indexer.py` writes wiki documents into the FTS index, while `retrieval/retriever.py` filters to chunk document types, so a query never surfaces a wiki page even though the index contains one. The effect is a missed surface, not a wrong answer.

**`JsonText` has NUMERIC affinity on SQLite.** `JSON()` contains none of SQLite's affinity keywords, so columns declared with it get NUMERIC rather than TEXT, contrary to what the type's own comment claims. Measured impact is narrow: dicts, lists, strings and booleans round-trip exactly, because JSON serialization quotes them. Only a *bare top-level numeric scalar* drifts — `1.0` returns as `1`, and integers beyond float precision lose digits. The exposed columns are the `value_json` fields that can hold a scalar claim value. Not changed here because altering the column type at freeze time would rewrite 44 columns across both migrations for a case no current writer is known to hit; the honest statement is that the comment is wrong and the risk is latent.
