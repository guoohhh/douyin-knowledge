# Douyin Knowledge — Retrieval & AI Conversation Architecture

Status: Living document  
Phase: Product / retrieval design  
Version: 0.1  
Last updated: 2026-09-17

---

## 1. Purpose

This document defines how the AI conversation layer should retrieve, reason over, and answer questions using the user's saved knowledge.

The system must not behave like a generic vector-search chatbot.

Its job is to turn the user's collection into an external memory that can be:

- searched precisely;
- explored semantically;
- synthesized across many sources;
- converted into actions/plans;
- cited back to trustworthy source evidence.

Core principle:

> The AI should plan retrieval over structured data, text, semantic indexes, claims, entities, and evidence — not just retrieve the nearest embedding chunks.

---

## 2. Conversation Capabilities

The AI conversation layer supports four primary user intents.

### Find

Locate saved items/entities.

Examples:

```text
我之前收藏过哪些香港徒步路线？
我是不是存过一个讲 MCP 的视频？
找一下我收藏过的旺角日料。
```

### Answer

Answer a factual or explanatory question using saved content.

Examples:

```text
我收藏的那个西贡路线大概要走多久？
那家餐厅视频里推荐了什么菜？
```

### Synthesize

Combine multiple saved sources.

Examples:

```text
综合我收藏过的 Agent 视频，它们共同提到了哪些趋势？
我收藏的香港徒步攻略里，哪些路线最适合新手？
```

### Act

Turn saved knowledge into an actionable output.

Examples:

```text
根据我收藏的香港地点，帮我排一个周六行程。
根据我收藏的学习方法，给我做一个两周计划。
```

The router may assign more than one intent to a query.

---

## 3. Knowledge Scope

A core safety/trust requirement is that the system must distinguish **the user's saved knowledge** from **general model knowledge**.

### Scope A — Personal / collection-grounded

Examples:

```text
我之前收藏过什么？
我收藏的视频里大家怎么讲 MCP？
根据我的收藏推荐几家餐厅。
```

For this scope, factual claims about the user's collection must come from the local knowledge base.

If there is insufficient evidence, the system should say so rather than filling gaps from general knowledge.

---

### Scope B — General knowledge

Example:

```text
MCP 是什么？
```

This question does not inherently ask about the user's collection.

The AI may answer using its general knowledge, but the response should not imply that the answer came from the user's saved content.

The UI may indicate:

```text
General answer
```

rather than presenting collection citations.

---

### Scope C — Hybrid

Example:

```text
结合我收藏的 Agent 内容和你的通用知识，给我做学习路线。
```

Hybrid answers are allowed, but the two evidence domains must remain distinguishable.

Recommended presentation:

```text
From your collection
...

Additional general context
...
```

A general-model statement must never be given a fake collection citation.

---

## 4. Default Scope Behavior

The application is primarily a personal knowledge system, so collection context should be easy to invoke but not silently fabricated.

Recommended rules:

```text
Explicit personal wording
("我收藏的", "我之前存过", "我的收藏")
→ personal_required

Explicit general wording
("一般来说", "你知道的", "不看我的收藏")
→ general

Explicit combination
("结合我的收藏和你的知识")
→ hybrid
```

For ambiguous queries such as:

```text
香港有什么适合约会的地方？
```

inside Douyin Knowledge, the default can be **personal-first**:

1. search the user's collection first;
2. answer clearly as “你收藏里有……”;
3. do not silently supplement with unrelated external/general recommendations unless the user asks for broader suggestions.

This keeps the product centered on external memory instead of turning into a generic chatbot.

---

## 5. Query Understanding

Every query should first be converted into a structured `QueryPlanInput`.

Conceptual output:

```yaml
intent:
  - find
  - recommend
scope: personal_required
entity_types:
  - restaurant
constraints:
  city: Hong Kong
  district: Mong Kok
  cuisine: Japanese
  price_max: 100
user_state:
  visited: false
semantic_requirements:
  - cheap
  - suitable_for_date
sort_preferences:
  - relevance
```

The parser should identify:

- user intent;
- knowledge scope;
- entity type(s);
- exact filters;
- fuzzy semantic requirements;
- time constraints;
- location constraints;
- user-state constraints;
- references to earlier conversation turns.

Unknown values should remain unknown rather than being invented.

### 5.1 The implemented QueryPlan contract (V1)

The shape above is the target. What ships is narrower, and the difference matters because
everything downstream treats a validated plan as trusted: `retrieval/query_plan.py` is the
only place where untrusted input becomes an execution instruction.

