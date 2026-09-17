# Douyin Knowledge — Product Spec

Status: Living document  
Phase: Product design  
V1 anchor: Douyin collections  
Last updated: 2026-09-17

---

## 1. Vision

Douyin Knowledge turns saved short-form content into a personal external memory system.

The product starts from a concrete problem: users save food recommendations, places to visit, learning methods, tools, products, opinions, and life advice on Douyin, but most saved content is rarely revisited. The goal is not merely to export or summarize videos. The goal is to transform saved content into knowledge that can be searched, understood, compared, acted on, and resurfaced when it becomes useful again.

The first version is anchored on Douyin collections. Long term, Douyin should be treated as one capture channel among many rather than the whole product.

Potential future capture channels include Xiaohongshu, YouTube, web pages, articles, screenshots, and other saved content sources.

---

## 2. Product Promise

A user should be able to keep using Douyin exactly as they already do:

```text
See something that may be useful later
↓
Tap 收藏
↓
Continue scrolling
```

No copy/paste, manual tagging, manual folders, manual downloading, or sending links to a bot should be required for the normal capture flow.

The system should handle the rest:

```text
Capture
↓
Understand
↓
Retrieve
↓
Synthesize
↓
Resurface
```

The core promise of V1 is:

> When the user later vaguely remembers “I think I saved something about this before”, they should be able to ask naturally and recover the relevant knowledge within seconds, with traceable sources.

---

## 3. Product Positioning

Douyin Knowledge is not primarily:

- a Douyin downloader;
- a folder of AI summaries;
- a traditional bookmark manager;
- a plain vector database;
- a generic ChatPDF-style RAG application.

It should become a personal knowledge system whose main question is:

> What did the past version of me think was worth saving, and how can that help me now?

Public search and public AI answer what exists on the internet. Douyin Knowledge should answer what the user previously chose to preserve.

---

## 4. Core Product Principles

### P-001 — Zero-friction capture

The user should not need to perform a second action after tapping 收藏 on Douyin.

### P-002 — Douyin is the V1 capture channel, not the long-term product boundary

The architecture should start with Douyin but avoid coupling the knowledge model to Douyin-specific concepts.

### P-003 — Raw source and AI interpretation must be separated

Original metadata, text, transcript, OCR output, screenshots/keyframes, and other source evidence should be preserved independently from AI-generated summaries and structured interpretations.

This allows future models to reprocess old content without needing to reacquire the source.

### P-004 — AI conversation is the primary knowledge access interface

Traditional browsing, folders, tags, and lists are useful secondary interfaces. The main interaction should allow the user to ask natural-language questions about their saved knowledge.

### P-005 — Retrieval must not rely on vector search alone

The system should eventually combine:

- structured queries;
- full-text search;
- semantic/vector search;
- source-level retrieval;
- entity-level retrieval.

The AI should choose or combine retrieval modes according to the query.

### P-006 — Answers about saved content must be traceable

Claims based on the user’s collection should link back to source material wherever possible.

Traceability may include:

- source video;
- author;
- saved date;
- transcript segment;
- timestamp range;
- frame/keyframe;
- extracted entity.

### P-007 — One source may contain multiple knowledge entities

A single video is not necessarily one unit of knowledge.

Example:

```text
Source video:
“10 restaurants worth trying in Tokyo”

↓

1 source
10 place/restaurant entities
```

This distinction is critical for later search and retrieval.

### P-008 — The product must help users use saved knowledge, not merely store it

The system should support four AI interaction modes:

1. Find — locate saved content.
2. Answer — answer a question using saved content.
3. Synthesize — compare or combine multiple saved items.
4. Act — turn saved knowledge into an actionable plan or next step.

### P-009 — Resurfacing is a first-class capability

The third layer, retrieval/resurfacing, is more important than summary generation alone.

The system should help bring old saved knowledge back when it is relevant, while avoiding unnecessary interruption.

### P-010 — User feedback becomes higher-value personal knowledge

The user’s own experience after visiting, buying, learning, trying, or rejecting something should become part of the knowledge base and should be distinguishable from the original creator’s opinion.

---

## 5. User Journey V1

### 5.1 First-time connection

The first-time experience should focus on connecting the user’s Douyin collection and explaining the value of the system.

Possible onboarding concept:

```text
Welcome to Douyin Knowledge

Turn things you saved before
into knowledge you can actually use later.

[Connect my Douyin collection]
```

After connection, the system discovers existing collections and saved items.

---

### 5.2 Metadata import before expensive AI processing

Initial sync and AI understanding must be independent stages.

For a large historical collection, the system should first build a lightweight local index containing metadata such as:

```text
source_id
platform
video_id
title
description
author
source_url
cover
publish_time
saved_time (when available)
duration
source_collection
```

This lets the system become usable before every historical item has gone through expensive AI processing.

---

### 5.3 Progressive historical processing

Historical items should be processed progressively rather than blocking first use.

Likely priority order:

```text
recent saves
↓
items explicitly opened/searched by the user
↓
high-value or easy-to-process items
↓
remaining archive
```

