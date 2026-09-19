# GPT vs Claude V1 Implementation Experiment

Status: Frozen experiment record  
Repository: `guoohhh/douyin-knowledge`  
Shared baseline: `ee98fc7bd57363daec5069a2ad56bc90c4943be2`

## 1. Purpose

This document records the first independent implementation experiment for Douyin Knowledge.

Two strong coding models received the same product specifications and broad implementation mandate. They started from the same repository state and were asked to produce independent V1 implementations without inspecting each other's implementation.

The goal was not to decide which model is universally "better". The goal was to learn:

- which architectural choices survive independent implementation;
- which product semantics are easy to accidentally weaken;
- where a compact vertical-slice implementation is stronger than a larger architecture-first implementation;
- which implementation should become the basis of the integrated V1;
- what must be carried forward from the losing branch rather than discarded.

The two frozen implementations are permanent reference points, not active development branches.

## 2. Frozen artifacts

| Variant | Branch | Frozen tag | Final commit |
| --- | --- | --- | --- |
| GPT V1 | `exp/gpt-v1` | `experiment-gpt-v1-final` | `ecfd7843fa1c996f6c9dfac9f23bd0ce2b591b25` |
| Claude V1 | `exp/claude-v1` | `experiment-claude-v1-final` | `5a8fba62aee0d79f7120d9961a78bc84483eaa6b` |

Both branches were created from the same baseline commit.

The experiment branches and tags must remain frozen. Future work belongs on `integration/v1` or later mainline branches.

## 3. Review method and limitation

The comparison was architecture- and code-review-driven. It inspected:

- database models and migrations;
- processing policy behavior;
- capture/provider boundaries;
- processing and reprocessing semantics;
- evidence/claim/entity provenance;
- retrieval implementation;
- conversation and citation behavior;
- Wiki implementation;
- UI structure;
- tests and failure-handling code;
- current Douyin sidecar API contract where relevant.

At the time of the comparison, neither frozen commit had independent GitHub Actions status. Test counts reported by each implementation were therefore treated as supporting evidence, not as independently verified execution proof.

The approximate implementation size was:

| | GPT | Claude |
| --- | ---: | ---: |
| Added lines | ~5.9k | ~19.8k |
| Changed files | 44 | 138 |

Code volume was not treated as a quality metric.

## 4. High-level conclusion

The experiment produced two distinct engineering styles.

### GPT V1

GPT behaved like a strong product/startup engineer:

- prioritized a working vertical slice;
- used fewer abstractions;
- covered more immediately visible product behavior with less code;
- handled several real-world edge cases well;
- implemented Processing Policy behavior close to the user's original intent;
- produced a more plausible real Douyin -> media -> ASR path.

Its main weakness is that long-term domain/data boundaries were compressed for speed.

### Claude V1

Claude behaved more like a staff/principal engineer:

- modeled domain concepts explicitly;
- separated source truth, processing state, evidence, claims and conversation state;
- invested heavily in provenance and versioning;
- treated failure semantics, migrations and auditability seriously;
- built a stronger retrieval/conversation foundation;
- built a much stronger long-term Wiki/provenance foundation.

Its main weakness is architecture getting ahead of the real product path: several abstractions exist before their end-to-end behavior is actually wired, and several live-integration gaps were found.

## 5. Comparative findings

### 5.1 Data model and provenance — Claude stronger

Claude more faithfully implemented the intended data spine:

```text
Source
  -> ProcessingRun
  -> EvidenceUnit
  -> EntityMention / Claim
  -> Entity
  -> Wiki
  -> Conversation
```

Notable strengths include separate mutable processing state, versioned runs, many-to-many claim/evidence support, explicit conversation citations, aliases and entity-resolution state.

GPT intentionally compressed several dimensions into the Source model, including processing/policy state and some derived fields. This is simpler for V1 but becomes costly once a large private corpus exists.

Decision: preserve Claude's data/domain spine in the integrated V1.

### 5.2 Claim grounding — GPT behavior worth importing