`QueryPlan` fields: `raw_query`, `intent`, `scope`, `entity_types`, `entity_subtypes`,
`location`, `claim_constraints`, `text_requirements`, `semantic_requirements`, `user_state`,
`conversation_entity_refs`, `limit`, `parser`, `notes`.

Supported constraint fields, and the storage they map onto:

| plan field | claim predicate | column | operators |
| --- | --- | --- | --- |
| `price_per_person` | `price_per_person` | `value_number` | `<` `<=` `>` `>=` `=` |
| `cuisine` | `cuisine` | `value_text` | `=` |
| `district` | `located_in` | `value_text` | `=` |

Five rules hold the boundary:

1. **The operator set is a whitelist, not an escape.** An operator is matched against a
   frozenset and used to select a Python comparison. A SQL fragment in that position can
   only ever be an unrecognised string, which is why "no model-generated SQL" is a
   structural property rather than a promise.
2. **Every rejection is a code, not a silent default.** `QueryPlanError` carries codes like
   `unsupported_operator`, `unknown_district`, `bad_value_type`, `duplicate_constraint`.
   A malformed predicate fails; it never degrades into a fragment or a guessed value.
3. **`scope` is a parameter, never read from the proposed plan.** A model that answered
   我收藏过 with `scope: general` would license answering from world knowledge, which is
   the one failure the scope classifier exists to prevent.
4. **Two constraints on one field are rejected** rather than silently resolved, because the
   executor implements no conjunction semantics for them.
5. **`limit` is capped** (`MAX_LIMIT`), and candidate generation is bounded
   (`MAX_CANDIDATES`), so plan cost cannot grow with a model's enthusiasm.

Not supported in V1, deliberately: ranges (`50-100`), time constraints, `sort_preferences`,
relations, any predicate outside the three above, and `city` as a filter (it is accepted and
carried, never executed). Districts and cuisines come from closed alias vocabularies in
`retrieval/vocabulary.py`; a value outside them is rejected rather than passed through, so
an unmatchable constraint never looks like a satisfied one.

Parsing is deterministic first (`query_parser.py`), and a structured model is consulted only
when the deterministic pass found neither a location nor a claim constraint. Whatever the
model proposes goes through the same `validate_plan` as everything else. `parse_query` never
raises: a parser failure degrades to an unstructured plan and ordinary retrieval, leaving the
reason in diagnostics rather than in a traceback.

---

## 6. Retrieval Surfaces

V1 should expose multiple retrieval surfaces to the AI planner.

### 6.1 Structured search

Best for exact or semi-exact constraints.

Examples:

```text
city = Hong Kong
entity_type = restaurant
price_max <= 100
user_state != visited
creator_id = ...
saved_at >= ...
```

Likely backed by SQLite relational queries.

Of those examples, V1 implements `district`, `cuisine` and `price_per_person` only; see 5.1.

**Hard constraints and similarity signals are different kinds of thing.** A structured
constraint is a condition on membership: a candidate that fails it is not a worse answer, it
is not an answer. FTS and vector scores are signals about *ordering and presentation* among
candidates that already qualify. Treating them as commensurable — letting a strong similarity
score compensate for a failed constraint — produces the failure this phase exists to rule
out: a restaurant at 人均 150 returned for 人均100以下 because its text was a good match for
旺角 and 日料. It matched the words; it does not match the question.

---

### 6.2 Full-text search

Best for names, phrases, remembered wording, OCR, captions, and transcript text.

Search targets include:

- source title/caption;
- native subtitles;
- ASR transcript;
- OCR;
- KnowledgeItem text;
- claim text;
- entity names and aliases.

Likely backed by SQLite FTS for V1.

---

### 6.3 Semantic / vector search

Best for concepts whose wording may differ.

Examples:

```text
适合约会但不要太正式
不要过度规划人生
适合新手的徒步
学生党性价比
```

Recommended initial vector surfaces:

- KnowledgeItem semantic representation;
- RetrievalChunks built from transcript/evidence;
- canonical entity semantic profile.

---

### 6.4 Entity retrieval

Best when the user is referring to a real object already known to the system.

Examples:

```text
那家旺角日料
我收藏过的那几个 AI 工具
迪士尼附近的地点
```

Entity retrieval should combine:

- canonical name/aliases;
- structured attributes;
- linked claims;
- linked sources;
- personal UserState.

---

### 6.5 Claim / evidence retrieval

