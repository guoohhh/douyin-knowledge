# Douyin Knowledge — Knowledge Model & Data Schema

Status: Living document  
Phase: Product / conceptual data design  
Version: 0.1  
Last updated: 2026-09-17

---

## 1. Purpose

This document defines the conceptual knowledge model for Douyin Knowledge.

The goal is to avoid treating an AI summary as the knowledge itself. The system must preserve the difference between:

- what was captured from the original source;
- what evidence exists inside that source;
- what the source or creator claims;
- what entities the source refers to;
- what the AI inferred;
- what the user personally experienced later.

This separation is required for trustworthy citations, reprocessing, deduplication, cross-source synthesis, and future expansion beyond Douyin.

---

## 2. Core Model

The proposed conceptual chain is:

```text
Capture Channel
    ↓
SOURCE
    ↓
EVIDENCE UNIT
    ↓
PROCESSING RUN / INTERPRETATION
    ↓
┌────────────────────┬─────────────────────┐
│                    │                     │
ENTITY MENTION      CLAIM                TOPIC
│                    │                     │
└──────────┬─────────┴──────────┬──────────┘
           ↓                    ↓
     CANONICAL ENTITY      KNOWLEDGE ITEM
           ↓                    ↓
           └──────────┬─────────┘
                      ↓
                  USER STATE
```

Not every object has to become a separate physical database table in V1. This document defines conceptual boundaries first; physical implementation can be simplified later.

---

## 3. Source

A `Source` is one externally captured item.

Examples:

- one Douyin video;
- one Xiaohongshu note in a future version;
- one YouTube video;
- one article;
- one screenshot.

A source represents **where information came from**, not what the system believes to be true.

### Example

```yaml
source_id: src_123
platform: douyin
external_id: "734..."
source_type: video
creator_name: some_creator
title: 港大附近5家值得吃的店
caption: ...
source_url: ...
published_at: ...
saved_at: ...
availability: available
```

### Rules

1. Source identity should be stable.
2. Source data should not be overwritten by AI interpretation.
3. Platform-specific fields may exist, but the core model must remain platform-neutral.
4. A source can belong to multiple original platform collections.
5. Original Douyin collection/folder names should be preserved as capture metadata, but should **not** become the canonical knowledge taxonomy.

---

## 4. Evidence Unit

An `EvidenceUnit` is the smallest source-derived object that can support a citation.

Examples:

- a transcript segment from 00:12 to 00:19;
- OCR text visible in a keyframe;
- the original caption;
- a keyframe containing a restaurant address;
- creator metadata;
- a visual observation extracted from a frame.

### Example

```yaml
evidence_id: ev_456
source_id: src_123
kind: transcript
start_ms: 12000
end_ms: 19000
text: "这家店人均七八十，叉烧饭我最推荐"
```

### Why Evidence is required

Without an evidence layer, the system can only say:

> "AI extracted this fact."

With an evidence layer, it can say:

> "This claim came from this source, at this timestamp, from this transcript/frame."

This is the foundation for trustworthy AI answers.

---

## 5. Processing Run

A `ProcessingRun` records one versioned execution of the AI understanding pipeline.

It identifies which models, prompts, schemas, and processor versions produced derived information.

### Example

```yaml
processing_run_id: run_20260917_001
source_id: src_123
processor_version: "0.1.0"
schema_version: "knowledge-v1"
models:
  asr: ...
  vision: ...
  llm: ...
processed_at: ...
status: completed
```

### Rules

- Reprocessing should create a new run rather than destroying source evidence.
- Derived outputs must be attributable to a processing run.
- The system can mark one run as current while retaining older runs for debugging or migration.

---

## 6. Claim

A `Claim` is an atomic assertion extracted from a source.

A claim is **not automatically an objective fact**.

This is one of the most important distinctions in the entire system.

If a creator says:

> "这家店人均 80，而且是旺角最好吃的日料。"

we should not store:

```text
Restaurant.price = 80
Restaurant.quality = best
```

as unquestioned truth.

Instead, we store source-attributed claims.

### Example 1 — structured factual-style claim

```yaml
claim_id: cl_001
subject_entity_id: ent_restaurant_01
predicate: average_price
value:
  min: 70
  max: 90
  currency: HKD
attribution: creator
evidence_ids:
  - ev_456
confidence: 0.93
```

### Example 2 — subjective recommendation

```yaml
claim_id: cl_002
subject_entity_id: ent_restaurant_01
predicate: recommendation
value: "strongly_recommended"
attribution: creator
evidence_ids:
  - ev_457
```

### Claim provenance categories

At minimum, claims should distinguish:

```text
explicit_source_statement
on_screen_text
visual_observation
model_inference
```

User-authored experience should normally live in `UserState` / `UserObservation`, not be mixed with creator claims.

### Why Claim exists

