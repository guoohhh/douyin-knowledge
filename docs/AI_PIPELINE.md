# Douyin Knowledge — AI Processing Pipeline

Status: Living document  
Phase: Product / AI pipeline design  
Version: 0.1  
Last updated: 2026-09-17

---

## 1. Purpose

This document defines how one captured source moves from raw Douyin collection metadata to searchable, traceable knowledge.

The pipeline must satisfy five goals at the same time:

1. preserve trustworthy source evidence;
2. produce useful structured knowledge;
3. avoid treating AI interpretation as objective truth;
4. scale to thousands of historical saves without blindly using expensive multimodal models;
5. support later reprocessing and on-demand enrichment.

The central design principle is:

> Process adaptively. Start cheap, measure information sufficiency, and escalate only when deeper understanding is likely to add value.

---

## 2. End-to-End Pipeline

```text
COLLECTION SYNC
    ↓
SOURCE INGESTION
    ↓
CHEAP TRIAGE
    ↓
PROCESSING PLAN
    ↓
EVIDENCE ACQUISITION
    ├── caption / metadata
    ├── native subtitle if available
    ├── ASR when needed
    ├── OCR when needed
    ├── keyframes when needed
    └── visual understanding when needed
    ↓
CONTENT CLASSIFICATION
    ↓
PROCESSING PROFILE SELECTION
    ↓
STRUCTURED EXTRACTION
    ├── KnowledgeItem
    ├── EntityMention
    ├── Claim
    └── Topic
    ↓
ENTITY RESOLUTION
    ↓
INDEXING
    ├── structured DB
    ├── full-text search
    └── vector embeddings
    ↓
QUALITY CHECK
    ↓
READY FOR RETRIEVAL
```

The pipeline should be resumable. Failure in one expensive stage must not destroy already acquired source data.

---

## 3. Stage 0 — Collection Sync

Collection sync discovers new or changed sources.

For V1, Douyin is the capture channel. The sync layer should ideally capture lightweight metadata first without requiring full media download.

Typical fields:

```text
platform
external_id
source_url
creator_name
title
caption
cover_url
published_at
saved_at (when available)
duration
original_collection_ids
availability
```

### Responsibilities

- create a stable Source identity;
- detect whether a source already exists;
- preserve original Douyin collection membership;
- avoid reprocessing unchanged items unnecessarily;
- enqueue new or stale items for processing.

### Important separation

```text
sync_status
≠
processing_status
```

A source may already be synchronized while AI understanding is still pending.

---

## 4. Stage 1 — Cheap Triage

Cheap triage determines what information is already available and how deeply the source probably needs to be processed.

This stage should use only inexpensive signals where possible.

Possible inputs:

- title;
- caption/description;
- hashtags;
- creator metadata;
- duration;
- cover image;
- platform-provided subtitle/text if available;
- file/media type;
- original Douyin collection names;
- basic audio/video metadata.

### Cheap triage outputs

```yaml
language_hint: zh-CN
speech_likelihood: high
text_on_screen_likelihood: medium
visual_dependency: medium
content_density: high
likely_domains:
  - food
likely_forms:
  - recommendation
  - list
processing_priority: normal
```

These are routing hints, not durable facts.

---

## 5. Adaptive Processing Levels

V1 should use progressive processing levels instead of one universal pipeline.

### Level 0 — Metadata Only

Available immediately after collection sync.

Contains:

- title;
- caption;
- creator;
- source URL;
- collection membership;
- timestamps/metadata.

Use cases:

- immediate browsing after first import;
- lightweight indexing;
- queue prioritization;
- identifying content likely not worth expensive processing yet.

Level 0 does **not** count as fully processed.

---

### Level 1 — Text First

Use existing text without downloading or deeply analyzing full video when possible.

Inputs may include:

- caption;
- hashtags;
- platform subtitle;
- creator-provided text;
- light OCR from cover if useful.

Suitable for sources whose useful content is already represented in text.

Produces an initial KnowledgeItem and classification, but should mark coverage as limited if the video itself was not understood.

---

### Level 2 — Audio / Speech Understanding

Run when spoken content is likely important and sufficient transcript does not already exist.

Typical process:

```text
obtain audio or media
↓
ASR
↓
segment transcript with timestamps
↓
normalize punctuation / speaker-independent text
↓
create EvidenceUnits
```

ASR transcript segments become first-class evidence.