Best for generating trustworthy factual answers.

Once candidate entities/sources are found, important answer statements should be grounded in:

```text
Claim → EvidenceUnit → Source
```

rather than relying on a generated source summary alone.

---

### 6.6 Metadata-only source retrieval

Sources excluded by Processing Policy may still be searched by basic metadata.

Example:

```text
我之前跳过了哪些综艺片段？
```

However, metadata-only items should not be treated as understood knowledge.

If the user asks for details from one excluded item, the AI may propose/trigger one-time processing for that item.

---

## 7. Retrieval Planning

The planner chooses retrieval operations based on the structured query.

### Example A — Exact structured query

User:

```text
我收藏过哪些旺角人均 100 以下的日料？
```

Plan:

```text
Entity search: restaurant
↓
Structured filters:
- district = Mong Kok
- cuisine = Japanese
- price claim <= HKD 100
↓
Claim/evidence verification
↓
Answer
```

Vector search may be unnecessary.

---

### Example B — Semantic query

User:

```text
我以前收藏过哪些关于不要过度规划人生的观点？
```

Plan:

```text
Semantic search over KnowledgeItems / transcript chunks
↓
Full-text supplement
↓
Retrieve relevant Claims/Evidence
↓
Cluster related arguments
↓
Answer with source citations
```

---

### Example C — Mixed query

User:

```text
周末想和朋友在香港出去玩，不要太累，有没有我收藏过但还没去过的地方？
```

Plan:

```text
Structured:
- location = Hong Kong
- user_state != visited
- entity type = place/activity

Semantic:
- suitable_for_friends
- low physical intensity
- weekend

↓
Candidate fusion
↓
Evidence verification
↓
Rank / explain matches
```

This is where hybrid retrieval is most valuable.

---

## 8. Candidate Generation and Fusion

The system should generally retrieve broader candidate sets before answer generation.

Conceptually:

```text
Structured candidates
      +
FTS candidates
      +
Vector candidates
      +
Entity candidates
      ↓
Candidate fusion
      ↓
Re-ranking
      ↓
Evidence loading
```

V1 does not need a sophisticated learning-to-rank system.

Candidate fusion can initially use understandable weighted scoring or rank fusion, then be benchmarked with real collection queries.

Important ranking factors may include:

- exact constraint match;
- semantic similarity;
- evidence quality;
- entity resolution confidence;
- recency when relevant;
- user state;
- source diversity;
- processing coverage.

### 8.1 What V1 actually does: structured-first, not additive

The `+` in that diagram reads as a union, and for an unstructured question it is one. For a
plan carrying hard constraints, the implemented arrangement is stricter:

```text
QueryPlan (validated)
      ↓
Structured executor  →  qualifying entities + per-constraint support + rejections
      ↓
qualifying source ids  ──→  passed to FTS/vector as an allow-list
      ↓
fusion / re-ranking (within the allowed set only)
      ↓
Evidence packet
```

`retrieve_structured` runs the executor first and hands `qualifying_source_ids` to the
existing hybrid retriever as a `source_ids` allow-list. An empty allow-list short-circuits to
no candidates rather than degrading to unrestricted search — the distinction between "no
filter" (`None`) and "filtered to nothing" (`[]`) is load-bearing.

This is why hard-constraints-beat-similarity is a structural property here and not a scoring
weight that a large enough similarity score could overcome: a rejected candidate never enters
the ranked set, so there is no score it could win with. The cost is that similarity cannot
*rescue* a candidate whose structured evidence is missing, which is the intended trade —
absent evidence is a no-result with a reason (see 15), not a low-confidence guess.

Rejections are retained, not discarded. Each carries the entity, the constraint that failed
and a human-readable detail, which is what lets the system say "有匹配的店，但人均 150 超过
了 100" instead of "没找到".

---

## 9. Retrieval Targets by Question Type

Different questions should retrieve different primary objects.

### “Did I save something about X?”

Primary target:

```text
Source / KnowledgeItem
```

### “Which restaurants/places/tools did I save?”

Primary target:

```text
Canonical Entity
```

### “What did the creator say about X?”

Primary target:

```text
Claim + Evidence
```

### “What are the common ideas across these videos?”

Primary target:

```text
KnowledgeItems + Claims + RetrievalChunks
```

### “Where exactly did this come from?”

Primary target:

```text
EvidenceUnit + Source timestamp/frame
```

This prevents treating every retrieval problem as chunk search.

---

## 10. On-Demand Enrichment During Retrieval

