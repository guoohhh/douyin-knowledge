# Implemented V1 slice and design decisions

This document records what the current code implements. The other design documents describe the broader target system.

## Working flow

`FileCaptureProvider` or the Douyin sidecar 5.1+ REST adapter supplies source metadata. Sync deduplicates by `(platform, external_id)`, retains multiple original collection memberships, records source snapshots, policy decisions, and sync results, and evaluates user rules. Eligible sources enter a SQLite job queue. The worker creates a versioned ProcessingRun, retains caption/transcript EvidenceUnits, optionally transcribes short-caption video with a configured audio API, extracts claims, validates each claim's quoted text against an EvidenceUnit, resolves entities by normalized exact identity, creates KnowledgeItem, updates Wiki revisions and support links, and updates FTS/vector projections. Retrieval combines direct source/entity matching, FTS, and vector scores. Answers expose source, claim, and evidence IDs. Explicit “want to go/try/learn” personal states produce a source-backed resurfacing list. A configured AI endpoint can generate a prose answer from the retrieved claims.

## Decisions differing from the original design

| Original design | Implemented choice | Reason and trade-off |
| --- | --- | --- |
| ~50 physical tables in five migrations | Focused relational tables plus FTS5 in five migrations | A compact slice could be verified end to end. Collection membership, source snapshots, and policy decisions are retained; collection history, topic links, detailed job events, and conversation threads are deferred. A small Wiki quality ledger tracks deterministic lint findings and fixes. |
| LanceDB vector index | SQLite JSON vector projection with cosine search | One local database simplifies installation and rebuild. Exact linear scan is suitable only for a small library; large archives need a vector index. The local hash fallback offers weak similarity, not true semantic understanding. Configured remote embeddings offer semantic matching. |
| ASR/OCR/Vision escalation | Level 1 caption and Level 2 supplied or optional remote speech transcript | HTTPS video is downloaded within a 100 MiB limit, audio is extracted locally, and a configured transcription endpoint supplies text. Media is discarded afterward. OCR, vision, and frame selection are not integrated. Missing evidence fails visibly; the system does not invent transcript content. |
| LLM two-stage Wiki integration | Deterministic entity page compilation from current claims | This makes support and rebuildability easy to verify. Concept/topic/synthesis pages and model-guided integration remain future work. |
| UTC epoch milliseconds | UTC SQLAlchemy DateTime fields | Reduces conversion code for this slice, but differs from the detailed physical schema. Future migrations should standardize timestamps before wider data import. |
| Sidecar lists saved collections | Direct 5.1+ REST adapter for `/api/v1/douyin/user/collections` and `/api/v1/douyin/collection/posts` | The 5.1 release added the needed endpoints. We paginate, normalize, and deduplicate posts; a mocked HTTP contract test passes. A live account and imported identity were unavailable for verification. |

## Trust boundaries

- Source contains captured metadata only; AI summaries are in KnowledgeItem.
- Claim carries source, run, evidence, and optional entity IDs. Empty or unmatched LLM quotes and values absent from their quoted span are dropped; this conservative rule may omit legitimate paraphrases.
- UserState is a separate table and cannot overwrite creator claims.
- Wiki revisions point to claims and can be recompiled from current claims.
- `metadata_only` sources remain visible in the library but cannot contribute to normal answers.
- Personal answers with no cited evidence state that evidence is insufficient.

## Operations and quality

ProcessingRun records model identity and output counts without storing prompts or secrets. Worker and sync loop emit JSON logs with event and source/job/run identifiers. Wiki lint detects broken, missing, and stale supports; `dk wiki-lint` records findings, and `dk wiki-fix` recompiles deterministic entity pages and resolves repaired findings. This is a limited quality loop, not model-guided page maintenance. The UI exposes search, recent sync results, and job errors.

## Known gaps

The sidecar adapter has not been exercised against a real account. Continuous sync requires a separately running poll loop; there is no managed scheduler, OCR/Vision adapter, frame selection, historical collection membership revisions, typed price/location constraints, model cost ledger or full conversation memory. The media adapter has been tested through an injected transcriber, not against a real account or transcription service. The local fallback extractor yields sentence-level source claims but does not reliably identify entities in arbitrary saves; fixture annotations and the configured model path demonstrate entity integration. The search planner is deterministic and intentionally limited; it does not generate SQL.