### ASR should run when

- the video is speech-heavy;
- native subtitles are missing or incomplete;
- caption text is clearly insufficient;
- the user explicitly requests deeper understanding of the source.

### ASR may be skipped when

- reliable native transcript already exists;
- the source is primarily visual/text slideshow content;
- metadata indicates negligible speech.

---

### Level 3 — Visual Enhancement

Run when visual information materially affects understanding.

Possible operations:

- scene-change / representative keyframe extraction;
- OCR on selected frames;
- visual object/place/product recognition;
- reading menus, addresses, prices, route maps, screenshots, slides, UI screens, or product labels.

### Do not OCR every frame

The default strategy should be selective:

```text
scene change detection
+
representative frame sampling
+
frames with high text likelihood
```

This avoids redundant OCR and unnecessary storage.

### Visual processing should be strongly considered for

- restaurant/food recommendations showing menu/prices;
- travel guides showing maps/locations;
- image slideshows;
- screen-recorded tutorials;
- products/tools where visual identity matters;
- videos with poor speech coverage but dense on-screen text.

---

### Level 4 — Deep Multimodal Interpretation

Use an expensive multimodal model only when cheaper evidence remains insufficient or when the user explicitly needs high-confidence/deep interpretation.

Examples:

- visually demonstrated tutorial where speech alone omits critical steps;
- complex route/map explanation;
- ambiguous entities requiring combined visual + text reasoning;
- user asks a question whose answer cannot be resolved from existing evidence;
- important/high-value source marked for deeper processing.

Level 4 should be exceptional for historical bulk processing, not the default.

---

## 6. Escalation Logic

Processing should be based on **information sufficiency**, not simply source type.

Conceptual logic:

```text
Start with available text
        ↓
Can we classify and extract reliably?
        │
   yes ─┴─ no
   │        ↓
   │      ASR needed?
   │        ↓
   │      transcript
   │        ↓
   │      still missing important information?
   │        ↓
   │      OCR / keyframes
   │        ↓
   │      still ambiguous / visually dependent?
   │        ↓
   └──── deep multimodal
```

The system should record why escalation occurred.

Example:

```yaml
escalation_reason:
  - insufficient_text_coverage
  - visual_price_information_detected
```

---

## 7. On-Demand Enrichment

A source does not need to be maximally processed before it becomes useful.

If a user later asks about a lightly processed source, retrieval can trigger enrichment.

Example:

```text
Source currently at Level 1
↓
User asks:
“视频里具体推荐了哪三个菜？”
↓
Current evidence cannot answer
↓
Queue/execute Level 2 or Level 3 enrichment
↓
Update current derived knowledge
↓
Answer with newly acquired evidence
```

This is especially important for large historical archives.

### Principle

> Processing depth should follow user value, not only ingestion time.

---

## 8. Historical Import Strategy

For a user with thousands of saved videos, first-time import must not attempt deep processing on everything immediately.

Recommended phases:

### Phase A — Fast inventory

Sync metadata for the entire collection.

Result:

```text
all sources visible
basic search available
processing queue created
```

### Phase B — Recent / high-value first

Prioritize:

1. recent saves;
2. sources the user opens/searches for;
3. sources with rich text that are cheap to process;
4. repeated topics indicating current interest;
5. remaining historical archive.

### Phase C — Progressive background enrichment

Process deeper over time subject to resource/cost limits.

### Query-triggered promotion

Any historical source involved in an active user question can jump ahead in the queue.

---

## 9. Evidence Acquisition

Every durable extracted claim should be traceable to one or more EvidenceUnits when feasible.

### Evidence kinds

Recommended V1 kinds:

```text
caption
native_subtitle
transcript
ocr
keyframe
visual_observation
creator_metadata
```

### Evidence normalization

Text evidence should preserve both:

```text
raw_text
normalized_text
```

where possible.

Normalization may fix punctuation, spacing, or obvious ASR formatting, but should not silently alter semantic meaning.

### Time alignment

Video/audio evidence should preserve timestamps:

```text
start_ms
end_ms
```

This enables future UI playback at the cited moment.

---

## 10. Transcript Chunking

Do not use arbitrary fixed-size character chunks as the only segmentation method.

Preferred hierarchy:

```text
ASR utterances
↓
semantic/topic boundaries
↓
small evidence segments
↓
retrieval chunks may combine adjacent evidence
```