Retrieval may find the right source but discover that it was only processed shallowly.

Example:

```text
User asks:
“那条视频具体推荐了哪三道菜？”

Candidate source found
processing_level = 1
no transcript / OCR evidence
```

The system should run a coverage check:

```text
Can current evidence answer reliably?
```

If no:

```text
on-demand enrichment
→ ASR and/or OCR/Vision
→ new EvidenceUnits / Claims
→ index update
→ answer
```

For explicitly excluded `metadata_only` items, this requires an explicit one-time user request or confirmation according to Processing Policy.

The broad exclusion rule remains intact unless the user changes it.

---

## 11. Evidence Assembly

Before the LLM writes a collection-grounded answer, the system should build an evidence packet.

A packet may include:

```text
relevant entities
relevant claims
source attribution
supporting EvidenceUnits
short source context
user personal state/notes
```

The answer model should not receive thousands of raw chunks when a smaller set of verified claims/evidence is sufficient.

### Evidence preference

For factual-style statements, prefer:

```text
explicit source statement / on-screen text
>
clear visual observation
>
model inference
```

Model inferences should remain labeled as inferences.

---

## 12. Source Diversity and Cross-Source Synthesis

When synthesizing a topic, retrieval should avoid returning ten near-duplicate chunks from one video.

The planner should attempt diversity across:

- sources;
- creators;
- time;
- viewpoints;
- entities.

Example:

```text
综合我收藏的 Agent 内容，大家共同认为未来会往哪里发展？
```

A useful synthesis should sample multiple relevant sources rather than allowing one long video to dominate the context window.

---

## 13. Conflicting Claims

Different saved sources may disagree.

Example:

```text
Source A: restaurant average price ≈ HKD 80
Source B: average price ≈ HKD 150
```

The system should not silently average or overwrite these claims.

Possible answer:

```text
你收藏的不同视频对价格描述不一致：
- 视频 A 提到约 HK$70–90
- 视频 B 提到约 HK$140–160

两条收藏时间不同，价格可能已经变化。
```

Conflicts are useful information, not database errors.

---

## 14. Citation Requirements

For `personal_required` answers, the system should cite the user's saved sources for material factual claims.

Ideal citation precision hierarchy:

```text
Source + timestamp + transcript/frame evidence
>
Source + evidence excerpt
>
Source-level citation
```

Not every conversational sentence needs a citation, but the user should be able to trace substantive claims.

### No fake provenance

The answer generator must never:

- cite a source that does not support the statement;
- attach collection citations to general model knowledge;
- imply that an AI inference was directly stated by the creator.

---

## 15. “No Result” Behavior

The system must be comfortable saying:

```text
我没有在你已经处理的收藏里找到足够证据。
```

Then it can distinguish possible reasons:

```text
- no matching save exists;
- relevant sources exist but are metadata-only;
- relevant sources have not been processed deeply enough;
- the query constraints are too narrow.
```

It may offer actions such as:

```text
搜索被跳过的收藏
放宽条件
深度处理候选视频
```

But it should not fabricate a match.

---

## 16. Conversation Memory / Follow-ups

The chat layer should preserve short-term conversational references.

Example:

```text
User: 我收藏过哪些旺角日料？
Assistant: A、B、C...
User: 第二家有什么推荐菜？
```

The second query should resolve:

```text
“第二家” → Entity B from previous answer
```

The conversation state may track:

```text
mentioned entity IDs
mentioned source IDs
active filters
active topic
current answer evidence set
```

This state is separate from the long-term personal knowledge base.

---

## 17. Personal State in Retrieval

UserState should influence answers where relevant.

Example:

```text
User: 给我找几个旺角餐厅
```

Possible useful distinctions:

```text
saved but unvisited
visited and liked
visited and disliked
```

If the user asks:

```text
有没有我收藏过但还没去过的？
```

UserState becomes a hard filter.

If the user asks:

```text
我以前去过哪些觉得不错？
```

personal ratings/notes become primary evidence.

Creator opinion and personal experience must remain separate in the answer.

---

## 18. Conversation Actions

The AI should eventually be able to do more than read.

Examples:

```text
“这家我已经去过了，给 3 分。”
→ update UserState

“以后这个博主的视频都别处理。”
→ create ProcessingRule

“这条虽然被跳过了，帮我处理一下。”
→ one-time processing override

“把这几个地方标成想去。”
→ update entity states
```

V1 implementation may stage these write actions after read-only retrieval is stable, but the architecture should anticipate them.

