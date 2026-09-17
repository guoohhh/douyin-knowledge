# Douyin Knowledge — Compounding Wiki Layer

Status: Living document  
Phase: Product / knowledge integration design  
Version: 0.1  
Last updated: 2026-09-17

---

## 1. Purpose

Douyin Knowledge should not behave as a pure RAG system where every query re-reads raw transcript chunks from scratch.

The system should continuously **compile newly captured sources into a persistent, evolving Wiki view** of the user's knowledge.

However, the Wiki is not the source of truth.

The durable trust hierarchy is:

```text
Raw Source
    ↓
EvidenceUnit
    ↓
Claim / Entity / UserState
    ↓
Compounding Wiki
    ↓
Conversation / Synthesis
```

The lower layers are more trustworthy and provenance-oriented. The upper layers are more compressed, useful, and synthetic.

Core principle:

> Compile repeatedly used knowledge ahead of query time, but never lose the path back to structured claims and source evidence.

---

## 2. Why Add a Wiki Layer

Pure retrieval has an important limitation: the system can find information, but it does not necessarily **accumulate understanding**.

Without a persistent synthesis layer:

```text
save source A
save source B
save source C
↓
three independent KnowledgeItems / chunks
↓
every future query re-discovers their relationship
```

With a compounding Wiki:

```text
save source A
↓
create/update topic and entity pages

save source B
↓
revise the same pages
add new claims
add disagreements
add links

save source C
↓
knowledge becomes richer instead of merely larger
```

The Wiki therefore serves as the system's **compiled knowledge representation**.

---

## 3. Relationship to Existing Layers

The Wiki complements, rather than replaces, the existing architecture.

### Structured knowledge layer

Best at:

- exact identity;
- source provenance;
- structured filters;
- user state;
- claim-level attribution;
- conflicting claims;
- machine-readable relations.

Examples:

```text
Entity = restaurant X
Claim A = creator A says average price ≈ HKD 80
Claim B = creator B says average price ≈ HKD 150
UserState = visited, rating 4
```

### Compounding Wiki layer

Best at:

- persistent conceptual summaries;
- cross-source synthesis;
- recurring themes;
- readable entity/topic pages;
- navigable relationships;
- reducing repeated query-time synthesis work.

### Raw / RAG layer

Best at:

- finding details not yet compiled into Wiki;
- recovering omitted context;
- validating an answer;
- answering one-off long-tail questions;
- rebuilding derived knowledge.

The intended retrieval stack is:

```text
Query Planner
    ├── Structured DB
    ├── Compounding Wiki
    ├── FTS
    └── Vector / raw retrieval
             ↓
      Claim / Evidence verification
             ↓
           Answer
```

---

## 4. What We Reuse from the Previous LLM Wiki Design

The previous LLM Wiki implementation provides several proven engineering ideas that should be retained conceptually.

### 4.1 Two-stage integration

Do not give every incoming source the entire Wiki.

Use two stages:

```text
Stage 1 — Route / Select
cheap model or deterministic retrieval
↓
Which existing pages may be relevant?
Should this source create any durable Wiki knowledge?

Stage 2 — Integrate
stronger model
↓
Read the source evidence + selected pages
Create/update only the necessary pages
```

This is the Wiki equivalent of the adaptive processing pipeline.

---

### 4.2 Context pruning before strong-model integration

As the Wiki grows, passing the full page catalog into every prompt will become expensive and reduce model focus.

V1 should use progressive context narrowing:

```text
Level 1 — Page-family / topic narrowing
Level 2 — Candidate page + alias narrowing
Level 3 — Compact page metadata before loading full bodies
```

For Douyin Knowledge this can use more signals than the previous implementation:

- extracted EntityMentions;
- resolved Entity IDs;
- Topics;
- source domains/forms;
- FTS hits;
- embedding candidates;
- existing Wiki links.

Only the strongest candidate pages should have full bodies loaded for the integration model.

---

### 4.3 Deterministic indexes and write guards

LLMs should write semantic content, not infrastructure.

The following should be code-managed:

- page IDs;
- page path/slug normalization;
- index generation;
- created/updated timestamps;
- source/claim support references;
- duplicate ID checks;
- link validity checks;
- schema validation;
- atomic writes / version creation.

