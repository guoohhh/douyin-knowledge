# Implemented V1 slice and design decisions

This document records what the current code implements. The other design documents describe the broader target system.

## Working flow

`FileCaptureProvider` or the Douyin sidecar 5.1+ REST adapter supplies source metadata. Sync deduplicates by `(platform, external_id)`, retains multiple original collection memberships, and evaluates user rules. Eligible sources enter a SQLite job queue. The worker creates a versioned ProcessingRun, retains caption/transcript EvidenceUnits, extracts claims, validates each claim's quoted text against an EvidenceUnit, resolves entities by normalized exact identity, creates KnowledgeItem, updates Wiki revisions and support links, and updates FTS/vector projections. Retrieval combines direct source/entity matching, FTS, and vector scores. Answers expose source, claim, and evidence IDs. A configured AI endpoint can generate a prose answer from the retrieved claims.

## Decisions differing from the original design

| Original design | Implemented choice | Reason and trade-off |
| --- | --- | --- |
| ~50 physical tables in five migrations | 17 focused relational tables plus FTS5 in two migrations | A small text-first slice could be verified end to end. Collection membership is retained; collection history, snapshots, topic links, job events, granular policy audit, conversation threads, and quality ledger are deferred. |
| LanceDB vector index | SQLite JSON vector projection with cosine search | One local database simplifies installation and rebuild. Exact linear scan is suitable only for a small library; large archives need a vector index. The local hash fallback offers weak similarity, not true semantic understanding. Configured remote embeddings offer semantic matching. |
| ASR/OCR/Vision escalation | Level 1 caption and supplied transcript only | Media acquisition and ASR/OCR adapters are not integrated yet. Missing evidence fails visibly; the system does not invent transcript content. |
| LLM two-stage Wiki integration | Deterministic entity page compilation from current claims | This makes support and rebuildability easy to verify. Concept/topic/synthesis pages and model-guided integration remain future work. |
| UTC epoch milliseconds | UTC SQLAlchemy DateTime fields | Reduces conversion code for this slice, but differs from the detailed physical schema. Future migrations should standardize timestamps before wider data import. |
| Sidecar lists saved collections | Direct 5.1+ REST adapter for `/api/v1/douyin/user/collections` and `/api/v1/douyin/collection/posts` | The 5.1 release added the needed endpoints. We paginate, normalize, and deduplicate posts; a mocked HTTP contract test passes. A live account and imported identity were unavailable for verification. |

## Trust boundaries

- Source contains captured metadata only; AI summaries are in KnowledgeItem.
- Claim carries source, run, evidence, and optional entity IDs. Invalid LLM claim quotes are dropped.
- UserState is a separate table and cannot overwrite creator claims.
- Wiki revisions point to claims and can be recompiled from current claims.
- `metadata_only` sources remain visible in the library but cannot contribute to normal answers.
- Personal answers with no cited evidence state that evidence is insufficient.

## Known gaps

The sidecar adapter has not been exercised against a real account. Continuous sync requires a separately running poll loop; there is no managed scheduler or ASR/OCR/Vision adapter, media retention, source snapshots, historical collection membership revisions, typed price/location constraints, model cost ledger, quality ledger, or full conversation memory. The local fallback extractor yields sentence-level source claims but does not reliably identify entities in arbitrary saves; fixture annotations and the configured model path demonstrate entity integration. The search planner is deterministic and intentionally limited; it does not generate SQL.