This separates citation granularity from retrieval granularity.

### Why this matters

A retrieval chunk may contain 30–60 seconds of transcript for semantic search, while a cited claim may point only to a 6-second EvidenceUnit.

---

## 11. Keyframe Strategy

V1 should avoid storing hundreds of frames per video.

Candidate selection can combine:

- scene changes;
- fixed interval backup sampling;
- OCR/text likelihood;
- sharpness/quality filtering;
- duplicate-frame suppression.

Typical output should be a small representative set, not exhaustive video decomposition.

Keyframes should retain:

```text
frame_id
source_id
timestamp_ms
local_path / object_reference
ocr_text (if any)
visual_summary (if generated)
```

Exact sampling numbers should be benchmarked during implementation rather than hard-coded in product spec.

---

## 12. Content Classification

Classification occurs after enough evidence exists to make a reasonable judgment.

The pipeline should output multiple dimensions, not one giant type enum.

### Domain

Examples:

```text
food
travel
learning
career
technology
tools
shopping
lifestyle
finance
other
```

### Form / intent

Examples:

```text
recommendation
guide
tutorial
explanation
review
opinion
comparison
list
story
reference
```

### Topics

Free/extensible themes such as:

```text
AI Agent
MCP
Hong Kong hiking
cheap eats
internship
```

Classification confidence should be stored as derived metadata.

---

## 13. Processing Profiles

After classification, a routing layer chooses a specialized extraction profile.

Examples:

```text
food + recommendation/list
→ restaurant_guide

travel + guide
→ travel_guide

tools + review
→ tool_review

learning + tutorial
→ learning_tutorial

technology + explanation
→ concept_explanation

opinion
→ argument_extraction
```

A processing profile defines which structured fields are useful and which claims/entities to look for.

### Important rule

Processing profiles improve extraction quality, but the underlying knowledge model remains generic:

```text
Source
Evidence
EntityMention
Claim
Entity
KnowledgeItem
```

---

## 14. Structured Extraction

The extraction stage should produce structured outputs instead of only prose summaries.

Recommended outputs:

```text
KnowledgeItem summary
Domains / forms / topics
EntityMentions
Claims
Claim → Evidence links
EntityMention → Evidence links
Actionability hints
Quality/confidence metadata
```

### Example

A restaurant guide may produce:

```yaml
entities:
  - mention: "某某餐厅"
    hint_type: restaurant
    location_hint: Mong Kok

claims:
  - subject: "某某餐厅"
    predicate: average_price
    value:
      min: 70
      max: 90
      currency: HKD
    provenance_type: explicit_source_statement
    evidence_ids:
      - ev_12

  - subject: "某某餐厅"
    predicate: recommended_dish
    value: "叉烧饭"
    provenance_type: explicit_source_statement
    evidence_ids:
      - ev_13
```

The model should be explicitly instructed not to invent missing structured values.

Unknown is preferable to fabricated certainty.

---

## 15. Claim Quality Rules

A Claim should ideally include:

```text
subject
predicate
value/object
provenance_type
evidence_ids
confidence
processing_run_id
```

### Provenance types

At minimum:

```text
explicit_source_statement
on_screen_text
visual_observation
model_inference
```

`model_inference` must never be presented to the user as though the creator explicitly said it.

### Subjective vs factual-style claims

The system should distinguish, where possible:

```text
creator_opinion
reported_attribute
instruction/recommendation
prediction/speculation
```

This distinction improves later synthesis.

---

## 16. Entity Resolution Pipeline

Entity resolution should happen after extraction, not during raw transcription/OCR.

Recommended order:

### Step 1 — Normalize mention

Normalize text without deleting the original mention.

### Step 2 — Generate candidates

Use strong identifiers first:

- exact URLs;
- addresses;
- coordinates;
- place IDs;
- product model numbers;
- normalized name + city/district;
- known aliases.

### Step 3 — Score candidates

Use lexical/semantic similarity only as supporting evidence.

### Step 4 — Resolve conservatively

High confidence:

```text
auto-link mention → canonical entity
```

Medium confidence:

```text
retain unresolved candidate relationship
```

Low confidence:

```text
create/provisionally retain separate entity
```

### Principle

False merges are more damaging than temporary duplicates.

---

## 17. KnowledgeItem Generation

A KnowledgeItem is a user-friendly and retrieval-friendly interpreted view of the source.