---

## 19. Resurfacing Uses the Same Retrieval Core

Resurfacing should not be a completely separate recommendation engine.

It can reuse the same retrieval/index foundation with different query triggers.

Example trigger:

```text
recent saves strongly cluster around AI Agent
```

Generated internal retrieval task:

```text
find recent + historical Agent-related KnowledgeItems
↓
cluster recurring topics
↓
identify repeated / novel ideas
↓
produce a resurfacing card
```

This keeps search, synthesis, and resurfacing based on the same evidence model.

---

## 20. V1 Retrieval Architecture

Conceptual architecture:

```text
USER QUERY
    ↓
QUERY UNDERSTANDING
    ├── intent
    ├── scope
    ├── constraints
    ├── semantic needs
    └── conversation references
    ↓
QUERY PLANNER
    │
    ├── Structured DB Search
    ├── Full-text Search
    ├── Vector Search
    ├── Entity Search
    └── Metadata Search
    │
    ↓
CANDIDATE FUSION / RE-RANK
    ↓
COVERAGE CHECK
    │
    ├── sufficient ───────────────┐
    │                            │
    └── insufficient             │
           ↓                     │
       On-demand enrichment      │
           ↓                     │
       updated evidence ─────────┘
    ↓
CLAIM / EVIDENCE ASSEMBLY
    ↓
ANSWER GENERATION
    ↓
CITATION VALIDATION
    ↓
RESPONSE
```

---

## 21. V1 Tool Boundary for the AI Agent

Rather than giving the LLM direct unrestricted SQL, expose narrow retrieval tools/interfaces.

Conceptual tools:

```text
search_sources(...)
search_entities(...)
search_text(...)
semantic_search(...)
get_entity(...)
get_source(...)
get_claims(...)
get_evidence(...)
get_user_state(...)
request_enrichment(...)
```

Future write tools:

```text
update_user_state(...)
create_processing_rule(...)
process_source_once(...)
```

This makes behavior testable, safer, and easier to debug than allowing arbitrary database manipulation.

---

## 22. Retrieval Quality Evaluation

Before V1 is considered ready, the system should maintain a small real-query evaluation set.

Example query families:

```text
exact remembered item
fuzzy semantic memory
structured location/price filter
entity lookup
cross-source synthesis
personal-state filter
source citation
no-result query
metadata-only excluded item
follow-up reference
```

For each query, evaluate at least:

```text
Did retrieval find the right source/entity?
Did the answer use the right evidence?
Were citations actually supportive?
Did the system avoid unsupported claims?
Was excluded content handled correctly?
```

The system should optimize for retrieval usefulness and trust, not only embedding similarity benchmarks.

---

## 23. Confirmed Decisions

### RET-001 — AI conversation is tool-planned retrieval, not plain vector RAG

The planner combines structured, full-text, semantic, entity, and evidence retrieval.

### RET-002 — User collection knowledge and general model knowledge must remain distinguishable

General knowledge must never masquerade as information recovered from the user's saves.

### RET-003 — Ambiguous queries inside the app are personal-first

Search the user's collection first and label the result accordingly rather than silently turning the product into a generic web/chat assistant.

### RET-004 — Different questions retrieve different primary objects

Sources, entities, claims, evidence, and retrieval chunks serve different query types.

### RET-005 — Important factual collection answers should be grounded in Claim + Evidence

Generated summaries alone are not sufficient provenance.

### RET-006 — Retrieval may trigger on-demand enrichment

Shallowly processed relevant items can be promoted when needed.

### RET-007 — Conflicting source claims are preserved and surfaced

Do not silently overwrite disagreement.

### RET-008 — Metadata-only excluded sources do not participate in normal knowledge answers

They remain discoverable through metadata search and can be processed once on explicit request.

### RET-009 — Personal UserState is part of retrieval

Visited/used/completed/liked/disliked status can be a filter or ranking feature.

### RET-010 — Resurfacing should reuse the same retrieval/evidence core

Avoid maintaining an unrelated black-box recommendation system.

---

## 24. Open Implementation Questions

To resolve during architecture / benchmarking:

- exact vector database implementation for V1;
- embedding model;
- rank-fusion algorithm and weights;
- candidate set sizes;
- reranker model or heuristic;
- latency budget for query-triggered enrichment;
- how much evidence to pass to the answer model;
- exact citation rendering UI;
- whether general/hybrid mode ships in the earliest MVP or shortly after collection-only chat.