Claims make it possible to:

- keep different creators' opinions separate;
- compare conflicting information;
- attach precise citations;
- represent uncertainty;
- update time-sensitive information;
- avoid turning AI extraction into false certainty.

---

## 7. Entity Mention

An `EntityMention` represents a thing mentioned in one source before we know whether it is identical to an already-known canonical entity.

Example:

```yaml
mention_id: mention_001
source_id: src_123
text: "肥姐小食店"
entity_type_hint: restaurant
location_hint: Mong Kok
```

This intermediate layer is necessary because names can be ambiguous.

Two videos saying:

```text
"肥姐"
"肥姐小食店"
```

may refer to the same business — but the system should resolve that explicitly rather than silently merging strings.

---

## 8. Canonical Entity

An `Entity` is a canonical real-world or reusable conceptual object that may be referenced by multiple sources.

Examples:

- a restaurant;
- a tourist attraction;
- a city or district;
- a software tool;
- a product;
- a person or organization;
- a book/course/resource;
- potentially a dish or route where cross-source identity is useful.

### Example

```yaml
entity_id: ent_restaurant_01
entity_type: place
subtype: restaurant
canonical_name: "肥姐小食店"
aliases:
  - "肥姐"
location:
  city: Hong Kong
  district: Mong Kok
```

### Important rule

An entity should represent identity, not a single source's opinion about that identity.

Therefore:

```text
Entity
= "what thing is this?"

Claim
= "what did this source say about it?"
```

This distinction enables many sources to describe the same entity without overwriting each other.

---

## 9. Entity Resolution and Deduplication

Entity deduplication should be conservative.

### Strong matching signals

Examples:

- exact official URL/domain;
- exact address;
- geographic coordinates;
- platform place ID;
- brand + model identifier;
- normalized name + city/district;
- known aliases.

### Weak matching signals

Examples:

- fuzzy name similarity;
- similar description;
- AI semantic similarity.

Weak signals can propose a merge but should not automatically collapse entities unless confidence is high and contexts are compatible.

### Desired model

```text
Source A → Mention A ─┐
                     ├→ Canonical Entity X
Source B → Mention B ─┘
```

The original mentions should remain available even after resolution.

---

## 10. Topic

A `Topic` is a semantic theme used for clustering, retrieval, and resurfacing.

Examples:

```text
AI Agent
MCP
career planning
Hong Kong hiking
cheap eats
productivity
```

A Topic is different from an Entity.

- `Hong Kong Disneyland` is an entity.
- `Hong Kong travel` is a topic.
- `MCP` may be treated as a topic or concept depending on future ontology needs.

V1 should keep topic modeling lightweight and avoid building a formal ontology prematurely.

---

## 11. Knowledge Item

A `KnowledgeItem` is the system's derived, source-level interpretation used for UI and retrieval.

It may contain:

- source summary;
- classification;
- detected domains/forms;
- linked entities;
- linked claims;
- linked topics;
- important evidence;
- embeddings/search text.

### Important rule

A KnowledgeItem is **not the truth database**.

It is best understood as:

> a convenient interpreted view of one source.

The durable provenance chain remains:

```text
Source → Evidence → Claim / Entity Mention
```

This makes it safe to regenerate KnowledgeItems later with better models.

---

## 12. User State and Personal Knowledge

The user's own experience must be stored separately from creator/source claims.

Examples:

### Place

```yaml
entity_id: ent_restaurant_01
state: visited
rating: 4
note: "叉烧不错，但排队太久"
visited_at: ...
```

### Tool

```yaml
entity_id: ent_tool_01
state: using
note: "写论文时挺好用"
```

### Learning content

```yaml
knowledge_item_id: ki_001
state: completed
note: "第二部分值得复习"
```

The system should support personal state on either:

- canonical entities, when the action concerns a real object;
- knowledge items, when the action concerns the saved content itself.

### Principle

```text
Source claim
≠ AI inference
≠ User experience
```

They must remain distinguishable.

---

## 13. Relationships

V1 should avoid prematurely building a full graph database.

Relationships can initially be represented through structured claims.

Example:

```text
Restaurant A --located_in--> Mong Kok
Tool B --useful_for--> transcription
Route C --passes_through--> Sai Kung
```

A claim can have another entity as its object.

Later, frequently used/high-confidence relationships may be materialized into dedicated graph-style structures without changing the conceptual model.

---

## 14. Content Classification: Do Not Use One Giant Type Enum

A single `content_type` such as:

```text
food
travel
learning
tool
opinion
```

will become brittle because content is often multi-dimensional.

Instead, V1 should classify content on at least two axes.

### Axis A — Domain

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

Domains should be extensible and multi-label where useful.