Possible fields:

```text
title / generated_title
summary
key_points
domains
forms
topics
linked_entities
linked_claims
processing_coverage
processing_level
```

### Processing coverage

A KnowledgeItem should expose how deeply the source has been processed.

Example:

```yaml
processing_level: 2
coverage:
  caption: true
  transcript: true
  ocr: false
  vision: false
```

This prevents the system from pretending a Level-1 summary represents full video understanding.

---

## 18. Indexing Strategy V1

The system needs three complementary indexes.

### 18.1 Structured index

Primary use:

- filters;
- entity attributes;
- source metadata;
- user state;
- claim predicates;
- dates/locations/status.

Likely implementation: relational storage such as SQLite for V1.

---

### 18.2 Full-text search

Index text such as:

- titles;
- captions;
- transcript;
- OCR;
- KnowledgeItem summary;
- normalized claim text;
- entity names/aliases.

SQLite FTS is a strong V1 candidate.

---

### 18.3 Vector embeddings

Recommended initial embedding targets:

1. KnowledgeItem semantic representation;
2. retrieval-sized transcript/evidence chunks;
3. canonical entity semantic profile.

Claims may be embedded later if benchmarks show value.

### Do not embed everything blindly

Embedding every raw object increases cost and retrieval noise.

V1 should benchmark a small number of high-value retrieval surfaces first.

---

## 19. Retrieval Chunk vs Evidence Unit

These objects should not be identical.

### EvidenceUnit

Optimized for:

```text
citation
provenance
precise source traceability
```

### RetrievalChunk

Optimized for:

```text
semantic search
context completeness
LLM retrieval
```

One RetrievalChunk may reference multiple adjacent EvidenceUnits.

Example:

```text
RetrievalChunk rc_1
├── ev_10
├── ev_11
├── ev_12
└── ev_13
```

This allows good semantic recall without sacrificing precise citations.

---

## 20. Quality Check

Before marking processing complete, the system should run lightweight validation.

Possible checks:

- extracted claims have evidence links;
- referenced EvidenceUnits exist;
- entity mention text actually appears in evidence or is clearly derived;
- structured prices/dates/locations have valid formats;
- no impossible timestamp ranges;
- summary is consistent with extracted evidence;
- processing coverage metadata is present.

Low-confidence items should be marked rather than silently treated as complete.

---

## 21. Processing Status Model

Recommended lifecycle:

```text
synced
↓
queued
↓
processing
↓
partially_processed
↓
ready
```

Error states:

```text
source_unavailable
acquisition_failed
asr_failed
vision_failed
extraction_failed
indexing_failed
```

Failures should be stage-specific so retry does not restart the whole pipeline.

---

## 22. Idempotency and Reprocessing

Every stage should be safe to retry.

Derived outputs should be tied to:

```text
processing_run_id
processor_version
schema_version
model identifiers
prompt/extractor version
```

A new processing run should not overwrite raw source evidence destructively.

If only one stage changes — for example, a better entity resolver — the system should ideally avoid re-running ASR unnecessarily.

---

## 23. Cost and Resource Control

The system should treat compute/API usage as a budgeted resource.

### Controls may include

- maximum concurrent jobs;
- daily API budget;
- historical processing depth;
- local vs remote models;
- model routing by difficulty;
- caching;
- content hashes to avoid duplicate work;
- on-demand promotion of relevant items.

### Model routing principle

Use the cheapest model/process that can reliably perform the task.

Examples:

```text
simple language/topic classification
→ cheap model

structured extraction from clean transcript
→ standard text model

ambiguous multi-entity extraction
→ stronger text model

visual-heavy source
→ multimodal model
```

---

## 24. Recommended V1 Media Retention Policy

This is a recommended product decision for V1, subject to implementation benchmarking.

### Keep permanently by default

- Source metadata;
- caption/native text;
- timestamped transcript;
- OCR text;
- a small set of useful keyframes;
- derived EvidenceUnits;
- KnowledgeItems, Claims, Entities, Topics;
- processing/version metadata.

### Treat as temporary cache by default

- full downloaded video;
- extracted audio;
- intermediate frame dumps;
- model temporary files.

After successful durable evidence extraction, temporary media can be deleted automatically unless the user explicitly chooses archival retention.

### Why

The goal is a personal knowledge system, not an ever-growing local video mirror.