The product should show processing state without requiring the user to wait for full completion.

---

### 5.4 Daily capture and incremental sync

Normal daily flow:

```text
User saves content on Douyin
↓
System performs incremental sync
↓
New source is detected
↓
Deduplicate
↓
Fetch metadata/source evidence
↓
Understand content
↓
Classify
↓
Extract structured knowledge
↓
Index for retrieval
```

The default behavior should be quiet. Routine processing should not generate unnecessary notifications.

---

## 6. Content Understanding Pipeline — Product-Level View

A source should not immediately be reduced to a summary.

The conceptual pipeline is:

```text
Source
  │
  ├── creator metadata
  ├── description/caption
  ├── transcript / ASR
  ├── OCR
  ├── keyframes
  └── visual understanding
  │
  ▼
Content classification
  │
  ▼
Type-specific extraction
  │
  ├── structured fields
  ├── entities
  ├── relationships
  ├── summary
  └── tags/topics
```

Important principle:

> Ask “what kind of information is this?” before asking “how should it be summarized?”

Different content types require different extraction schemas.

Examples:

Restaurant/food content may require:

- venue name;
- location;
- dishes;
- price;
- queue/wait information;
- creator opinion;
- practical tips.

Learning content may require:

- main idea;
- steps/method;
- prerequisites;
- resources;
- actionable exercises;
- caveats.

Tool content may require:

- tool name;
- URL;
- purpose;
- price;
- use cases;
- limitations.

Opinion content may require:

- thesis;
- supporting arguments;
- assumptions;
- counterpoints/uncertainty.

The exact type system is not yet finalized.

---

## 7. Knowledge Layers

At minimum, the model should distinguish three conceptual layers:

```text
SOURCE
Original captured item

↓

KNOWLEDGE
AI-understood meaning, transcript, summary, topics, extracted claims

↓

ENTITY
Concrete things mentioned inside the source
```

Example:

```text
SOURCE
Douyin video: “5 restaurants near HKU”

↓

KNOWLEDGE
Video summary, transcript, food-guide structure, creator recommendations

↓

ENTITIES
Restaurant A
Restaurant B
Restaurant C
Restaurant D
Restaurant E
```

This model should support the same entity being mentioned by multiple sources in the future.

---

## 8. AI Conversation and Retrieval

The AI conversation interface should be the primary product surface for using stored knowledge.

Example user questions:

- “我之前是不是收藏过旺角比较便宜的日料？”
- “我以前收藏过哪些关于 AI Agent 的学习方法？”
- “帮我找我收藏过的东京三日旅行攻略。”
- “综合我收藏过的这些 Agent 视频，它们有什么共同观点？”

The system should first interpret intent and constraints, then plan retrieval.

Conceptual example:

```text
User query
↓
Intent + constraint parsing
↓
Retrieval planning
↓
Structured search + full-text + semantic search
↓
Candidate sources/entities
↓
Evidence reading
↓
Answer with citations
```

For structured questions such as:

> “旺角人均 100 港币以下的日料”

structured metadata should carry most of the retrieval burden.

For conceptual questions such as:

> “关于不要过度规划人生的观点”

semantic retrieval is more appropriate.

The system may combine both.

---

## 9. Source Traceability

The system should retain enough provenance to support precise citations.

Potential source links:

```text
source_id
video_id
timestamp_start
timestamp_end
transcript_chunk
frame_id
extraction_id
```

An ideal answer can tell the user not only which video supports a statement, but which segment of the video produced the relevant evidence.

---

## 10. Single Knowledge Item View

A single processed item should eventually expose both AI output and underlying evidence.

Conceptual layout:

```text
Title

Source
- platform
- creator
- saved date
- original link

AI Summary

Structured information

Extracted entities

Keyframes

Transcript

User status / personal note
```

The user should always be able to inspect what the AI based its interpretation on.

---

## 11. Lifecycle and Personal State

A saved item should not remain permanently in a single `saved` state.

Different entity types may have different lifecycles.

Place:

```text
saved
→ want_to_go
→ visited
→ favorite / disliked
```

Learning item:

```text
saved
→ want_to_learn
→ learning
→ completed
```

Tool:

```text
saved
→ want_to_try
→ using
→ abandoned
```

Product:

```text
saved
→ considering
→ purchased
→ recommended / regretted
```

These states allow the system to distinguish passive interest from actions that actually became part of the user’s life.

---

## 12. Resurfacing

Resurfacing should not mean noisy notifications.

The system should surface old knowledge when there is a clear relevance signal.

Possible signals include:

- repeated recent saves about the same topic;
- a cluster of related saved items;
- a user query that connects to older material;
- a location or activity context in future versions;
- an unfinished actionable state.

Example:

```text
You saved 18 AI Agent-related items over the last 14 days.
Many discuss MCP, browser agents, and memory.

[Summarize what they collectively say]
```

The product should describe observed patterns rather than deciding what the user should care about.

---

## 13. Source Deletion and Durability

If the original platform source disappears, locally preserved knowledge should not disappear automatically.