A model must never be trusted to invent internal IDs or silently overwrite a page it was not given.

---

### 4.4 Lint + repair + ledger quality loop

Wiki quality must be continuously testable.

The system should maintain:

```text
lint finding
↓
repair attempt
↓
repair log / ledger
↓
recurrence statistics
↓
prompt or deterministic logic improvement
```

This converts repeated generation mistakes into system-level improvements rather than repeatedly fixing individual pages.

---

### 4.5 Query-time information sufficiency and early stopping

The conversation agent should not loop indefinitely through Wiki pages.

It should ask:

```text
Do I have enough grounded information to answer?
```

Possible checks:

- relevant page/entity found;
- requested constraints covered;
- important claims have provenance;
- contradictory evidence has been surfaced;
- answer scope is clear.

If additional Wiki reads repeatedly fail to improve evidence, stop and either fall back to structured/raw retrieval or report insufficient information.

---

### 4.6 Evaluation must be first-class

The previous system compared LLM Wiki against RAG using categorized query sets rather than relying on intuition alone.

Douyin Knowledge should preserve this discipline.

Before major retrieval architecture changes, maintain an evaluation set containing at least:

```text
exact find queries
semantic recall queries
cross-source synthesis queries
entity/attribute filters
source provenance questions
negative/no-result queries
metadata-only/excluded-content queries
follow-up conversation queries
```

Track both quality and latency/cost.

---

## 5. What We Intentionally Do Not Reuse Directly

Several choices from a creator-specific Wiki should not become architectural assumptions here.

### 5.1 Wiki is not the canonical database

The old architecture could treat Markdown Wiki pages as the primary compiled knowledge surface.

Douyin Knowledge has stronger structured requirements, so canonical durable state remains in SQLite.

Wiki pages are **derived compiled views**.

They must be rebuildable from:

```text
Source + Evidence + Claims + Entities + UserState
```

---

### 5.2 Do not make filesystem directories the ontology

A creator knowledge base can use per-domain folders such as composers/works/topics.

A personal collection crosses many domains and changes over time.

V1 should keep a small stable set of Wiki page families and allow Topics/links to express dynamic organization.

Avoid continuously auto-creating new top-level directories.

---

### 5.3 Do not reproduce company-specific multi-user/KV/HDFS architecture

V1 is local-first and single-user.

No HDFS, remote KV mirror, multi-blogger isolation, or C++ online service is needed.

The useful ideas are conceptual — incremental state, compiled pages, fast reads, and maintenance — not the original infrastructure.

---

### 5.4 Do not use fixed maintenance counts as hard product rules

Thresholds such as every 15 or 30 sources are implementation heuristics, not architectural truths.

Douyin Knowledge should support maintenance triggers such as:

```text
dirty-page count
new-source count
open lint findings
idle-time maintenance
manual maintain command
schema/model version change
```

Exact thresholds should be benchmarked later.

---

### 5.5 Failed ingestion must distinguish retryable from terminal

A failed source should not simply be marked permanently processed.

The job system should distinguish:

```text
retryable_failure
terminal_failure
policy_excluded
success
```

and retain enough error context to re-run intentionally.

---

## 6. Wiki Page Families V1

Use a deliberately small set of stable page families.

```text
wiki/
├── entities/
├── concepts/
├── topics/
├── syntheses/
└── source-digests/
```

These are logical page families. Physical storage may be Markdown export, database rows, or both.

### 6.1 Entity page

Represents one canonical reusable thing.

Examples:

- restaurant;
- place;
- software tool;
- product;
- person/organization;
- book/course/resource.

Entity pages summarize cross-source knowledge about the entity but do not overwrite conflicting source claims.

---

### 6.2 Concept page

Represents a reusable idea rather than a real-world identity.

Examples:

```text
AI Agent
MCP
spaced repetition
career optionality
```

Concept pages are especially valuable for learning/opinion content.

---

### 6.3 Topic page

Represents a broader user-relevant cluster.

Examples:

```text
Hong Kong cheap eats
Japan travel
AI learning
internship preparation
```

Topic pages can link entities, concepts, source digests, and synthesis pages.