### Axis B — Content Form / Intent

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
news/reference
```

A source may have one dominant form and additional secondary forms.

### Processing Profile

The AI pipeline may derive an internal `processing_profile` from domain + form.

Examples:

```text
food + recommendation/list
→ restaurant_guide_extractor

travel + guide
→ travel_route_extractor

tools + review
→ tool_review_extractor

technology + explanation
→ concept_explanation_extractor
```

This gives us structured extraction without forcing the entire knowledge system into one rigid content taxonomy.

---

## 15. Example: One Douyin Video End-to-End

Source:

```text
《港大附近5家值得吃的店》
```

### Step 1 — Source

```text
src_123
```

### Step 2 — Evidence

```text
ev_1: caption
ev_2: transcript 00:12–00:19
ev_3: transcript 00:20–00:30
ev_4: OCR from keyframe
ev_5: visual keyframe
```

### Step 3 — Knowledge Item

```text
ki_123

domains:
- food
- hong_kong

form:
- recommendation
- list

summary:
"A roundup of five restaurants near HKU..."
```

### Step 4 — Mentions

```text
mention_restaurant_A
mention_restaurant_B
mention_restaurant_C
mention_restaurant_D
mention_restaurant_E
```

### Step 5 — Entity resolution

```text
mention_restaurant_A → ent_A
...
```

### Step 6 — Claims

```text
ent_A average_price HKD 70–90
ent_A recommended_dish "叉烧饭"
ent_A located_near "HKU"
ent_B queue_time ~20min
...
```

Every claim links to evidence.

### Step 7 — User action later

```text
ent_A state = visited
ent_A personal_rating = 4/5
ent_A personal_note = "好吃但排队太久"
```

The creator's opinion and the user's experience now coexist without overwriting each other.

---

## 16. Retrieval Implications

This model naturally supports different retrieval methods.

### Structured retrieval

Search:

```text
entity_type = restaurant
city = Hong Kong
district = Mong Kok
user_state != visited
```

### Full-text retrieval

Search across:

- captions;
- transcripts;
- OCR;
- claim raw text;
- KnowledgeItem summaries.

### Semantic retrieval

Useful embedding targets may include:

- KnowledgeItem summaries;
- Evidence/transcript chunks;
- entity profiles;
- normalized claim text.

The exact embedding strategy will be defined in the AI/Retrieval design document.

### Answer generation

For important factual statements, the answer generator should prefer:

```text
Claim + Evidence
```

over relying only on the source summary.

---

## 17. V1 Conceptual Objects

The current recommended conceptual model is:

```text
Source
SourceCollection
EvidenceUnit
ProcessingRun
KnowledgeItem
EntityMention
Entity
Claim
Topic
UserState / UserAnnotation
```

Not all of these need a dedicated table on day one, but Codex should preserve the conceptual separation in implementation.

---

## 18. Confirmed Knowledge-Model Decisions

### KM-001 — Source is not Knowledge

The captured external item and AI interpretation are separate objects.

### KM-002 — Evidence is first-class

Traceable source fragments must exist independently from summaries.

### KM-003 — Claims are source-attributed assertions, not automatically objective facts

This prevents creator opinions and stale information from being silently promoted to truth.

### KM-004 — Entity identity is separated from claims about the entity

Multiple sources can refer to the same canonical entity.

### KM-005 — Entity mentions are preserved before canonical resolution

Deduplication must be explicit and reversible.

### KM-006 — KnowledgeItem is a derived source interpretation

It is useful for UI/RAG but is regenerable and should not be the sole provenance layer.

### KM-007 — User experience is separate from source claims

Personal notes, ratings, visits, usage, or completion states must not overwrite creator/source information.

### KM-008 — Content classification uses multiple dimensions

Use domain + content form/intent rather than one rigid giant enum.

### KM-009 — Original Douyin collections are preserved but are not the canonical taxonomy

They remain useful capture metadata and user context.

### KM-010 — A graph database is not required for V1

Entity relationships can initially be represented through structured claims and relational storage.

---

## 19. Open Questions for the Next Design Stage

The conceptual model is now stable enough to continue, but these implementation questions remain:

### Entity scope

- Which objects deserve canonical entity status in V1?
- Should dishes, routes, concepts, and individual learning methods be entities or structured fields/topics?

### Claim normalization

- How large should the initial predicate vocabulary be?
- Should arbitrary predicates be allowed as strings, or must extractors map to schemas?
- How should time-sensitive claims be modeled?

### User state

- Do we implement one generic state system or lifecycle schemas per entity type?
- How should conflicting states/history be preserved?

### Search representation

- Which objects get embeddings in V1?
- How are transcript chunks split?
- Do claims have their own embeddings?

### Physical schema

- Exact SQLite/Postgres tables and indexes.
- JSON columns versus normalized relational tables.
- How vector storage is integrated.

These should be resolved together with the AI pipeline and retrieval architecture rather than independently.