This policy also keeps multi-thousand-item archives practical on a laptop.

---

## 25. Priority Model

Processing priority should be dynamic.

Example score inputs:

```text
recency
user opened source
source matched an active query
repeated topic cluster
cheap-to-process bonus
unfinished actionable state
manual pin / high importance
```

The queue should allow an item involved in the current conversation to preempt background historical processing.

---

## 26. Example End-to-End

User saves:

```text
《香港大学附近5家值得吃的店》
```

### 1 — Sync

Create Source with metadata and original Douyin collection membership.

### 2 — Cheap triage

Caption indicates food/list/recommendation. Speech likelihood high.

### 3 — Level 2 chosen

No reliable native transcript → run ASR.

### 4 — Evidence

Create timestamped transcript EvidenceUnits.

### 5 — Visual escalation

Transcript says “价格都打在屏幕上了” and does not speak the prices → selected keyframes + OCR.

### 6 — Classification

```text
domains: food, travel
forms: recommendation, list
processing_profile: restaurant_guide
```

### 7 — Extraction

Find five EntityMentions and associated claims.

### 8 — Entity resolution

Resolve two restaurants against existing canonical entities already mentioned in earlier saved videos; create provisional new entities for the other three.

### 9 — Index

Update relational, FTS, and vector indexes.

### 10 — Ready

The source is now available for precise search and AI Q&A with timestamp citations.

---

## 27. Confirmed Pipeline Decisions

### PIPE-001 — Processing is adaptive rather than all-or-nothing

Not every source receives ASR + OCR + vision by default.

### PIPE-002 — Metadata sync is independent from AI processing

Large collections become visible before deep processing completes.

### PIPE-003 — Evidence acquisition precedes durable knowledge extraction

Claims and interpretations must reference source evidence when feasible.

### PIPE-004 — Transcript/OCR/keyframes are evidence, not merely prompt material

They remain reusable for reprocessing and citations.

### PIPE-005 — Historical archives are processed progressively

Recent, queried, high-value, or cheap items receive higher priority.

### PIPE-006 — User queries may trigger on-demand enrichment

A lightly processed source can be promoted when deeper understanding becomes useful.

### PIPE-007 — Retrieval chunks and citation evidence are separate concepts

Semantic retrieval can use larger context while citations remain precise.

### PIPE-008 — Entity resolution is conservative

False merges are considered worse than temporary duplicates.

### PIPE-009 — Processing completeness is explicit

The system records which modalities were actually analyzed.

### PIPE-010 — Full video/audio are temporary cache by default in V1

Durable knowledge evidence is kept; large media is deleted after successful processing unless archival retention is enabled.

### PIPE-011 — V1 uses structured + full-text + vector indexing

No single retrieval index is sufficient.

### PIPE-012 — Expensive multimodal processing is an escalation path, not the default

Cost follows information need and user value.

---

## 28. Open Questions

### Concrete model/runtime choices

- Which ASR implementation should be preferred locally vs via API?
- Which OCR implementation should be default?
- Which multimodal model(s) provide the best quality/cost tradeoff?
- Should the system support multiple providers from day one or abstract providers behind a common interface?

### Acquisition

- Should processing stream media directly where possible, or always create a temporary local file first?
- Which Douyin acquisition project/API should V1 integrate with first?

### Benchmarking

- What keyframe sampling strategy gives the best quality/cost tradeoff?
- How do we measure information sufficiency for escalation automatically?
- What processing depth is acceptable for initial historical import?

### Local-first deployment

- Which stages should run entirely locally by default?
- Should remote API usage be opt-in, default, or configurable by stage?
- How should model/API credentials be stored locally?

### Retrieval

- Which embedding model should V1 use?
- Which vector store is simplest alongside SQLite?
- Do entity profiles need embeddings immediately, or can V1 begin with KnowledgeItem + RetrievalChunk embeddings?

---

## 29. Next Design Step

With the knowledge model and processing pipeline defined, the next major design area is the **Retrieval & AI Conversation Architecture**:

```text
User asks a question
↓
Intent parsing
↓
Query planning
↓
Structured / FTS / vector retrieval
↓
Evidence gathering
↓
Cross-source synthesis
↓
Citation generation
↓
Answer / action
```

That document should define how the AI decides what to search, how to combine retrieval modes, how citations work, and how the system avoids answering from unsupported memory or model priors when the user explicitly asks about their saved collection.