---

### 6.4 Synthesis page

Represents a deliberately compiled cross-source answer or recurring synthesis.

Examples:

```text
Common arguments across my saved AI Agent videos
Hong Kong date ideas from my saved places
Recurring advice in my career-planning saves
```

Synthesis pages should be updated when materially relevant new knowledge arrives, not regenerated for every source.

---

### 6.5 Source digest

One compact interpreted page per processed source.

The Source digest is similar to `KnowledgeItem`, optimized for human navigation and Wiki linking.

Suggested content:

```text
summary
key claims
mentioned entities
important topics
important evidence references
links to affected Wiki pages
source provenance
```

It does not replace the original source/evidence records.

---

## 7. Wiki Page Data Model

A Wiki page should have a stable machine identity independent of filename/title.

Conceptual structure:

```yaml
wiki_page_id: wiki_...
page_family: entity | concept | topic | synthesis | source_digest
title: AI Agent
aliases:
  - autonomous agent
summary: ...
body_markdown: ...
linked_entity_ids: []
linked_topic_ids: []
supporting_claim_ids: []
supporting_source_ids: []
processing_run_id: ...
created_at: ...
updated_at: ...
revision: 7
status: active
```

### Support rule

Material factual statements in Wiki pages should, when feasible, trace to:

```text
Wiki statement
→ Claim
→ EvidenceUnit
→ Source
```

The page may contain high-level synthesis that is not reducible to one claim, but the supporting claim/source set must still be retained.

---

## 8. Integration Pipeline

The Wiki integration stage runs **after** structured extraction/entity resolution.

```text
Processed Source
↓
KnowledgeItem + Claims + Entities + Topics
↓
Wiki Route / Select
↓
Candidate pages
↓
Wiki Integrate
↓
Validate proposed mutations
↓
Commit page revisions
↓
Rebuild deterministic indexes
↓
Wiki lint
```

This ordering is intentional.

The Wiki model should receive structured knowledge and targeted evidence rather than being asked to rediscover everything directly from raw video.

---

## 9. Stage 1 — Route / Select

Goal: cheaply decide what existing Wiki context is relevant.

Inputs:

```text
source metadata
KnowledgeItem
Entity IDs / mentions
Topics
domains / forms
key Claims
existing page catalog metadata
```

Outputs:

```yaml
skip_wiki_integration: false
candidate_pages:
  - wiki_entity_x
  - wiki_topic_y
potential_new_pages:
  - family: concept
    title_hint: MCP
integration_priority: normal
```

### Candidate generation order

Prefer deterministic/high-signal candidates first:

1. exact linked canonical Entity pages;
2. exact Topic/Concept IDs or aliases;
3. FTS over Wiki page names/aliases/descriptions;
4. semantic candidate retrieval;
5. model-assisted routing.

The model should not receive the full Wiki catalog if code can narrow it first.

---

## 10. Stage 2 — Integrate

The integration model receives:

```text
new structured knowledge
selected evidence when needed
selected Wiki page bodies
page schemas
active quality constraints
```

It produces **mutation proposals**, not direct filesystem writes.

Conceptual operations:

```text
create_page
update_page
add_link
remove_stale_link
mark_conflict
create_synthesis_candidate
no_change
```

A mutation must specify which new Claims/Sources justify the change.

### Unseen-page protection

The model may modify only pages included in its integration context, except for explicitly proposed new pages.

This is a hard code-level guard.

---

## 11. Wiki Context Pruning

Use a three-stage strategy adapted from the earlier implementation.

### L1 — Page-family / domain narrowing

Expand only page families relevant to the source's extracted domains/entities/topics.

Other families contribute only compact counts/descriptions if needed.

### L2 — Alias and candidate narrowing

Use names, aliases, EntityMentions, Topic terms, FTS, and embeddings to keep only likely page candidates.

### L3 — Metadata first, body second

Initially provide compact records:

```text
page_id | title | aliases | one-line summary | updated_at
```

Load full page bodies only for the final candidate set.

This should substantially reduce integration-token growth as the Wiki scales.

---

## 12. Deterministic Wiki Map and Indexes

The Wiki should expose a navigable map, but index infrastructure is generated by code.

