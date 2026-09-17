# Douyin Knowledge — SQLite Physical Schema V1

Status: Living document  
Phase: Physical data design  
Version: 0.1  
Last updated: 2026-09-17

---

## 1. Purpose

This document maps the conceptual model in `DATA_SCHEMA.md`, the adaptive pipeline in `AI_PIPELINE.md`, the Processing Policy, Retrieval architecture, and Compounding Wiki into a concrete SQLite V1 schema.

The schema is designed for a local-first, single-user application. It deliberately distinguishes:

1. durable source/provenance data;
2. mutable operational state;
3. versioned AI-derived knowledge;
4. canonical entities and personal state;
5. rebuildable search/index state;
6. rebuildable Compounding Wiki state.

Core rule:

> SQLite is the authoritative local database. FTS indexes, vector indexes, Wiki Markdown exports, and generated summaries are rebuildable projections.

---

## 2. SQLite Conventions

### 2.1 IDs

Use application-generated sortable string IDs, preferably UUIDv7 or ULID, stored as `TEXT`.

Examples:

```text
src_...
ev_...
run_...
ent_...
clm_...
wiki_...
job_...
```

Human-readable prefixes are optional implementation details, but IDs must be globally unique and stable.

### 2.2 Time

Store timestamps as UTC epoch milliseconds in `INTEGER` columns.

```text
created_at_ms
updated_at_ms
published_at_ms
```

Convert to local/user time only at the UI boundary.

### 2.3 JSON

Use JSON-in-TEXT only for values that are:

- provider/model specific;
- sparse or evolving;
- not common hard-filter fields;
- naturally nested.

Where supported, add `CHECK (json_valid(...))`.

Do **not** hide frequently queried concepts such as source IDs, entity types, claim predicates, policy targets, processing states, or ratings inside JSON.

### 2.4 SQLite runtime settings

