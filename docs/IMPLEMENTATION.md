# Implemented V1 slice and design decisions

This document records what the current code implements. The other design documents describe the broader target system.

## Working flow

`FileCaptureProvider` or a normalized HTTP bridge supplies source metadata. Sync deduplicates by `(platform, external_id)` and evaluates user rules. Eligible sources enter a SQLite job queue. The worker creates a versioned ProcessingRun, retains caption/transcript EvidenceUnits, extracts claims, validates each claim's quoted text against an EvidenceUnit, resolves entities by normalized exact identity, creates KnowledgeItem, updates Wiki revisions and support links, and updates FTS/vector projections. Retrieval combines direct source/entity matching, FTS, and vector scores. Answers expose source, claim, and evidence IDs. A configured AI endpoint can generate a prose answer from the retrieved claims.

## Decisions differing from the original design

| Original design | Implemented choice | Reason and trade-off |
| --- | --- | --- |
| ~50 physical tables in five migrations | 16 focused relational tables plus FTS5 in one initial migration | A small text-first slice could be verified end to end. Collection history, snapshots, topic links, job events, granular policy audit, conversation threads, and quality ledger are deferred. |
| LanceDB vector index | SQLite JSON vector projection with cosine search | One local database simplifies installation and rebuild. Exact linear scan is suitable only for a small library; large archives need a vector index. The local hash fallback offers weak similarity, not true semantic understanding. Configured remote embeddings offer semantic matching. |
| ASR/OCR/Vision escalation | Level 1 caption and supplied transcript only | No dependable media acquisition contract exists yet. Missing evidence fails visibly; the system does not invent transcript content. |
| LLM two-stage Wiki integration | Deterministic entity page compilation from current claims | This makes support and rebuildability easy to verify. Concept/topic/synthesis pages and model-guided integration remain future work. |
| UTC epoch milliseconds | UTC SQLAlchemy DateTime fields | Reduces conversion code for this slice, but differs from the detailed physical schema. Future migrations should standardize timestamps before wider data import. |
| Sidecar lists saved collections | Configurable normalized collection-export bridge | Upstream v5 public documentation currently describes posts/authors/playlists but does not confirm a saved-collection listing endpoint. Live automatic account sync is unverified; JSON import is the reliable V1 capture path. |

## Trust boundaries

- Source contains captured metadata only; AI summaries are in KnowledgeItem.
- Claim carries source, run, evidence, and optional entity IDs. Invalid LLM claim quotes are dropped.
- UserState is a separate table and cannot overwrite creator claims.
- Wiki revisions point to claims and can be recompiled from current claims.
- `metadata_only` sources remain visible in the library but cannot contribute to normal answers.
- Personal answers with no cited evidence state that evidence is insufficient.

## Known gaps

The sidecar bridge contract is not an implemented Douyin favorites API. There is no automatic scheduler, ASR/OCR/Vision adapter, media retention, source snapshots, many-to-many original collections, typed price/location constraints, model cost ledger, quality ledger, or full conversation memory. The local fallback extractor yields sentence-level source claims but does not reliably identify entities in arbitrary saves; fixture annotations and the configured model path demonstrate entity integration. The search planner is deterministic and intentionally limited; it does not generate SQL.