Possible logical hierarchy:

```text
L0 — Wiki map / page-family overview
L1 — page-family index / compact page metadata
L2 — full Wiki page
L3 — linked Claims / Source digests
L4 — Evidence / raw source
```

This gives the Query Planner a cheap-to-expensive navigation path.

### Index rule

The LLM does not own indexes.

Indexes are rebuilt from page metadata and relationships.

---

## 13. Wiki Retrieval Strategy

Wiki retrieval becomes one tool available to the existing Query Planner.

### Best Wiki questions

```text
我最近收藏的 Agent 内容总体在讲什么？
我收藏里关于职业选择有哪些反复出现的观点？
这几个香港徒步路线之间有什么关系？
```

### Poor Wiki-only questions

```text
旺角、人均 < 100、没去过的日料有哪些？
视频 00:43 具体说了什么？
```

Those should primarily use structured DB / Evidence.

### Retrieval order

A typical conceptual/synthesis query may use:

```text
Wiki map
↓
Wiki candidate search
↓
read 1–N pages
↓
information sufficiency check
↓
Claim/Evidence verification
↓
answer
```

If the Wiki is insufficient, fall back to FTS/vector/raw evidence rather than forcing more Wiki loops.

---

## 14. Wiki Lint V1

The earlier Wiki's large lint suite should be adapted to our data model.

Recommended V1 lint categories:

### Structural

- missing required page fields;
- invalid page family;
- duplicate stable IDs;
- invalid aliases;
- invalid links;
- orphan pages;
- index/page mismatch;
- page title/slug normalization issues.

### Provenance

- factual-looking content with no supporting Claim/Source;
- supporting Claim no longer active/current;
- broken Claim → Evidence reference;
- source digest with missing source;
- model inference presented as source statement.

### Consistency

- duplicate pages likely representing same entity/concept;
- conflicting summaries not acknowledging known Claim conflicts;
- stale page after linked entity/claim changes;
- Wiki page points to merged/deprecated Entity.

### Quality

- page too empty to justify existence;
- excessive source-by-source repetition instead of synthesis;
- noisy text copied from source;
- synthesis page dominated by one source when multiple are expected;
- unresolved integration proposal backlog.

---

## 15. Auto-Fix vs LLM Repair

Apply a strict principle:

> Code fixes syntax and invariants; LLM fixes semantics.

### Code-level auto-fix examples

- normalize slugs/Unicode/whitespace;
- repair known ID migrations;
- rebuild indexes;
- remove references to deleted revisions;
- update timestamps;
- deduplicate exact aliases;
- regenerate source links from IDs.

### LLM repair examples

- merge near-duplicate concept pages;
- rewrite unsupported synthesis;
- reconcile conflicting summaries;
- add missing conceptual connections;
- repair incomplete semantic sections.

All repairs should create auditable revisions.

---

## 16. Wiki Quality Ledger

Maintain a persistent ledger of recurring Wiki failures.

Conceptual schema:

```yaml
finding_type: unsupported_statement
page_id: wiki_...
sample_context: ...
first_seen_at: ...
last_seen_at: ...
occurrence_count: 4
repair_attempts: 2
status: open
```

Repairs append events rather than overwriting history.

### Prompt governance loop

```text
Lint detects recurring issue
↓
Ledger shows pattern
↓
Convert pattern into deterministic guard or prompt constraint
↓
Future integrations include active constraint
↓
Lint verifies recurrence decreases
↓
Constraint may later be retired if obsolete
```

This is one of the most valuable long-term engineering patterns from the previous system.

---

## 17. Maintenance Triggers

Wiki maintenance should be event/counter based rather than tied to one magic number.

Potential triggers:

```text
new integrated sources >= threshold
dirty pages >= threshold
open lint findings >= threshold
entity merge/deprecation event
processing schema version change
prompt/model version change
idle maintenance window
manual maintain command
```

Maintenance operations may include:

- lint scan;
- deterministic repairs;
- stale-page rebuild;
- candidate page merges;
- topic/synthesis refresh;
- orphan-page review;
- Wiki overview refresh;
- quality-ledger analysis.

---

## 18. Revisions and Rebuildability

