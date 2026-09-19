# Integration Plan V1

Status: Active  
Working branch: `integration/v1`  
Primary implementation base: Claude V1  
Behavior/reference implementation: GPT V1

## 1. Purpose

This document defines how the two independent V1 implementations converge into one production-quality mainline.

It exists to prevent the integration phase from turning into a third uncontrolled rewrite.

The guiding rule is:

> Preserve the stronger long-term architecture and data spine from Claude V1, while selectively importing the runtime behavior, product semantics, regression cases and pragmatic safeguards that GPT V1 demonstrated better.

The frozen experiment record is in `docs/EXPERIMENT_GPT_VS_CLAUDE_V1.md`.

## 2. Branch roles

```text
main
  specification / accepted mainline

exp/gpt-v1
  frozen GPT experiment

exp/claude-v1
  frozen Claude experiment

experiment-gpt-v1-final
  immutable GPT experiment tag

experiment-claude-v1-final
  immutable Claude experiment tag

integration/v1
  only active convergence branch

review/gpt-integration-audit
  temporary adversarial-review branch when needed
```

Rules:

- never develop new features on the frozen experiment branches;
- never move the experiment tags;
- do not mechanically merge GPT into Claude;
- do not merge `integration/v1` into `main` before the final acceptance review.

## 3. Roles

### Claude — primary integration engineer

Claude owns implementation on `integration/v1`.

Its responsibilities are:

- preserve the accepted domain/data architecture unless a defect proves it wrong;
- fix real runtime gaps;
- adapt useful GPT behavior into the integration architecture;
- keep documentation aligned with live behavior;
- make minimal, testable changes;
- keep CI green.

### GPT — independent adversarial reviewer

GPT is not a second primary builder after the experiment.

Its responsibilities are:

- assume apparently working code can still be wrong;
- attack invariants and edge cases;
- prefer failing regression tests over broad refactors;
- review real end-to-end behavior rather than reward abstractions;
- verify fixes after Claude remediation.

GPT should not redesign the integrated architecture unless a reproducible defect demonstrates a fundamental problem.

### Human / final reviewer

The human owner controls:

- when each stage begins;
- which findings are accepted;
- when `integration/v1` is ready for final review;
- the final merge to `main`.

## 4. Integration sequence

```text
Freeze experiment versions
        ↓
Create integration/v1 from Claude V1
        ↓
Stage 1 — establish independent CI
        ↓
Stage 2 — Claude integration pass
        ↓
Stage 3 — GPT adversarial audit + failing tests
        ↓
Stage 4 — Claude remediation
        ↓
Stage 5 — GPT verification pass
        ↓
Stage 6 — Structured Retrieval vertical slice
        ↓
Stage 7 — GPT Structured Retrieval audit
        ↓
Stage 8 — final system review
        ↓
integration/v1 -> main
```

## 5. Stage 0 — freeze the experiment

Already frozen references:

```text
GPT
tag: experiment-gpt-v1-final
sha: ecfd7843fa1c996f6c9dfac9f23bd0ce2b591b25

Claude
tag: experiment-claude-v1-final
sha: 5a8fba62aee0d79f7120d9961a78bc84483eaa6b
```

`integration/v1` was created from the Claude experiment.

Acceptance:

- tags point to the reviewed commits;
- experiment branches are treated as read-only;
- all new work occurs elsewhere.

## 6. Stage 1 — establish CI first

Owner: Claude

Goal: create an independent execution source of truth before integration changes accumulate.

CI must cover, where configured:

- clean backend installation;
- Alembic migration from empty SQLite;
- pytest;
- ruff;
- Python static type checking;
- clean frontend installation;
- frontend lint;
- frontend typecheck;
- frontend tests if present;
- production frontend build;
- deterministic fixture/demo smoke path where practical.

CI rules:

- no private Douyin account;
- no private API key;
- no `continue-on-error` for required checks;
- do not weaken existing tests to obtain green status.

Stage acceptance:

- GitHub Actions runs on `integration/v1`;
- all required jobs are green;
- README commands match actual CI/bootstrap commands.

Do not begin feature integration before this stage is green.

## 7. Stage 2 — primary integration pass

Owner: Claude

The first integration pass is intentionally focused. It must not become a general rewrite.

### P0-A — real Douyin pagination safety

Verify against the actual current sidecar contract.

Required behavior:

```text
data.items
data.cursor
data.has_more
```

with opaque cursor pass-through.

Must support:

- multi-page traversal;
- final page;
- repeated/non-advancing cursor detection;
- intermediate-page failure;
- no silent partial-success interpretation;
- no destructive membership pruning unless the traversal completed successfully.

A failed or interrupted walk must never cause unseen historical collection membership to be marked absent.