V1 should enable:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
```

Only one background worker writes heavy pipeline output at first, reducing lock contention.

### 2.5 Deletion philosophy

A source disappearing from Douyin is **not** a delete event.

Instead:

```text
sources.availability = 'unavailable'
```

Actual local purge is an explicit user action.

---

## 3. Trust and Rebuild Boundaries

### Durable / authoritative

These survive model/provider changes:

```text
creators
collections
sources
source_snapshots
source_collection_memberships
source_assets (retained assets)
processing_rules
policy_decisions
evidence_units
entities
entity_aliases
entity_external_ids
user state / annotations
```

### Versioned derived knowledge

These are retained for reproducibility but may be regenerated:

```text
processing_runs
knowledge_items
entity_mentions
claims
retrieval_chunks
wiki integration/revisions
```

### Rebuildable indexes/projections

These can be dropped and rebuilt:

```text
search_documents / FTS5
LanceDB vectors
wiki_links
Markdown exports
cached entity profiles
```

---

# Part A — Capture and Source Layer

## 4. `creators`

Canonical platform creator/account metadata.

```text
id                  TEXT PK
platform            TEXT NOT NULL
external_creator_id TEXT NOT NULL
display_name        TEXT
handle              TEXT
profile_url         TEXT
avatar_url          TEXT
raw_json            TEXT
first_seen_at_ms    INTEGER NOT NULL
last_seen_at_ms     INTEGER NOT NULL
```

Constraint:

```text
UNIQUE(platform, external_creator_id)
```

Indexes:

```text
(platform, display_name)
(handle)
```

Creator display names are not identity keys.

---

## 5. `collections`

Original platform collection/folder metadata.

```text
id                     TEXT PK
platform               TEXT NOT NULL
external_collection_id TEXT NOT NULL
name                   TEXT NOT NULL
description            TEXT
raw_json               TEXT
first_seen_at_ms       INTEGER NOT NULL
last_synced_at_ms      INTEGER
```

Constraint:

```text
UNIQUE(platform, external_collection_id)
```

These represent the user's original organization, not the canonical knowledge taxonomy.

---

## 6. `sources`

One captured external item.

```text
id                   TEXT PK
platform             TEXT NOT NULL
external_id          TEXT NOT NULL
source_type          TEXT NOT NULL
creator_id           TEXT FK creators.id NULL
title                TEXT
caption_raw          TEXT
source_url           TEXT
cover_url            TEXT
published_at_ms      INTEGER NULL
saved_at_ms          INTEGER NULL
duration_ms          INTEGER NULL
availability         TEXT NOT NULL DEFAULT 'available'
latest_snapshot_id   TEXT NULL
first_seen_at_ms     INTEGER NOT NULL
last_seen_at_ms      INTEGER NOT NULL
locally_deleted_at_ms INTEGER NULL
```

Constraint:

```text
UNIQUE(platform, external_id)
```

Suggested `source_type` values:

```text
video
image_album
article
image
other
```

Suggested `availability` values:

```text
available
unavailable
unknown
```

Important:

- No AI summary fields belong here.
- No processing-policy decision should overwrite source metadata.
- `locally_deleted_at_ms` is only for an explicit local soft delete/purge flow.

---

## 7. `source_snapshots`

Immutable snapshots of provider metadata returned during synchronization.

```text
id             TEXT PK
source_id      TEXT NOT NULL FK sources.id ON DELETE CASCADE
fetched_at_ms  INTEGER NOT NULL
content_hash   TEXT NOT NULL
raw_json       TEXT NOT NULL
```

Index:

```text
(source_id, fetched_at_ms DESC)
```

Why keep snapshots:

- upstream metadata may change;
- source availability can change;
- platform parsing logic can improve;
- debugging ingestion should not depend on only the latest normalized row.

Repeated identical snapshots may be deduplicated by `(source_id, content_hash)`.

---

## 8. `source_collection_memberships`

Many-to-many relation between captured sources and original platform collections.

```text
source_id         TEXT FK sources.id ON DELETE CASCADE
collection_id     TEXT FK collections.id ON DELETE CASCADE
first_seen_at_ms  INTEGER NOT NULL
last_seen_at_ms   INTEGER NOT NULL
is_present        INTEGER NOT NULL DEFAULT 1
PRIMARY KEY(source_id, collection_id)
```

Do not immediately delete a membership when the source disappears from one folder. Mark `is_present = 0` so historical organization can be preserved.

---

## 9. `source_assets`

Local media/evidence files associated with a source.

```text
id               TEXT PK
source_id        TEXT NOT NULL FK sources.id ON DELETE CASCADE
asset_type       TEXT NOT NULL
retention_class  TEXT NOT NULL
storage_key      TEXT NOT NULL
mime_type        TEXT
byte_size        INTEGER
sha256           TEXT
duration_ms      INTEGER
width            INTEGER
height           INTEGER
timestamp_ms     INTEGER NULL
created_at_ms    INTEGER NOT NULL
expires_at_ms    INTEGER NULL
```

Suggested `asset_type`:

```text
video
audio
image
keyframe
subtitle
other
```

Suggested `retention_class`:

```text
cache
retained
user_pinned
```

`storage_key` should be relative to the configured data root, not an absolute machine-specific path.

V1 default: full video/audio are typically `cache`; selected keyframes/subtitles may be `retained`.

---

# Part B — Processing Policy and Operational State

## 10. `processing_rules`

User-configured or system-suggested rules deciding whether a source enters knowledge processing.

```text
id                    TEXT PK
name                  TEXT
is_enabled            INTEGER NOT NULL DEFAULT 1
rule_type             TEXT NOT NULL
action                TEXT NOT NULL
priority              INTEGER NOT NULL DEFAULT 0
target_source_id      TEXT NULL FK sources.id
target_creator_id     TEXT NULL FK creators.id
target_collection_id  TEXT NULL FK collections.id
matcher_json          TEXT NULL
origin                TEXT NOT NULL DEFAULT 'user'
created_at_ms         INTEGER NOT NULL
updated_at_ms         INTEGER NOT NULL
```

Suggested `rule_type`:

```text
source
creator
collection
metadata
semantic
```

Suggested `action`:

```text
process
metadata_only
always_process
```

Suggested `origin`:

```text
user
system_suggestion
migration
```

Examples of `matcher_json`:

```json
{"title_contains":["电影片段","综艺"]}
```

or:

```json
{"semantic_labels":["movie_clip","variety_clip"]}
```

Indexes should exist for each target foreign key plus `(is_enabled, rule_type)`.

Rule precedence is resolved in application logic, not by encoding a brittle hierarchy into SQL.

---

## 11. `policy_decisions`

Audit log of policy evaluation per source.

```text
id                TEXT PK
source_id         TEXT NOT NULL FK sources.id ON DELETE CASCADE
rule_id           TEXT NULL FK processing_rules.id ON DELETE SET NULL
phase             TEXT NOT NULL
action            TEXT NOT NULL
reason_code       TEXT
explanation_json  TEXT
model_name        TEXT NULL
created_at_ms     INTEGER NOT NULL
```

Suggested `phase`:

```text
metadata
semantic
manual_override
```

This preserves why a source was skipped or processed.

---

## 12. `source_processing_state`

One mutable operational row per source.

```text
source_id                   TEXT PK FK sources.id ON DELETE CASCADE
current_policy_action       TEXT NOT NULL DEFAULT 'process'
current_policy_decision_id  TEXT NULL FK policy_decisions.id
processing_status           TEXT NOT NULL DEFAULT 'pending'
desired_level               INTEGER NOT NULL DEFAULT 1
achieved_level              INTEGER NOT NULL DEFAULT 0
current_processing_run_id   TEXT NULL
last_success_at_ms          INTEGER NULL
last_error_json             TEXT NULL
updated_at_ms               INTEGER NOT NULL
```

Suggested `processing_status`:

```text
pending
metadata_only
queued
processing
ready
failed
stale
```

This table prevents mutable pipeline state from polluting `sources`.

---

# Part C — Processing Runs and Evidence

## 13. `processing_runs`

Versioned execution of source understanding/enrichment.

```text
id                 TEXT PK
source_id          TEXT NOT NULL FK sources.id ON DELETE CASCADE
run_kind           TEXT NOT NULL
processor_version  TEXT NOT NULL
schema_version     TEXT NOT NULL
target_level       INTEGER NOT NULL
achieved_level     INTEGER NOT NULL DEFAULT 0
status             TEXT NOT NULL
models_json        TEXT
config_json        TEXT
started_at_ms      INTEGER NOT NULL
finished_at_ms     INTEGER NULL
error_json         TEXT NULL
```

Suggested `run_kind`:

```text
initial
enrichment
reprocess
repair
```

Suggested `status`:

```text
queued
running
completed
failed
cancelled
```

Old completed runs are retained. `source_processing_state.current_processing_run_id` identifies the run used by normal retrieval.

---

## 14. `evidence_units`

Durable citation-sized source evidence.

```text
id               TEXT PK
source_id        TEXT NOT NULL FK sources.id ON DELETE CASCADE
asset_id         TEXT NULL FK source_assets.id ON DELETE SET NULL
kind             TEXT NOT NULL
start_ms         INTEGER NULL
end_ms           INTEGER NULL
raw_text         TEXT NULL
normalized_text  TEXT NULL
observation_json TEXT NULL
language         TEXT NULL
confidence       REAL NULL
content_hash     TEXT NOT NULL
created_at_ms    INTEGER NOT NULL
```

Suggested `kind`:

```text
caption
native_subtitle
transcript
ocr
keyframe
visual_observation
creator_metadata
```

A `ProcessingRun` consumes evidence but should not own it.

Therefore use a join table rather than putting `processing_run_id` directly on every evidence row.

---

## 15. `processing_run_evidence`

```text
processing_run_id TEXT FK processing_runs.id ON DELETE CASCADE
evidence_id       TEXT FK evidence_units.id ON DELETE CASCADE
usage_role        TEXT NOT NULL DEFAULT 'input'
PRIMARY KEY(processing_run_id, evidence_id)
```

This allows reprocessing to reuse previously acquired transcript/OCR/keyframes without duplicating evidence.

---

## 16. `retrieval_chunks`

Semantic-search-sized text chunks derived from one processing run.

```text
id                 TEXT PK
source_id          TEXT NOT NULL FK sources.id ON DELETE CASCADE
processing_run_id  TEXT NOT NULL FK processing_runs.id ON DELETE CASCADE
chunk_type         TEXT NOT NULL
ordinal            INTEGER NOT NULL
text               TEXT NOT NULL
start_ms           INTEGER NULL
end_ms             INTEGER NULL
content_hash       TEXT NOT NULL
created_at_ms      INTEGER NOT NULL
```

Constraint:

```text
UNIQUE(processing_run_id, chunk_type, ordinal)
```

`retrieval_chunks` optimize recall/context completeness. They are not citation units.

---

## 17. `retrieval_chunk_evidence`

```text
retrieval_chunk_id TEXT FK retrieval_chunks.id ON DELETE CASCADE
evidence_id        TEXT FK evidence_units.id ON DELETE CASCADE
PRIMARY KEY(retrieval_chunk_id, evidence_id)
```

This preserves the path:

```text
RetrievalChunk → EvidenceUnit → Source
```

---

# Part D — Source-Level Knowledge

## 18. `knowledge_items`

One interpreted source-level view per processing run.

```text
id                 TEXT PK
source_id          TEXT NOT NULL FK sources.id ON DELETE CASCADE
processing_run_id  TEXT NOT NULL FK processing_runs.id ON DELETE CASCADE
generated_title    TEXT
summary            TEXT
key_points_json    TEXT
processing_level   INTEGER NOT NULL
coverage_json      TEXT NOT NULL
confidence         REAL NULL
created_at_ms      INTEGER NOT NULL
```

Constraint:

```text
UNIQUE(processing_run_id)
```

The current KnowledgeItem is determined through the current processing run, not an independent mutable `is_current` flag.

---

## 19. `knowledge_item_labels`

Indexed multi-dimensional Domain/Form labels.

```text
knowledge_item_id TEXT FK knowledge_items.id ON DELETE CASCADE
label_type        TEXT NOT NULL
value             TEXT NOT NULL
confidence        REAL NULL
PRIMARY KEY(knowledge_item_id, label_type, value)
```

Suggested `label_type`:

```text
domain
form
semantic_route
```

Index:

```text
(label_type, value)
```

This is intentionally simpler than a formal ontology.

---

## 20. `topics`

Canonical reusable semantic themes.

```text
id             TEXT PK
name           TEXT NOT NULL
normalized_name TEXT NOT NULL
description    TEXT
created_at_ms  INTEGER NOT NULL
updated_at_ms  INTEGER NOT NULL
```

Constraint:

```text
UNIQUE(normalized_name)
```

Topics remain lightweight. Do not build a taxonomy tree in V1 unless real usage proves it necessary.

---

## 21. `knowledge_item_topics`

```text
knowledge_item_id TEXT FK knowledge_items.id ON DELETE CASCADE
topic_id          TEXT FK topics.id ON DELETE CASCADE
confidence        REAL NULL
PRIMARY KEY(knowledge_item_id, topic_id)
```

---

# Part E — Entities and Claims

## 22. `entities`

Canonical real-world or reusable conceptual objects.

```text
id                    TEXT PK
entity_type           TEXT NOT NULL
subtype               TEXT
canonical_name        TEXT NOT NULL
normalized_name       TEXT NOT NULL
profile_json          TEXT NULL
status                TEXT NOT NULL DEFAULT 'active'
merged_into_entity_id TEXT NULL FK entities.id ON DELETE SET NULL
created_at_ms         INTEGER NOT NULL
updated_at_ms         INTEGER NOT NULL
```

Suggested `status`:

```text
active
merged
archived
```

Do not store creator opinions such as price/quality directly as authoritative entity columns unless they later become explicitly verified user/system facts.

---

## 23. `entity_aliases`

```text
id               TEXT PK
entity_id        TEXT NOT NULL FK entities.id ON DELETE CASCADE
alias            TEXT NOT NULL
normalized_alias TEXT NOT NULL
language         TEXT NULL
origin           TEXT NOT NULL
created_at_ms    INTEGER NOT NULL
```

Constraint:

```text
UNIQUE(entity_id, normalized_alias)
```

Suggested `origin`:

```text
source
model
user
external_provider
```

Index `normalized_alias` for resolution/search.

---

## 24. `entity_external_ids`

Strong identity signals used during entity resolution.

```text
id          TEXT PK
entity_id   TEXT NOT NULL FK entities.id ON DELETE CASCADE
namespace   TEXT NOT NULL
value       TEXT NOT NULL
created_at_ms INTEGER NOT NULL
```

Constraint:

```text
UNIQUE(namespace, value)
```

Examples:

```text
douyin_place_id
maps_place_id
official_domain
product_sku
isbn
```

---

## 25. `entity_mentions`

Source-local mentions before/after canonical resolution.

```text
id                    TEXT PK
source_id             TEXT NOT NULL FK sources.id ON DELETE CASCADE
processing_run_id     TEXT NOT NULL FK processing_runs.id ON DELETE CASCADE
mention_text          TEXT NOT NULL
normalized_text       TEXT NOT NULL
entity_type_hint      TEXT NULL
context_json          TEXT NULL
resolved_entity_id    TEXT NULL FK entities.id ON DELETE SET NULL
resolution_status     TEXT NOT NULL DEFAULT 'unresolved'
resolution_confidence REAL NULL
created_at_ms         INTEGER NOT NULL
```

Suggested `resolution_status`:

```text
unresolved
candidate
resolved
disputed
```

Never delete the original mention after resolution.

---

## 26. `entity_mention_evidence`

```text
entity_mention_id TEXT FK entity_mentions.id ON DELETE CASCADE
evidence_id       TEXT FK evidence_units.id ON DELETE CASCADE
PRIMARY KEY(entity_mention_id, evidence_id)
```

---

## 27. `claims`

Atomic source-attributed assertions.

```text
id                 TEXT PK
source_id          TEXT NOT NULL FK sources.id ON DELETE CASCADE
processing_run_id  TEXT NOT NULL FK processing_runs.id ON DELETE CASCADE
subject_entity_id  TEXT NULL FK entities.id ON DELETE SET NULL
subject_topic_id   TEXT NULL FK topics.id ON DELETE SET NULL
subject_text       TEXT NULL
predicate          TEXT NOT NULL
object_entity_id   TEXT NULL FK entities.id ON DELETE SET NULL
object_topic_id    TEXT NULL FK topics.id ON DELETE SET NULL
value_type         TEXT NOT NULL
value_text         TEXT NULL
value_number       REAL NULL
value_json         TEXT NULL
unit               TEXT NULL
currency           TEXT NULL
claim_kind         TEXT NOT NULL
provenance_type    TEXT NOT NULL
attribution        TEXT NOT NULL
confidence         REAL NULL
valid_from_ms      INTEGER NULL
valid_to_ms        INTEGER NULL
created_at_ms      INTEGER NOT NULL
```

Suggested `value_type`:

```text
text
number
range
boolean
entity
topic
json
```

Suggested `claim_kind`:

```text
reported_attribute
creator_opinion
recommendation
instruction
prediction
speculation
relationship
```

Suggested `provenance_type`:

```text
explicit_source_statement
on_screen_text
visual_observation
model_inference
```

Suggested `attribution`:

```text
creator
source_text
model
unknown
```

At least one subject representation must exist:

```text
subject_entity_id OR subject_topic_id OR subject_text
```

A numeric price claim should remain a Claim even if later used for structured filtering.

Indexes:

```text
(subject_entity_id, predicate)
(subject_topic_id, predicate)
(predicate)
(source_id)
(processing_run_id)
```

Normal retrieval uses claims belonging to the source's current processing run.

---

## 28. `claim_evidence`

```text
claim_id     TEXT FK claims.id ON DELETE CASCADE
evidence_id  TEXT FK evidence_units.id ON DELETE CASCADE
support_role TEXT NOT NULL DEFAULT 'supports'
PRIMARY KEY(claim_id, evidence_id, support_role)
```

Suggested `support_role`:

```text
supports
context
contradicts
```

The core provenance path becomes:

```text
Claim → ClaimEvidence → EvidenceUnit → Source
```

---

# Part F — Personal State

## 29. `entity_user_states`

Current personal state for actionable entities.

```text
entity_id         TEXT PK FK entities.id ON DELETE CASCADE
state             TEXT NULL
rating            REAL NULL
note              TEXT NULL
attributes_json   TEXT NULL
first_action_at_ms INTEGER NULL
last_action_at_ms INTEGER NULL
updated_at_ms     INTEGER NOT NULL
```

State vocabulary is entity-type specific in application logic.

Examples:

```text
place: saved / want_to_go / visited / favorite / disliked
tool: want_to_try / using / abandoned
product: considering / purchased / recommended / regretted
```

---

## 30. `knowledge_item_user_states`

Personal state concerning the saved content itself.

```text
knowledge_item_id TEXT PK FK knowledge_items.id ON DELETE CASCADE
state             TEXT NULL
note              TEXT NULL
attributes_json   TEXT NULL
updated_at_ms     INTEGER NOT NULL
```

Examples:

```text
want_to_read
reviewed
learning
completed
archived
```

---

## 31. `user_annotations`

Append-only personal observations/history.

```text
id                TEXT PK
source_id         TEXT NULL FK sources.id ON DELETE CASCADE
entity_id         TEXT NULL FK entities.id ON DELETE CASCADE
knowledge_item_id TEXT NULL FK knowledge_items.id ON DELETE CASCADE
annotation_type   TEXT NOT NULL
text              TEXT NULL
value_json        TEXT NULL
occurred_at_ms    INTEGER NULL
created_at_ms     INTEGER NOT NULL
```

At least one target foreign key must be present.

Examples:

```text
visit_note
personal_opinion
correction
reminder
manual_tag
```

This keeps personal history separate from source/creator claims.

---

# Part G — Compounding Wiki

## 32. `wiki_integration_runs`

One Wiki compile/update operation.

```text
id                    TEXT PK
trigger_type          TEXT NOT NULL
trigger_source_id     TEXT NULL FK sources.id ON DELETE SET NULL
processing_run_id     TEXT NULL FK processing_runs.id ON DELETE SET NULL
status                TEXT NOT NULL
selected_pages_json   TEXT NULL
model_name            TEXT NULL
prompt_version        TEXT NULL
context_stats_json    TEXT NULL
started_at_ms         INTEGER NOT NULL
finished_at_ms        INTEGER NULL
error_json            TEXT NULL
```

Suggested `trigger_type`:

```text
source_ingest
batch_ingest
manual
maintenance
rebuild
```

`context_stats_json` should record enough information to benchmark Prompt pruning over time.

---

## 33. `wiki_pages`

Stable Wiki page identity.

```text
id               TEXT PK
page_type        TEXT NOT NULL
canonical_key    TEXT NOT NULL
title            TEXT NOT NULL
slug             TEXT NOT NULL
entity_id        TEXT NULL FK entities.id ON DELETE SET NULL
topic_id         TEXT NULL FK topics.id ON DELETE SET NULL
source_id        TEXT NULL FK sources.id ON DELETE SET NULL
status           TEXT NOT NULL DEFAULT 'active'
created_at_ms    INTEGER NOT NULL
updated_at_ms    INTEGER NOT NULL
```

Suggested `page_type`:

```text
entity
concept
topic
synthesis
source_digest
index
```

Constraint:

```text
UNIQUE(canonical_key)
```

`canonical_key` is an internal stable identifier and should not depend solely on a mutable title or filesystem path.

---

## 34. `wiki_revisions`

Immutable revisions of a Wiki page.

```text
id                  TEXT PK
page_id             TEXT NOT NULL FK wiki_pages.id ON DELETE CASCADE
integration_run_id  TEXT NULL FK wiki_integration_runs.id ON DELETE SET NULL
revision_no         INTEGER NOT NULL
content_markdown    TEXT NOT NULL
summary             TEXT NULL
frontmatter_json    TEXT NULL
content_hash        TEXT NOT NULL
is_current          INTEGER NOT NULL DEFAULT 1
created_at_ms       INTEGER NOT NULL
```

Constraint:

```text
UNIQUE(page_id, revision_no)
```

Use a partial unique index so only one revision per page is current:

```sql
CREATE UNIQUE INDEX ux_wiki_revision_current
ON wiki_revisions(page_id)
WHERE is_current = 1;
```

Wiki Markdown files on disk are exports of current revisions, not a separate source of truth.

---

## 35. `wiki_supports`

Maps individual Wiki statements/sections back to structured provenance.

```text
id               TEXT PK
wiki_revision_id TEXT NOT NULL FK wiki_revisions.id ON DELETE CASCADE
statement_key    TEXT NOT NULL
claim_id         TEXT NULL FK claims.id ON DELETE SET NULL
evidence_id      TEXT NULL FK evidence_units.id ON DELETE SET NULL
source_id        TEXT NULL FK sources.id ON DELETE SET NULL
support_role     TEXT NOT NULL DEFAULT 'supports'
created_at_ms    INTEGER NOT NULL
```

At least one of `claim_id`, `evidence_id`, or `source_id` must be present.

`statement_key` is an internal stable marker generated during integration, for example:

```text
core_fact:price:1
section:current_bottlenecks:item:2
summary:1
```

This is more reliable than trying to infer provenance later from free-form Markdown.

---

## 36. `wiki_links`

Rebuildable page-to-page links parsed from current revisions.

```text
from_page_id      TEXT FK wiki_pages.id ON DELETE CASCADE
to_page_id        TEXT FK wiki_pages.id ON DELETE CASCADE
link_type         TEXT NOT NULL DEFAULT 'related'
source_revision_id TEXT FK wiki_revisions.id ON DELETE CASCADE
PRIMARY KEY(from_page_id, to_page_id, link_type, source_revision_id)
```

`wiki_links` are a projection and may be fully rebuilt from Markdown/revisions.

---

## 37. `wiki_lint_findings`

Detected Wiki quality problems.

```text
id                TEXT PK
page_id           TEXT NULL FK wiki_pages.id ON DELETE CASCADE
revision_id       TEXT NULL FK wiki_revisions.id ON DELETE CASCADE
finding_type      TEXT NOT NULL
severity          TEXT NOT NULL
status            TEXT NOT NULL DEFAULT 'open'
details_json      TEXT NOT NULL
detected_at_ms    INTEGER NOT NULL
resolved_at_ms    INTEGER NULL
```

Suggested statuses:

```text
open
fixed
ignored
obsolete
```

Examples of finding types:

```text
unsupported_statement
broken_link
duplicate_page
orphan_page
stale_page
missing_required_section
conflicting_claims
invalid_entity_link
index_mismatch
```

---

## 38. `quality_rules`

The persistent "wrong-answer notebook" / prompt-governance rules derived from repeated failures.

```text
id                  TEXT PK
rule_key            TEXT NOT NULL UNIQUE
finding_type        TEXT NOT NULL
instruction         TEXT NOT NULL
status              TEXT NOT NULL DEFAULT 'open'
pass_count          INTEGER NOT NULL DEFAULT 0
fail_count          INTEGER NOT NULL DEFAULT 0
last_triggered_at_ms INTEGER NULL
created_at_ms       INTEGER NOT NULL
updated_at_ms       INTEGER NOT NULL
```

Suggested status:

```text
open
monitoring
closed
```

Open/monitoring rules may be injected into relevant LLM prompts.

---

## 39. `quality_rule_samples`

Concrete samples attached to a quality rule.

```text
id                TEXT PK
quality_rule_id   TEXT NOT NULL FK quality_rules.id ON DELETE CASCADE
finding_id        TEXT NULL FK wiki_lint_findings.id ON DELETE SET NULL
source_id         TEXT NULL FK sources.id ON DELETE SET NULL
wiki_page_id      TEXT NULL FK wiki_pages.id ON DELETE SET NULL
context_json      TEXT NULL
is_fixed          INTEGER NOT NULL DEFAULT 0
created_at_ms     INTEGER NOT NULL
fixed_at_ms       INTEGER NULL
```

Samples preserve local context so later repair does not need to rediscover everything globally.

---

## 40. `quality_ledger`

Append-only repair/maintenance history.

```text
id              TEXT PK
finding_id      TEXT NULL FK wiki_lint_findings.id ON DELETE SET NULL
quality_rule_id TEXT NULL FK quality_rules.id ON DELETE SET NULL
action_type     TEXT NOT NULL
actor           TEXT NOT NULL
before_json     TEXT NULL
after_json      TEXT NULL
model_name      TEXT NULL
created_at_ms   INTEGER NOT NULL
```

Suggested `action_type`:

```text
auto_fix
llm_repair
manual_fix
rule_created
rule_closed
rebuild
```

Suggested `actor`:

```text
code
llm
user
system
```

This supports later `ledger-report` style analysis.

---

# Part H — Search and Vector Index Tracking

## 41. `search_documents`

Unified rebuildable text-search projection.

```text
id            TEXT PK
doc_type      TEXT NOT NULL
object_id     TEXT NOT NULL
title         TEXT NULL
body          TEXT NOT NULL
metadata_json TEXT NULL
content_hash  TEXT NOT NULL
updated_at_ms INTEGER NOT NULL
```

Constraint:

```text
UNIQUE(doc_type, object_id)
```

Possible `doc_type`:

```text
source
knowledge_item
retrieval_chunk
entity
claim
wiki_page
```

A single unified projection simplifies FTS rebuilds and ranking experiments.

---

## 42. `search_fts`

SQLite FTS5 virtual table backed by `search_documents`.

Conceptually:

```sql
CREATE VIRTUAL TABLE search_fts USING fts5(
  title,
  body,
  content='search_documents',
  content_rowid='rowid',
  tokenize='unicode61'
);
```

The exact tokenizer configuration should be benchmarked for Chinese text before being frozen.

FTS is derived state and can be regenerated from the authoritative/derived objects above.

---

## 43. `vector_documents`

Tracks which logical documents have vectors in the external local vector index.

```text
id               TEXT PK
doc_type         TEXT NOT NULL
object_id        TEXT NOT NULL
content_hash     TEXT NOT NULL
embedding_model  TEXT NOT NULL
embedding_dim    INTEGER NOT NULL
vector_key       TEXT NOT NULL
indexed_at_ms    INTEGER NOT NULL
```

Constraint:

```text
UNIQUE(doc_type, object_id, embedding_model)
```

The actual vector lives in LanceDB. This SQLite row provides synchronization and stale-index detection.

When `content_hash` or `embedding_model` changes, re-embed.

---

# Part I — Conversation and Citations

## 44. `conversations`

```text
id             TEXT PK
title          TEXT
created_at_ms  INTEGER NOT NULL
updated_at_ms  INTEGER NOT NULL
```

---

## 45. `messages`

```text
id                 TEXT PK
conversation_id    TEXT NOT NULL FK conversations.id ON DELETE CASCADE
role               TEXT NOT NULL
content            TEXT NOT NULL
knowledge_scope    TEXT NULL
query_plan_json    TEXT NULL
response_meta_json TEXT NULL
created_at_ms      INTEGER NOT NULL
```

Suggested `knowledge_scope`:

```text
personal_required
general
hybrid
```

Do not stuff full raw evidence packets into the message row. Keep traceable citations separately.

---

## 46. `message_citations`

```text
id            TEXT PK
message_id    TEXT NOT NULL FK messages.id ON DELETE CASCADE
ordinal       INTEGER NOT NULL
source_id     TEXT NULL FK sources.id ON DELETE SET NULL
evidence_id   TEXT NULL FK evidence_units.id ON DELETE SET NULL
claim_id      TEXT NULL FK claims.id ON DELETE SET NULL
wiki_page_id  TEXT NULL FK wiki_pages.id ON DELETE SET NULL
label         TEXT NULL
created_at_ms INTEGER NOT NULL
```

At least one provenance target must be present.

This makes chat provenance queryable rather than burying citation IDs in Markdown text.

---

## 47. `conversation_state`

Rebuildable short-term reference state for follow-ups.

```text
conversation_id TEXT PK FK conversations.id ON DELETE CASCADE
state_json      TEXT NOT NULL
updated_at_ms   INTEGER NOT NULL
```

Possible contents:

```json
{
  "mentioned_entity_ids": [],
  "mentioned_source_ids": [],
  "active_filters": {},
  "active_topic_ids": []
}
```

This is conversational state, not personal long-term knowledge.

---

# Part J — Background Work

## 48. `jobs`

SQLite-backed durable job queue.

```text
id              TEXT PK
job_type        TEXT NOT NULL
status          TEXT NOT NULL
priority        INTEGER NOT NULL DEFAULT 0
source_id       TEXT NULL FK sources.id ON DELETE CASCADE
payload_json    TEXT NULL
dedupe_key      TEXT NULL UNIQUE
attempt         INTEGER NOT NULL DEFAULT 0
max_attempts    INTEGER NOT NULL DEFAULT 3
available_at_ms INTEGER NOT NULL
locked_at_ms    INTEGER NULL
locked_by       TEXT NULL
created_at_ms   INTEGER NOT NULL
started_at_ms   INTEGER NULL
finished_at_ms  INTEGER NULL
last_error_json TEXT NULL
```

Suggested statuses:

```text
queued
running
succeeded
failed
cancelled
```

Suggested job types:

```text
sync_collections
sync_collection_sources
process_source
enrich_source
reprocess_source
cleanup_cache
rebuild_fts
rebuild_vectors
wiki_integrate
wiki_lint
wiki_maintain
export_markdown
```

Priority convention can reserve higher bands for active-user/query-triggered work.

---

## 49. `job_events`

Append-only diagnostic progress/events.

```text
id            TEXT PK
job_id        TEXT NOT NULL FK jobs.id ON DELETE CASCADE
event_type    TEXT NOT NULL
message       TEXT NULL
data_json     TEXT NULL
created_at_ms INTEGER NOT NULL
```

Useful for UI progress, debugging, and postmortems without putting large logs in the main job row.

---

# Part K — Application Settings

## 50. `app_settings`

Non-secret user/application settings.

```text
key           TEXT PK
value_json    TEXT NOT NULL
updated_at_ms INTEGER NOT NULL
```

Examples:

```text
processing.default_level
media.retention_policy
wiki.maintenance_interval
retrieval.personal_first
```

Secrets such as API keys, Douyin cookies, or provider credentials should not be stored here in plaintext. Prefer environment variables, OS keychain/secret storage, or the capture sidecar's own credential storage.

---

# Part L — Important Query Patterns

## 51. Exact collection query

User:

```text
我收藏过哪些旺角人均100以下的日料？
```

Primary path:

```text
entities
→ claims(predicate = cuisine/location/average_price)
→ current processing runs
→ claim_evidence
→ evidence_units
→ sources
```

Do not make the Wiki or vector index the only way to answer structured constraints.

---

## 52. Concept synthesis query

User:

```text
我最近收藏的 Agent 内容主要有什么共同观点？
```

Primary path:

```text
wiki_pages / current wiki_revisions
+
knowledge_items / topics
```

Fallback / verification:

```text
claims
retrieval_chunks
claim_evidence
```

---

## 53. Source-specific detail query

User:

```text
那条视频具体推荐了什么菜？
```

Primary path:

```text
conversation_state
→ source/entity resolution
→ claims
→ evidence_units
```

If coverage is insufficient, enqueue `enrich_source`.

---

## 54. Metadata-only/excluded query

User:

```text
我之前跳过过哪些综艺片段？
```

Primary path:

```text
sources
→ source_processing_state(current_policy_action = metadata_only)
→ policy_decisions
```

Excluded sources are discoverable as metadata but are not treated as understood knowledge.

---

# Part M — Entity Merge Strategy

## 55. Never destructive-merge entity history

When entity B is determined to be the same as entity A:

```text
entities.B.status = 'merged'
entities.B.merged_into_entity_id = A
```

Then migrate/resolution-link future reads to A.

Do not immediately delete B because old mentions, claims, citations, and debugging history may reference it.

A maintenance job may later rewrite foreign keys safely while retaining a merge audit trail.

---

# Part N — Reprocessing Strategy

## 56. Processing runs are immutable history

When a better model/schema reprocesses a source:

```text
old processing_run remains
new processing_run created
source_processing_state.current_processing_run_id = new run
```

Old KnowledgeItems, claims, entity mentions, and retrieval chunks remain linked to the old run for reproducibility.

Normal retrieval filters to the current run unless explicitly comparing revisions/debugging.

Evidence may be reused across runs.

---

# Part O — V1 Migration Order

## 57. Migration 001 — Core capture and operations

Create:

```text
creators
collections
sources
source_snapshots
source_collection_memberships
source_assets
processing_rules
policy_decisions
source_processing_state
jobs
job_events
app_settings
```

Goal: sync inventory + Processing Policy works before AI processing exists.

---

## 58. Migration 002 — Evidence and source understanding

Create:

```text
processing_runs
evidence_units
processing_run_evidence
retrieval_chunks
retrieval_chunk_evidence
knowledge_items
knowledge_item_labels
topics
knowledge_item_topics
```

Goal: adaptive processing can produce traceable source-level knowledge.

---

## 59. Migration 003 — Entity/Claim layer

Create:

```text
entities
entity_aliases
entity_external_ids
entity_mentions
entity_mention_evidence
claims
claim_evidence
entity_user_states
knowledge_item_user_states
user_annotations
```

Goal: cross-source entity resolution, structured retrieval, and personal state.

---

## 60. Migration 004 — Search and conversation

Create:

```text
search_documents
search_fts
vector_documents
conversations
messages
message_citations
conversation_state
```

Goal: hybrid retrieval and traceable AI chat.

---

## 61. Migration 005 — Compounding Wiki and quality loop

Create:

```text
wiki_integration_runs
wiki_pages
wiki_revisions
wiki_supports
wiki_links
wiki_lint_findings
quality_rules
quality_rule_samples
quality_ledger
```

Goal: persistent synthesis, revisions, provenance-backed Wiki, lint, and prompt-governance feedback loop.

---

# Part P — Deliberately Not Modeled in V1

Do not create these prematurely:

```text
Neo4j / dedicated graph database
formal ontology engine
multi-user tenant tables
cloud billing/account tables
social/collaboration tables
complex recommender feature store
separate microservice databases
one table per content domain
```

Restaurant, travel, tool, learning, and opinion-specific extraction should map into the generic Entity / Claim / KnowledgeItem system plus type-specific JSON only where genuinely necessary.

---

# Part Q — Schema Decisions

## DB-001 — SQLite is the authoritative local database

Markdown and vector storage are projections.

## DB-002 — Source metadata and mutable processing state are separate

`Source` describes the captured item; `source_processing_state` describes what the system is doing with it.

## DB-003 — Evidence is reusable across processing runs

A better LLM should not require re-running ASR/OCR when the evidence already exists.

## DB-004 — Current derived knowledge is selected by current ProcessingRun

Do not mutate old derived rows in place.

## DB-005 — Claims remain source-attributed

Structured filtering over claims does not convert them into unquestioned global facts.

## DB-006 — Entity merge is reversible/auditable

Canonicalization must not destroy mention/history provenance.

## DB-007 — Personal state is physically separate from source claims

The user's experience must never overwrite creator claims.

## DB-008 — Wiki is persistent but rebuildable derived state

Wiki revisions live in SQLite for fast querying/revision history; Markdown is exportable.

## DB-009 — Wiki statements can point to Claims/Evidence

Provenance is stored explicitly through `wiki_supports` rather than inferred from prose after generation.

## DB-010 — Quality governance is first-class data

Lint findings, repeated error rules, samples, and repair history are persisted so prompt behavior can improve over time.

## DB-011 — FTS and vectors are rebuildable projections

They must never contain knowledge that cannot be reconstructed from SQLite's core/derived tables.

## DB-012 — Conversation citations are structured records

The UI should be able to navigate from answer → Claim/Evidence → Source without parsing citation text.

---

## 62. Next Step

After this schema is accepted, implementation planning can safely move to:

```text
TASKS.md
AGENTS.md
```

The remaining implementation-spec work should define:

- project bootstrap sequence;
- migration creation;
- phase-by-phase deliverables;
- interfaces/protocols;
- acceptance tests;
- benchmark/evaluation fixtures;
- exact Codex working rules.