Every semantic Wiki update should create a revision boundary.

At minimum record:

```text
wiki_page_id
revision
previous_revision
processing_run_id
mutation_type
supporting_claim_ids
created_at
```

The entire Wiki must be rebuildable from canonical structured data.

This gives us two important capabilities:

1. recover from a bad integration prompt/model;
2. rebuild using a better future Wiki compiler.

---

## 19. Interaction with Processing Policy

`metadata_only` / excluded sources do not enter normal Wiki integration.

```text
metadata_only source
→ no KnowledgeItem / Claims / Wiki mutation
```

If the user explicitly one-time processes an excluded source, the resulting structured knowledge can participate in Wiki integration without changing the broader exclusion rule.

---

## 20. Interaction with UserState

Wiki pages may summarize personal experience, but must distinguish it from creator/source information.

Example entity page:

```text
What saved sources say
...

My experience
Visited once; personal rating 4/5; note ...
```

The underlying data remains separate:

```text
Source Claim != User Observation
```

---

## 21. V1 Scope

V1 Wiki should support:

```text
source digests
entity pages
concept/topic pages
basic synthesis pages
two-stage route + integrate
deterministic page/index validation
supporting Claim/Source references
basic lint
revision history
manual rebuild/maintain command
```

V1 should **not** require:

- a graph database;
- autonomous creation of arbitrary top-level taxonomies;
- cloud KV storage;
- multi-user Wiki isolation;
- complex learned page-ranking models;
- fully autonomous semantic repairs without audit logs.

---

## 22. Confirmed Wiki Decisions

### WIKI-001 — Adopt the compiled-knowledge pattern

Repeatedly useful knowledge should be integrated ahead of query time rather than reconstructed only from raw chunks on every query.

### WIKI-002 — Wiki is derived state, not source of truth

Canonical truth/provenance remains Source + Evidence + Claims + Entities + UserState.

### WIKI-003 — Keep hybrid retrieval

Structured DB, Wiki, FTS, vector search, and raw evidence are complementary retrieval surfaces.

### WIKI-004 — Integrate after structured extraction

Wiki receives normalized entities/claims/topics rather than rediscovering the entire source ontology itself.

### WIKI-005 — Use two-stage Wiki ingestion

Cheap route/select first; stronger integration model second.

### WIKI-006 — Prune Wiki context aggressively

Use deterministic signals and search before loading full existing page bodies.

### WIKI-007 — Infrastructure is code-owned

IDs, indexes, schemas, references, write guards, and deterministic repairs are not delegated to the LLM.

### WIKI-008 — Quality is a continuous loop

Lint, repair, ledger, and prompt/logic governance are first-class architecture components.

### WIKI-009 — Every Wiki mutation must remain auditable and rebuildable

Bad model generations must be recoverable without corrupting canonical data.

### WIKI-010 — Evaluation accompanies architecture changes

Wiki and retrieval improvements should be benchmarked on representative personal-knowledge queries, including negative queries and provenance quality.

---

## 23. Open Questions

These should be resolved before final physical schema/task planning where they materially affect implementation.

- Should Wiki pages live canonically as database rows with Markdown export, or canonical Markdown files indexed by SQLite? Current direction favors database rows + export.
- How granular should statement-level support mapping be inside a Wiki page: section-level, paragraph-level, or sentence-level?
- Should synthesis pages be created automatically only after repeated demand/topic density, or proactively during maintenance?
- What exact page families should be enabled in the earliest MVP?
- How should Wiki revisions be diffed and surfaced in UI?
- Which lint findings can safely auto-fix without semantic review?
- When should a concept become its own page instead of remaining a Topic/tag?
- How should Wiki page embeddings interact with KnowledgeItem/Entity embeddings?
- What evaluation metrics should determine whether Wiki retrieval is preferred over raw semantic retrieval for a query class?

---

## Implementation note (2026-09-18)

The current slice compiles entity pages deterministically from current source-backed claims. Every revision has `WikiSupport` links and can be rebuilt. `dk wiki-lint` checks revision/support integrity. The proposed concept/topic/synthesis integration and quality ledger are not yet implemented; see [`IMPLEMENTATION.md`](IMPLEMENTATION.md).