Also verify the actual sidecar health/readiness endpoints.

### P0-B — media acquisition -> ASR

Fix the missing bridge between remote media references and ASR.

Target flow:

```text
remote SourceAsset
↓
retryable/idempotent acquisition
↓
managed local asset
↓
ASR provider
↓
timestamped EvidenceUnit
```

Requirements:

- acquisition only after Processing Policy allows expensive work;
- local storage identity separate from remote URL;
- stale/changed media references handled safely;
- failures persisted/observable;
- fixture/native-subtitle path remains credential-free.

### P0-C — reversible, retroactive Processing Policy

Rule mutation must change current knowledge eligibility, not only future jobs.

Required transitions:

```text
processed
→ add exclude/metadata-only rule
→ historical run/evidence preserved
→ source leaves normal retrieval/wiki/current answers

remove/disable rule
→ source becomes eligible again
→ history remains intact
```

Current policy state and current decision pointers must remain accurate.

Required cases:

- creator exclude;
- collection rule;
- source override;
- broad exclude + per-source always-process;
- rule changes while work is queued.

### P1-A — semantic/content-type and keyword rules

Support the user requirement to skip classes such as:

```text
movie_clip
variety_clip
music_clip
meme
sports_highlight
other_entertainment
```

and keyword/hashtag rules.

Use a two-pass shape:

```text
metadata
↓
deterministic policy
↓
cheap triage when needed
↓
semantic policy
↓
expensive processing only if allowed
```

Expose these dimensions in API/UI.

### P1-B — deterministic Claim grounding validation

Keep Claude's typed Claim and many-to-many ClaimEvidence model.

Add a hard validator so that a model-produced claim is not durable merely because it names an EvidenceUnit.

At minimum verify:

- evidence belongs to the correct source/run context;
- model-returned supporting span occurs in the evidence after reasonable normalization;
- literal/numeric values claimed as observations are supported;
- invalid grounding is rejected or explicitly downgraded.

### P1-C — import high-value GPT regression behavior

Port or reimplement tests for:

- source disappearance/reappearance;
- collection movement;
- stale media reference cleanup;
- failed reprocessing keeps previous successful run current;
- superseded run cannot leak into current retrieval;
- workers cannot incorrectly claim the same job;
- excluded sources cannot leak through another index;
- citations use current evidence.

### P1-D — restrained Resurface V1

Implement useful UserState-driven resurfacing for states such as:

```text
want_to_go
want_to_try
want_to_learn
```

Resurface cards must remain provenance-backed and current-policy/current-run aware.

Do not add recommendation engines or notification systems in this pass.

### P1-E — remove misleading dead capability

Audit abstractions that imply a live feature but are not on a live path.

Known examples from the experiment review:

- unused QueryPlanner;
- inert KnowledgeItem writer path;
- JobTypes without handlers;
- Wiki content indexed but not actually retrieved.

For this pass:

- wire small pieces only when clearly necessary;
- otherwise remove/deprecate/document honestly;
- do not implement large new subsystems just to justify an abstraction.

## 8. Stage 3 — GPT adversarial audit

Owner: GPT

Branch: `review/gpt-integration-audit`, created from current `integration/v1`.

GPT should first run the full baseline suite, then try to falsify the system.

Priority attack areas:

### Capture

- multi-page pagination;
- incomplete walk;
- repeated cursor;
- page N failure;
- destructive pruning;
- disappear/reappear;
- collection movement;
- sidecar contract mismatch.

### Processing/media

- remote -> local -> ASR;
- stale URLs;
- source changing mid-run;
- paid work before policy gate;
- failed replacement run;
- retry/idempotency.

### Policy

- rule added after processing;
- rule deletion/disable;
- source override;
- semantic/keyword rules;
- queued jobs after rule mutation;
- leakage through FTS/vector/Wiki/entity paths.

### Provenance

- hallucinated evidence span;
- evidence from wrong source/run;
- stale claim;
- stale citation;
- invented citation marker;
- cross-source support.

### Entity

- same name, different entity;
- fuzzy auto-merge;
- alias behavior;
- reprocess behavior.

### Retrieval/conversation

- personal/general boundary;
- no-result hallucination;
- stale follow-up context;
- hard constraints bypassed by similarity;
- current-run isolation.

### Wiki

- excluded/superseded support;
- stale page after reprocess;
- broken support link;
- collapsed conflict;
- Wiki mistakenly treated as primary evidence.

### Jobs/transactions

- duplicate derivation;
- double claim;
- rollback losing audit;
- lease/retry mistakes;
- failure marked success.

Audit output should primarily be failing regression tests, not production rewrites.

Severity:

```text
P0 — corruption, misattribution, destructive behavior, or broken core real path
P1 — core product semantics materially wrong
P2 — important reliability/UX/maintainability problem
P3 — lower-priority polish
```

## 9. Stage 4 — Claude remediation

Owner: Claude

For each GPT finding:

1. reproduce independently;
2. accept or reject with evidence;
3. import the smallest valid regression test;
4. fix production code minimally;
5. run the full suite;
6. keep CI green.

Policy:

- all P0 must be resolved;
- P1 should be resolved unless genuinely blocked;
- fix localized P2 when low-risk;
- P3 may remain documented.

Tests must not be weakened to fit current behavior when the specification says otherwise.

## 10. Stage 5 — GPT verification

Owner: GPT

This is a verification pass, not another architecture pass.

Verify:

- every previous P0;
- every previous P1;
- all added regression tests;
- no new regression from remediation.

Do not modify production code during this pass.

## 11. Stage 6 — Structured Retrieval vertical slice

Owner: Claude

Only after the integration foundation is stable.

Canonical target query:

```text
我收藏过哪些旺角人均100以下的日料？
```

The system must represent this as actual constraints, not only embedding text.

Minimum target QueryPlan:

```text
intent
knowledge scope
target entity type
location/district constraints
typed numeric Claim constraints
text/semantic requirements
optional user-state constraints
conversation references
```

Example:

```text
entity_type = restaurant
district = 旺角
cuisine = Japanese
price_per_person <= 100
```

Execution model:

```text
Natural language
↓
validated QueryPlan
↓
bounded deterministic structured retrieval
+
FTS
+
vector where useful
↓
fusion/rerank
↓
Claim/Evidence verification
↓
cited answer
```

The model may propose a plan. It may not emit arbitrary executable SQL.

Hard structured constraints must not be overridden by FTS/vector similarity.

Minimum deterministic fixtures:

1. Mong Kok Japanese restaurant, price 80 -> included.
2. Mong Kok Japanese restaurant, price 150 -> excluded.
3. Mong Kok non-Japanese restaurant, price 70 -> excluded.
4. Japanese restaurant outside Mong Kok, price 80 -> excluded.
5. Same restaurant, creator claims 80 and 120 -> disagreement preserved.
6. Metadata-only source -> not treated as understood knowledge.
7. Superseded run has 80, current run has 130 -> old claim cannot satisfy.
8. Personal-required query with no qualifying current evidence -> honest no-result.

Expose enough diagnostics to inspect:

- parsed QueryPlan;
- structured candidate set;
- FTS candidates;
- vector candidates;
- fusion/ranking reason;
- final evidence packet.

## 12. Stage 7 — GPT Structured Retrieval audit

Owner: GPT

Critical invariant:

> FTS/vector relevance must never override a hard structured constraint.

Attack:

- numeric boundaries;
- Chinese number variants;
- approximate price phrases;
- missing numeric claim;
- conflicting claims;
- same name across districts;
- old/current run mismatch;
- excluded source;
- metadata-only source;
- highly similar but constraint-violating content;
- malformed planner output;
- unsupported operators;
- follow-up references;
- personal/general boundary.

Prefer failing tests over redesign.

## 13. Stage 8 — final system review

Before `integration/v1` may merge to `main`, perform a final review against:

- PRODUCT_SPEC;
- DATA_SCHEMA;
- PROCESSING_POLICY;
- AI_PIPELINE;
- WIKI;
- RETRIEVAL;
- ARCHITECTURE;
- PHYSICAL_SCHEMA;
- this Integration Plan;
- the experiment findings.

Final review areas:

```text
capture contract
policy semantics
processing/reprocessing
provenance
entity identity
structured retrieval
conversation
Wiki
resurface
jobs/retries
migrations
CI
dead code
documentation drift
```

## 14. Merge gate

`integration/v1` may merge to `main` only when:

- required CI is green;
- all accepted P0 findings are closed;
- all accepted P1 findings are closed or explicitly accepted as known limitations;
- Structured Retrieval vertical slice passes its fixture cases;
- current-run/current-policy isolation is tested;
- provenance/citation integrity is tested;
- real-sidecar contract behavior is covered by deterministic contract tests where live credentials are unavailable;
- docs describe the system that actually exists;
- final human/system review approves the state.

The final merge to `main` is a human-controlled action.

## 15. What is intentionally outside this integration pass

Do not allow integration to expand indefinitely.

The following remain later work unless directly required by a defect:

- full OCR/keyframe/Vision ladder;
- broad on-demand enrichment system;
- complete LLM Wiki route/select/integrate loop;
- external web verification;
- graph database;
- multi-user/SaaS architecture;
- recommendation engine;
- notification automation;
- large ontology.

The purpose of `integration/v1` is convergence and correctness, not feature maximalism.