The system may retain:

- metadata;
- transcript;
- OCR;
- AI output;
- keyframes;
- extracted entities;
- provenance.

The source can then be marked unavailable.

Conceptually:

```text
source_status: unavailable
```

A key value proposition is that platform content may disappear while the user’s external memory remains intact.

---

## 14. Reprocessing and Versioning

AI interpretation should be versioned.

Possible fields:

```text
processor_version
model
processed_at
schema_version
```

Raw source evidence should remain stable while derived knowledge can be regenerated using improved models or improved schemas.

The system should eventually support reprocessing without reacquiring every source.

---

## 15. V1 Scope

V1 should focus on making this path reliable:

```text
Douyin collection
↓
automatic/incremental sync
↓
AI understanding
↓
structured knowledge
↓
local persistence
↓
AI search and Q&A
↓
source traceability
```

V1 should not be blocked by building:

- Xiaohongshu integration;
- YouTube integration;
- a mobile app;
- browser extensions;
- collaboration/social features;
- a cloud account system;
- a sophisticated graph database;
- complex recommendation systems.

Future extensibility should influence architecture, but should not expand V1 implementation scope unnecessarily.

---

## 16. Product Success Criteria

V1 should not be judged primarily by the number of videos downloaded or summarized.

The strongest success test is:

> The user vaguely remembers saving something useful before, asks in natural language, and receives the relevant saved knowledge quickly with trustworthy source evidence.

A secondary success test is:

> Multiple fragmented saves can be synthesized into a useful comparison, plan, or answer that would have been tedious to assemble manually.

---

## 17. Confirmed Design Decisions

### DEC-001 — V1 is anchored on Douyin collections

Douyin is the initial capture channel, while the knowledge architecture should remain extensible.

### DEC-002 — Capture should require no additional user action after 收藏

Manual copying, tagging, and categorization should not be part of the default flow.

### DEC-003 — Raw source data and AI-derived knowledge are stored separately

Derived content must be regenerable.

### DEC-004 — AI conversation is the primary knowledge retrieval interface

Browsing UI remains useful but secondary.

### DEC-005 — Retrieval is hybrid, not pure vector RAG

Structured, full-text, and semantic retrieval should coexist.

### DEC-006 — Answers should be source-traceable

The system should retain provenance down to source segments where feasible.

### DEC-007 — One source may produce multiple knowledge entities

Video-level storage alone is insufficient.

### DEC-008 — Retrieval and resurfacing are core value, not an afterthought

Generating summaries alone does not solve the “收藏吃灰” problem.

### DEC-009 — Personal post-action feedback becomes part of the knowledge system

The user’s own experience should remain distinguishable from creator/source claims.

### DEC-010 — The project is specification-first

Product and architecture decisions should be stabilized before Codex begins full implementation.

---

## 18. Open Questions

These are intentionally unresolved and should be discussed before implementation where they materially affect architecture.

### Knowledge model

- What are the canonical top-level content types?
- Can one item have more than one primary type, or should there always be one primary type plus secondary facets?
- What is the exact distinction between Knowledge, Entity, Claim, Topic, and Source?
- How should one entity mentioned by multiple videos be merged or deduplicated?
- Should creator recommendations be modeled as claims with provenance?

### Collection semantics

- Should the user’s existing Douyin collection/folder organization be preserved?
- How much weight should original collection names have versus AI classification?

### Media retention

- Should downloaded video be deleted after processing?
- Which keyframes/audio/raw artifacts are worth keeping permanently?
- What is the local storage strategy for large historical collections?

### AI processing

- When should ASR, OCR, and visual understanding each run?
- Can cheap early classifiers avoid expensive multimodal processing for some items?
- How should processing quality/cost tiers work for thousands of historical saves?

### Retrieval

- What gets embedded: source, knowledge item, entity, transcript chunk, extracted claim, or some combination?
- Which full-text search technology should be used?
- How should retrieval results be ranked when structured and semantic matches disagree?

### Resurfacing

- What resurfacing events belong in V1 versus later phases?
- Should resurfacing happen only inside the app initially, or include notifications?

### User interface

- Is V1 best as a local web app, desktop app, CLI + web UI, or another form?
- What is the minimum browsing experience needed alongside chat?

### Douyin integration

- Which existing open-source project should be adopted as the primary Douyin ingestion layer?
- How should login/session/cookie state be handled safely and resiliently?
- How should the system react to Douyin API/anti-bot changes?

---

## 19. Next Design Stage

The next design stage is **Knowledge Model V1**.

Its purpose is to define the product’s conceptual data model before choosing implementation technologies.

Expected outputs:

1. canonical content-type system;
2. Source / Knowledge / Entity / Claim relationships;
3. common metadata fields;
4. type-specific schemas;
5. multi-entity extraction behavior;
6. entity deduplication principles;
7. provenance model;
8. lifecycle/state model;
9. what should and should not become an embedding unit.

Once this is agreed, the relevant sections of this document should be promoted into a dedicated `DATA_SCHEMA.md`.