GPT applied a particularly useful deterministic grounding rule: model-generated claims had to resolve to real evidence and a literal supporting quote/value before becoming durable.

Claude's provenance graph is stronger structurally, but its ClaimExtractor could persist a claim linked to an EvidenceUnit without deterministically proving that the model-returned evidence span actually occurs in that EvidenceUnit.

Decision: keep Claude's typed Claim/ClaimEvidence model and add GPT-style hard grounding validation.

### 5.3 Entity resolution — Claude stronger

Claude separates:

- exact identity resolution;
- aliases;
- fuzzy candidates;
- human-review ambiguity.

Fuzzy similarity does not automatically merge entities.

GPT's resolution is simpler and primarily based on normalized name/type identity.

Decision: keep Claude's conservative resolution architecture, then strengthen exact identity later with location/platform identifiers where available.

### 5.4 Processing Policy — GPT behavior materially stronger

GPT implemented more of the user-requested policy behavior:

- per-source;
- creator;
- collection;
- semantic/content type;
- keyword;
- cheap entertainment classification;
- retroactive reevaluation after rule changes;
- removal of newly excluded knowledge from normal retrieval while preserving history.

Claude's policy model and audit trail are stronger, but the frozen implementation had important behavior gaps:

- semantic policy was effectively inert in the pre-processing evaluator;
- the UI exposed fewer rule dimensions;
- rule changes did not reliably reevaluate already-processed Sources;
- current policy state could drift from current retrieval eligibility.

Decision: preserve Claude's rule/decision/state data model but port GPT's actual reversible policy semantics.

### 5.5 Real Douyin integration — GPT currently closer to a working path

Two material gaps were found in Claude's real sidecar path.

#### Pagination

The current sidecar contract exposes page information under `data.items`, `data.cursor`, and `data.has_more` (with related metadata also available). Claude's provider interpreted pagination fields incorrectly, risking a one-page traversal being treated as complete.

Combined with membership pruning, this could incorrectly mark historical collection membership absent after an incomplete walk.

GPT handled opaque pagination more defensively and included repeated-cursor protection.

#### Media acquisition

Claude stores a remote media reference in `SourceAsset.storage_key`, while its ASR pipeline expects a local file path. No complete acquisition stage connected those states, so real media transcription could be skipped even though fixture/native-subtitle flows passed.

GPT had a simpler but more complete remote-media -> local processing -> ASR path.

Decision: reimplement these GPT behaviors inside Claude's capture/job architecture.

### 5.6 Processing/reprocessing — both strong, Claude deeper

Both implementations protect the previous successful processing run until a replacement succeeds.

Claude additionally invested in:

- explicit run provenance;
- failure audit replay across transaction rollback;
- clearer current-run isolation.

Decision: keep Claude's versioned processing semantics and import GPT's high-value regression cases.

### 5.7 Retrieval and conversation — Claude stronger

Claude provides stronger foundations for:

- FTS + vector rank fusion;
- chunk/evidence relationships;
- personal/general/hybrid scope;
- persisted conversation state;
- follow-up resolution;
- inline citation markers;
- citation validation and evidence rail UX.

GPT's retrieval is smaller and easier to understand, but more source-level and manually weighted.

Decision: keep Claude's retrieval/conversation foundation.

### 5.8 Structured Query Planning — neither implementation finished the intended design

The intended system must eventually interpret queries such as:

```text
我收藏过哪些旺角人均100以下的日料？
```

as structured constraints such as:

```text
entity_type = restaurant
district = 旺角
cuisine = Japanese
price_per_person <= 100
```

Neither frozen implementation fully executes that design.

Claude contains a QueryPlanner abstraction but it is not the live retrieval path. GPT uses simpler term/structured heuristics.

Decision: Structured Retrieval is a dedicated post-integration vertical slice, not something to claim as complete in either experiment.

### 5.9 Compounding Wiki — Claude foundation stronger, neither reached the full LLM Wiki vision

GPT Wiki is primarily a compact entity dossier with revisions and claim support.

Claude includes a much richer foundation:

- WikiPage;
- WikiRevision;
- WikiSupport;
- WikiLink;
- integration runs;
- lint findings;
- quality structures.

However, the frozen Claude implementation also had a critical product gap: Wiki content could be indexed without being a real retrieval surface, so the conversation path did not yet benefit from the Compounding Wiki as intended.

Neither implementation fully implements the desired LLM-Wiki-style route/select/integrate/maintain loop.

Decision: keep Claude's Wiki schema/provenance foundation, wire it into retrieval later, and avoid calling the current implementation the final LLM Wiki design.

### 5.10 Resurfacing — GPT ahead

GPT implemented an initial UserState-driven resurfacing flow. Claude explicitly left resurfacing unfinished.

Decision: reimplement restrained resurfacing on top of Claude's UserState/Claim/Evidence model.

### 5.11 Testing and failure semantics — Claude stronger overall

Claude built a substantially broader test system and explicitly tested against migrated SQLite state rather than relying only on ORM `create_all`.

It also found and fixed subtle issues during self-audit, including:

- fake/mock answers paired with real citations;
- truncated structured model output being treated as successful empty extraction;
- audit records disappearing through transaction rollback.

GPT's suite was smaller but contained several excellent product-accident regression cases, including policy-after-processing, stale media and current-run isolation.

Decision: use Claude's test infrastructure and port GPT's strongest regression cases.

## 6. Review scorecard

These scores are an engineering-review rubric, not a benchmark and not an independent runtime measurement.

| Dimension | GPT | Claude |
| --- | ---: | ---: |
| Core data semantics | 7.0 | 9.2 |
| Provenance model | 7.5 | 9.0 |
| Claim-generation grounding | 9.0 | 7.5 |
| Entity resolution | 6.5 | 8.5 |
| Processing Policy behavior | 9.0 | 5.5 |
| Real Douyin path | 8.0 | 4.5 |
| Adaptive processing | 7.0 | 7.5 |
| Retrieval mechanics | 6.5 | 8.5 |
| Structured Query Planner | 4.5 | 5.0 |
| Conversation | 5.5 | 9.0 |
| Citation UX | 7.0 | 9.2 |
| Compounding Wiki foundation/current value | 5.5 | 7.5 |
| Resurfacing | 8.0 | 0 |
| Testing | 6.8 | 9.2 |
| Failure semantics | 7.5 | 9.0 |
| Simplicity | 9.0 | 6.5 |
| Long-term maintainability | 6.5 | 8.5 |
| UI engineering | 7.0 | 8.8 |
| Current V1 loop completeness | 8.0 | 7.0 |

Approximate static-review summary:

```text
Claude ~8.1 / 10
GPT    ~7.4 / 10
```

The important conclusion is not the numeric gap. It is that each branch wins in different failure-cost categories.

## 7. Integration decision

The integrated V1 uses:

```text
Claude architecture/data spine
+
GPT runtime/product behavior where proven better
```

Claude is the primary implementation base because changing long-lived data semantics after thousands of private items exist is more expensive than fixing localized runtime gaps.

GPT is retained as:

- a behavior reference implementation;
- a source of regression cases;
- an independent adversarial reviewer.

The experiment branches must not be mechanically merged.

## 8. What must be carried from GPT into the integrated implementation

At minimum:

1. correct/defensive sidecar pagination behavior;
2. complete media acquisition -> ASR behavior;
3. retroactive and reversible Processing Policy;
4. semantic/content-type and keyword policy behavior;
5. deterministic Claim grounding validation;
6. source disappear/reappear and collection-movement regression cases;
7. stale-media regression behavior;
8. failed-reprocess/current-run isolation tests;
9. restrained UserState-driven resurfacing.

These behaviors should be reimplemented inside Claude's architecture rather than copied wholesale.

## 9. Frozen experiment rule

Do not continue feature development on:

```text
exp/gpt-v1
exp/claude-v1
```

Do not move their final tags.

They are experimental evidence.

All convergence work belongs on:

```text
integration/v1
```

See `docs/INTEGRATION_PLAN_V1.md` for the execution plan.
