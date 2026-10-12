# Douyin Knowledge — Engineering Retrospective

Status: Living document  
Purpose: engineering memory / architecture understanding / interview evidence / handoff  
Evidence baseline: b08efb04f975641b41c0a368eba0a35bf88245bd  
Baseline branch at review time: phase3/real-douyin-sidecar  
First retrospective pass: 2026-09-29

> This document is not a changelog and not a second README.
>
> Its job is to explain how the system became what it is: which real problem forced each important design, what failed on real data, which invariant emerged from the failure, where that invariant lives in code, how it is tested, and what remains uncertain.

---

## Current Engineering State / Handoff Snapshot

**Review cutoff (2026-10-13):** `integration/v1` = `e1af20e5f839ec069d4a06adb42aa790bc876dd9` (PR #2 → PR #3); `main` remains at `ee98fc7bd57363daec5069a2ad56bc90c4943be2`. The **original historical retrospective baseline** is `b08efb04f975641b41c0a368eba0a35bf88245bd`, not today's code HEAD.

**Coverage:** architecture/invariants (§2), GPT-vs-Claude provenance and integration (§3, §6), sidecar/media/asset/grounding/retrieval/citation failures (§7–12), long-audio ASR (§13), Capture Scope (§13B), outcome semantics (§13C), scheduler and snapshot history (§13D); source/test map (§18), milestone SHA map (§19). This is an evidence-based retrospective, **not** an assertion of exhaustive independent code audit.

**Current milestone states:**
- **Stage 3D:** bounded-session ASR + timestamp restoration were REAL VALIDATED at the historical `b08efb0` source; conservative boundary-stitching code/tests (`4142ae8`, `638585f`) are now **CODE INTEGRATED** in `integration/v1`, but post-fix **REAL ASR / downstream EvidenceUnit validation remains pending**.
- **Stage 3E:** PR #2 **MERGED** (`1c494b7`). Stage 3E-6 full real-account + isolated DTK + browser workflow is **REAL E2E REPORTED BY CODEX / ACCEPTED BY PROJECT COORDINATOR / ORIGINAL LOCAL ARTIFACTS NOT INDEPENDENTLY ARCHIVED**; 16 default + 16 named observations overlap on 16 unique Sources (§13B). Do not confuse this accepted report with repository-archived raw test logs, or extrapolate it to second-page, multi-folder or multi-identity trials.
- **Stage 3F:** PR #3 **MERGED** (`e1af20e`); **CODE IMPLEMENTED / UNIT TESTED / CI VALIDATED**. Local report: 933 passed, 4 skipped; integration Backend/Frontend/Integration CI all SUCCESS. **REAL SCHEDULED E2E NOT TESTED**, including cross-day real-account runs (§13D).

**Load-bearing invariants:** capture selection ≠ observation ≠ Processing Policy; only completed authoritative page walks may reconcile absence; a Source remains one entity across targets; unpinned identity fails closed; transport/session reliability ≠ transcript semantic reconciliation; Evidence/Claim/citation must remain grounded; root job fan-out ≠ completed target work; automatic sync must not silently trigger paid AI; snapshots track content changes rather than signed-URL churn.

**Evidence boundaries:** Stage 3E real-run counts below are an *accepted coordinator-provided Codex report*, not independently archived original artifacts. Stage 3F's 8-source/8-snapshot repeat-sync result is *simulation* and is not a post-fix measurement of the actual 16-source corpus. Passing mock/fixture CI is not live provider proof.

**Priority open problems:** Stage 3D post-stitch live ASR + EvidenceUnits; Stage 3E real second page, multiple selected named folders, independent multi-identity provenance, long-running stability; Stage 3F live cross-day restart/scheduler operation and real-corpus snapshot measurement; incomplete Stage 3B/C run artifacts (§14–16). New maintainers should read **§0 → §2 → §13 → §13B → §13D → §15–16** before changing claims.

---

## 0. How to read and maintain this document

### 0.1 Evidence policy

The retrospective should distinguish evidence quality instead of flattening all project history into one narrative.

Use this priority order:

1. **Current code and tests at the reviewed SHA.** This is the strongest evidence for what the system actually guarantees now.
2. **Commit diffs and commit messages.** These are the strongest evidence for how a defect was discovered and why a change was made.
3. **Living design documents and ADR-style decisions.** These explain intended semantics, but may lag implementation.
4. **PR descriptions, CI output, reproducible local commands and retained real-run artifacts.**
5. **Human/agent recollection.** Useful for reconstructing motivation, but must be labeled when the repository cannot independently prove it.

Do not silently upgrade category 5 into category 1.

When code and an older design document disagree, record both the original design and the as-built decision. Examples already present in the repository include the vector backend changing from LanceDB-by-design to numpy-by-default, and the Wiki being indexed but not yet a retrieval surface.

### 0.2 What every mature engineering story should eventually contain

For each important topic, answer:

- What problem were we trying to solve?
- What did the first implementation do?
- Why did that first design look reasonable?
- What alternatives existed?
- What did fixtures/tests fail to reveal?
- What did real data or real providers reveal?
- What was the root cause?
- Which tempting/simple fixes were rejected, and why?
- What was the final fix?
- What invariant should never regress?
- Which modules own the behavior?
- Which tests pin the invariant?
- What was verified against real data?
- What P2 gaps/tradeoffs remain?
- How should this be explained in an interview?

### 0.3 Update protocol

When an important bug or design change lands:

1. Add the commit SHA to the relevant story.
2. Update “first version → failure → final invariant”, not only “current behavior”.
3. Add or update the code-path and test-path map.
4. Separate real validation from deterministic tests.
5. Add accepted gaps rather than making the story sound cleaner than reality.
6. If the change creates a reusable interview story, add a short interview card in the final section.

---

## 1. Project thesis: Personal External Memory, not a downloader

The project started from a product problem rather than a technology demo: a saved Douyin item is easy to collect but hard to recover and reuse later.

The core product loop is therefore:

~~~text
Capture
  ↓
Processing Policy
  ↓
Evidence acquisition
  ↓
Structured interpretation
  ↓
Knowledge integration
  ↓
Retrieval
  ↓
Grounded answer
  ↓
Resurface / user state
~~~

The important distinction is that **saving content is not the same as converting it into knowledge**.

The product specification repeatedly separates:

- Source: what was actually saved.
- Evidence: the smallest source-derived unit that can support a citation.
- Claim: what a source/creator asserted.
- Entity: a canonical reusable thing referred to across sources.
- UserState: what the user personally wants, did, rated or noted.
- Wiki: a compiled, rebuildable view.
- Conversation: a query-time synthesis over the above.

This is the basis of the “Personal External Memory” framing: the system should help recover “I remember saving something about this” and turn it into a traceable answer, without pretending that a model summary is the original truth.

Primary repository evidence:

- docs/PRODUCT_SPEC.md
- docs/DATA_SCHEMA.md
- docs/AI_PIPELINE.md
- docs/WIKI.md
- README.md
- AGENTS.md

---

## 2. The load-bearing architecture and invariants

### 2.1 Current high-level data flow

~~~text
Douyin account/session
        ↓
DTK / Douyin_TikTok_Download_API sidecar
        ↓
DouyinCaptureProvider
        ↓
Source / SourceSnapshot / SourceAsset
        ↓
Processing Policy + SourceProcessingState
        ↓
local media acquisition
        ↓
ASR / other evidence acquisition
        ↓
EvidenceUnit
        ↓
ProcessingRun
        ↓
EntityMention / Claim / Entity
        ↓
FTS + vector + Structured Retrieval
        ↓
CitationSet
        ↓
grounded answer
~~~

Wiki and UserState sit beside, not above, the provenance spine:

~~~text
Source → EvidenceUnit ← ClaimEvidence ← Claim → Entity
                  ↑
             RetrievalChunk

Entity ─────────────→ EntityUserState
Claim / Evidence ───→ WikiSupport → WikiRevision → WikiPage
~~~

### 2.2 Non-negotiable invariants

The following invariants recur across code, tests and design documents:

1. **Source is not knowledge.** AI interpretation never replaces the captured source record.
2. **Evidence is first-class.** A durable factual-looking assertion should have a path back to evidence.
3. **Claim is attributed, not global truth.** Two creators can disagree and both claims can remain valid data.
4. **UserState is separate from creator/source claims.**
5. **Reprocessing is versioned.** Current knowledge is selected by current_processing_run_id; old runs remain historical.
6. **Policy is a visibility rule, not destructive deletion.**
7. **RetrievalChunk is not EvidenceUnit.** Retrieval needs larger context; citation needs precise support.
8. **Hard structured constraints determine membership; similarity may only rank the qualifying set.**
9. **Wiki is derived and rebuildable.**
10. **Search/vector indexes are rebuildable accelerators, not canonical truth.**
11. **A missing citation is a correctness problem, not a presentation detail.**
12. **When the system cannot prove a condition, it should refuse or omit rather than fabricate provenance.**

Key implementations:

- backend/src/douyin_knowledge/db/models/processing.py
- backend/src/douyin_knowledge/db/models/entities.py
- backend/src/douyin_knowledge/db/models/userstate.py
- backend/src/douyin_knowledge/knowledge/eligibility.py
- backend/src/douyin_knowledge/conversation/citation_builder.py
- backend/src/douyin_knowledge/conversation/grounding.py
- backend/src/douyin_knowledge/wiki/builder.py

---

## 3. Development history: specification-first, dual implementation, convergence, real-data hardening

The project history is itself an engineering story.

### Phase 0 — specification before implementation

On 2026-09-17 the repository first accumulated the product specification, knowledge model, AI pipeline, Processing Policy, Retrieval design, architecture, Wiki design, physical schema, task plan and agent guardrails.

The implementation baseline was then frozen at:

- ee98fc7bd57363daec5069a2ad56bc90c4943be2

This mattered because the coding agents were not asked to invent the product from scratch; they were asked to implement an explicit set of invariants.

### Phase 1/2 — independent GPT and Claude implementations

Two branches started from the same baseline:

- exp/gpt-v1 → frozen final ecfd7843fa1c996f6c9dfac9f23bd0ce2b591b25
- exp/claude-v1 → frozen final 5a8fba62aee0d79f7120d9961a78bc84483eaa6b

The experiment showed two different engineering styles:

- GPT produced a smaller, more direct vertical slice and was stronger on several real-path/product behaviors.
- Claude produced a richer data/provenance model, stronger versioning, retrieval/conversation foundations, Wiki foundations, migrations and tests.

The integration decision was deliberately not “merge both” and not “pick the model with more code”. It was:

> preserve Claude's data/provenance spine, then reimplement the strongest GPT runtime behavior inside that architecture.

Primary evidence:

- docs/EXPERIMENT_GPT_VS_CLAUDE_V1.md
- docs/INTEGRATION_PLAN_V1.md
- branches exp/gpt-v1, exp/claude-v1, integration/v1

### Phase 2 integration — adversarial convergence

The integration plan formalized separate roles:

~~~text
Claude primary integration
        ↓
GPT adversarial audit / failing tests
        ↓
Claude remediation
        ↓
GPT verification
        ↓
Structured Retrieval vertical slice
        ↓
Structured Retrieval adversarial audit
~~~

This is important for the retrospective because several later invariants were not present in either frozen implementation. They emerged from **cross-implementation review + adversarial tests + real execution**.

### Phase 3 — real Douyin account and provider validation

The Phase 3 branch diverged from `integration/v1` at `62b4f25656ed4e33452d534804d0c7c5afacbf7b`; `b08efb04f975641b41c0a368eba0a35bf88245bd` is the historical Stage 3D segmentation validation baseline, not the latest integration SHA. The accepted Phase 3 sidecar branch later reached `423dee1` and [PR #1](https://github.com/guoohhh/douyin-knowledge/pull/1) was merged on 2026-10-12.

The current working stages are:

- Stage 3A — real metadata synchronization
- Stage 3B — real media → Doubao ASR → Evidence
- Stage 3C — real retrieval → Answer → Citation
- Stage 3D — full-corpus robustness, currently ongoing at this baseline

Historical transition: [PR #1](https://github.com/guoohhh/douyin-knowledge/pull/1), “Phase 3: validate real Douyin sidecar integration,” was marked **merged** by GitHub on 2026-10-12, but the recorded `merge_commit_sha` is **its head commit `423dee12bf68a21fe3e022a4925db12233550394`**, not a separate merge commit. The Phase 3 commits are present as ancestors of the Stage 3E PR #2 head (`8087006`), which entered `integration/v1` through actual merge commit `1c494b7`. The PR #1 merged flag must **not** be narrated as an independent integration merge. This ancestry says nothing about post-stitch live ASR validation.

---

## 4. Complete retrospective chapter plan

This is the intended long-term structure. Sections marked [seeded] already have enough repository evidence to begin; [partial] have code evidence but missing historical/real-run evidence; [future] depend on ongoing Stage 3D or later work.

1. **Project thesis and product constraints** [seeded]
   - Personal External Memory
   - why not downloader / bookmark manager / generic RAG
   - zero-friction capture vs selective processing
2. **System architecture and provenance spine** [seeded]
   - Source / Evidence / ProcessingRun / Claim / Entity / UserState
   - current-run semantics
   - derived vs canonical state
3. **AI-assisted engineering process** [seeded]
   - spec-first
   - GPT vs Claude independent implementations
   - integration strategy and adversarial review
4. **Processing Policy as a first-class product layer** [seeded]
   - first metadata gate
   - semantic rules and cheap triage
   - retroactive/reversible policy
   - one eligibility rule across retrieval/Wiki/API
5. **Douyin capture boundary and DTK sidecar** [seeded]
   - why sidecar, not embedded reverse-engineering
   - actual API contract vs fixtures
   - identity pinning
   - pagination safety
   - incomplete walk / destructive pruning invariant
   - default/all-favorites limitation [partial]
6. **Media acquisition and asset lifecycle** [seeded]
   - remote_url vs storage_key vs download_state
   - signed URL fingerprinting
   - acquisition jobs
   - stale media
   - multi-asset set reconciliation
   - retirement/reactivation
7. **Dependency and environment robustness** [partial]
   - Python compatibility
   - SQLAlchemy 2.1 drift
   - env/config drift
   - CI as independent source of truth
8. **Evidence acquisition and Doubao Seed-ASR** [seeded]
   - first single-session adapter
   - MP4 → 16 kHz mono WAV
   - WebSocket protocol
   - timestamped EvidenceUnit
   - provider error taxonomy
9. **Structured extraction with DeepSeek** [seeded]
   - provider abstraction
   - OpenAI-compatible transport without collapsing provider identity
   - forced tool call + JSON Schema
   - fail-closed finish reasons
   - thinking disabled for deterministic Stage 3 validation
10. **Claim grounding and knowledge eligibility** [seeded]
    - GPT behavior imported into Claude spine
    - span/value/source validation
    - Chinese normalization/numerals
    - downgraded vs rejected
    - global eligibility predicate
11. **Retrieval architecture** [seeded]
    - FTS5 + numpy vector fusion
    - why numpy replaced LanceDB default
    - current-run/current-policy filters
    - source vs chunk vs evidence
12. **Structured Retrieval** [seeded]
    - QueryPlan
    - hard constraints before similarity
    - supported fields and refusal boundary
    - conflict preservation
    - current known limits
13. **Answer grounding and citation correctness** [seeded]
    - numeric marker validation was insufficient
    - whole-answer grounding validator
    - deterministic fallback
    - same-source prompt grouping
    - evidence-budget failure
    - structured qualification provenance
    - multi-evidence citation projection
14. **Compounding Wiki** [seeded]
    - compiled view, not source of truth
    - statement-level support
    - conflict preservation
    - policy/current-run currency
    - archive when support disappears
    - known gap: indexed but not retrieval surface
15. **Stage 3D long-media reliability** [seeded]
    - real 841.721 s validation source
    - provider-side long-session nondeterminism
    - segmented provider sessions — REAL VALIDATED
    - global timestamp restoration — REAL VALIDATED
    - cross-boundary reconciliation — midpoint-only rule DISPROVEN / diagnostic ongoing
    - bounded retry and safe diagnostics
16. **Testing and real-account E2E methodology** [partial]
    - fixtures vs contract tests vs live sidecar tests
    - prove-fail-first regressions
    - tests that assert user-visible output
    - real-run evidence ledger
17. **Known P2 tradeoffs and technical debt** [seeded]
    - candidate cap/order dependence
    - source-level structured ranking
    - inert QueryPlan fields
    - KnowledgeItem no writer
    - Wiki not retrieved
    - partial-run checkpoint/resume
18. **Interview story cards** [future/continuous]
    - one-page narratives: context → failure → diagnosis → invariant → result
19. **Appendix A — code map**
20. **Appendix B — commit/story map**
21. **Appendix C — real-validation ledger**
22. **Appendix D — unresolved historical questions**

---

## 5. Confirmed engineering stories from repository evidence

This section is an index. Each story should later become a full narrative chapter/subchapter.

| Story | What the repository already proves | Primary evidence |
| --- | --- | --- |
| Spec-first architecture | Product/data/retrieval/wiki/policy specs existed before implementation | docs/* commits on 2026-09-17 |
| GPT vs Claude experiment | Same baseline, independent builds, frozen branches, explicit convergence strategy | EXPERIMENT_GPT_VS_CLAUDE_V1.md; INTEGRATION_PLAN_V1.md |
| Sidecar contract mismatch | Fixtures agreed with the client rather than the real sidecar; cursor/task/health/stats bugs coexisted | f54ee228f5b9; capture/douyin_provider.py |
| Incomplete walk must not prune | Partial pagination cannot be treated as a complete authoritative set | capture/pagination.py; test_sync_pruning.py |
| Remote media was not local media | Signed URL had been stored in storage_key; real ASR silently skipped while fixture subtitles hid it | 16604f9bceba; DEC-014 |
| Multi-asset reconciliation | Per-item same-kind replacement breaks image albums; reconciliation must be set-based | f2306b5c0986; test_multi_asset_sync.py |
| Asset lifecycle repair | Sync-retired assets must reactivate; acquisition-terminal assets must not; reconciliation must run even if source snapshot is unchanged | 23acb6fa9858; 99-asset DB note in commit |
| SQLAlchemy dependency drift | Supported range was explicitly pinned to >=2.0.36,<2.1 | a588e565b1ff; backend/pyproject.toml |
| Policy retroactivity | A rule change must hide already-processed knowledge without deleting history | 9612a28c7fe6; DEC-015 |
| Semantic policy was previously inert | Stored semantic/hashtag rules could exist without affecting processing | c5ce13473055; DEC-016 |
| One eligibility definition | Retrieval, structured retrieval, Wiki and knowledge APIs need the same current-run/current-policy/grounding rules | e8e1f714e965; knowledge/eligibility.py |
| Claim grounding | A real evidence_id alone is not enough; source, span and value must agree | 1b5fcd0a6bc1; extraction/grounding.py |
| Structured Retrieval | Hard constraints choose membership, similarity ranks only survivors | 8139daa84bb1 through 62b4f25656ed; DEC-020/021 |
| Doubao ASR adapter | Direct WebSocket adapter, ffmpeg preparation, normalized utterance timestamps | 2dc9d3af3a17; doubao_adapter.py |
| DeepSeek structured model | Forced tool call, JSON Schema validation, deterministic non-thinking mode | 3fee8ef56a3f; deepseek_adapter.py |
| Fail closed on ungrounded generated answers | Valid ordinals are not sufficient; every substantive model assertion needs provenance | e6dd0ba82e35; conversation/grounding.py |
| Evidence budget can delete the answer | Redundant metadata chunks can crowd out long ASR evidence despite correct retrieval | e6dd0ba82e35; test_grounded_answer_contract.py |
| Qualification needs renderable provenance | Executor qualification is not enough; every required claim-derived condition must be citable before saying a result qualifies | 2f6be242e21d; test_provable_qualification.py |
| Chunk is not citation | One ASR chunk may map to dozens of evidence units; selecting one arbitrary unit can cite unrelated speech | e6afd9f8d173; test_evidence_projection.py |
| Long-media WebSocket reliability | A real 841.721 s prepared WAV completed successfully when split into three bounded Doubao sessions; all sessions succeeded with zero retries | b08efb04f975; real Stage 3D validation |
| Segmented ASR status | Bounded provider sessions and restoration to the original media timeline are REAL VALIDATED. Midpoint-only overlap reconciliation is DISPROVEN as sufficient because adjacent sessions can segment the same underlying speech into different utterance boundaries | b08efb04f975; real Stage 3D boundary diagnostics |
| Capture Scope vs observation vs policy | V1 target selection, last membership observation and eligibility have distinct lifecycles; clean-fixture bootstrap regression found in CI | e5ee8e9; e50e871; 065376f; PR #2 |
| ASR success without evidence | Successful empty output must not claim L2 or create fake timestamp-less evidence; provider errors remain distinct | 423dee12; test_media_asr_closes_loop.py |
| Logging metadata can break extraction | Reserved LogRecord extra field was changed and explicitly tested at INFO level | ddfbbb91; test_extraction.py |
| Wiki is compiled state | Policy/current-run/grounding changes can remove support and archive pages without deleting history | Wiki builder + DEC-019 |

---

## 6. Seed story: why the GPT/Claude experiment mattered

### Problem

A project built largely with coding agents has a special risk: one agent can produce a coherent implementation whose hidden assumptions are never challenged.

The experiment therefore used two implementations from the same spec baseline rather than treating a single agent's output as the architecture.

### What the branches revealed

The GPT branch demonstrated that a smaller vertical slice could expose practical behaviors earlier: Processing Policy semantics, a more complete media path, stale media handling and resurfacing.

The Claude branch demonstrated that several concepts were expensive to retrofit later: separate ProcessingRun, EvidenceUnit, ClaimEvidence, entity identity, conversation citation state, Wiki revisions and broad migration/test discipline.

### Decision

The convergence strategy was not source-level merging. The repository explicitly chose:

~~~text
Claude data/provenance architecture
        +
selected GPT runtime behavior and regression cases
~~~

This is a reusable engineering lesson: **when two implementations differ, integrate invariants and tested behaviors, not file trees**.

### Interview angle

A strong explanation is not “I asked two AIs and chose the better one.” It is:

> I used independent implementations as an architecture experiment. Their failures were complementary. I froze both, documented what each did better, chose the data model with the higher future migration cost as the base, then reimplemented the other branch's better runtime behavior behind that model and used the losing branch as an adversarial regression source.

---

## 7. Seed story: real sidecar contract beat self-consistent fixtures

### First version

The Douyin provider already had fixtures and provider tests. It looked covered.

### Real defect

Commit f54ee228f5b9 records four simultaneous contract defects:

1. pagination cursor read from the wrong response shape;
2. async task states did not match the sidecar's actual states;
3. the health endpoint was a nonexistent API path that the SPA catch-all answered with HTTP 200;
4. missing statistics became None where the internal model required a dict.

The collection list also read only its first page.

### Why the tests missed it

The fixture provider encoded the assumptions of the client. Client and fixture could agree while both disagreed with the real sidecar.

### Final invariant

> External contracts need tests derived from the external contract, not only fixtures derived from our client.

A second invariant follows:

> An incomplete authoritative walk may upsert observations, but it must never run destructive pruning.

This asymmetry is important: partial positive observations are safe to retain; absence is only trustworthy after a complete traversal.

### Code/tests

- capture/douyin_provider.py
- capture/pagination.py
- capture/sync.py
- tests/unit/test_collection_pagination.py
- tests/unit/test_douyin_provider_transport.py
- tests/unit/test_sync_pruning.py

---

## 8. Seed story: media URL, local bytes and acquisition state are different facts

### First version

The capture layer wrote a provider media URL into SourceAsset.storage_key.

That seemed superficially reasonable because the field identified “the media”.

### Real defect

The processing layer interpreted storage_key as a path relative to the local media directory. A signed HTTPS URL therefore became an impossible local path.

Real sources silently recorded asr_skipped_media_not_downloaded.

### Why the suite missed it

The fixture corpus provided native_subtitle, allowing the pipeline to reach Level 2 without downloading a real media file.

### Final design

SourceAsset now separates:

- remote_url — current provider location;
- storage_key — deterministic local path;
- download_state — whether usable bytes exist locally;
- remote_url_fingerprint — stable content identity across URL re-signing.

Acquisition is a separate job. Processing Policy is checked again at the spending boundary.

### Invariant

> A locator, local storage identity and acquisition state must never share one field.

### Important rejected/simple fixes

Using the whole signed URL as identity would trigger re-download on every re-sign.

Downloading inside transcribe() would make retries re-fetch data, weaken resumability and mix network acquisition with AI processing.

### Evidence

- commit 16604f9bceba
- DEC-014
- media/store.py
- media/downloader.py
- media/service.py
- jobs/handlers.py
- media unit tests

---

## 9. Seed story: multi-asset reconciliation is a set problem

### Real shape

A Douyin image album can contain A, B and C, all with asset_type=image.

### First implementation mistake

The old logic reconciled one captured media item at a time. When processing B, it could interpret A as a stale same-kind asset; processing C could then retire B.

This accidentally assumed:

> one source has at most one current asset of a given kind

but the schema already allowed:

> one source has multiple assets of the same kind, distinguished by fingerprint.

### Root cause

The unit of reconciliation was wrong.

The authoritative information is not “this media item exists”; it is “this complete capture says the current asset set is S”.

### Final invariant

~~~text
stored current set
        vs
captured authoritative set
~~~

Retire exactly stored identities absent from the captured set. Do not infer replacement from matching asset_type alone.

### Follow-up lifecycle defect

The first set-based repair still had a real-database recovery issue. The 99-asset database had 27 pending and 72 unavailable assets. Re-syncing the unchanged source payload could not repair those rows because asset reconciliation only ran when the source snapshot changed.

Commit 23acb6fa9858 changed two things:

- reconciliation runs on every authoritative capture even when the source snapshot is unchanged;
- sync-retired unavailable assets can return to pending when they reappear, while acquisition-terminal unavailable assets remain terminal.

### Invariants

1. Snapshot deduplication and asset reconciliation are separate concerns.
2. A disappeared asset is historical state, not a row to delete.
3. Reappearance after sync retirement is repairable.
4. Permanent acquisition failure must not be reset merely because the URL appears again.
5. A repeated identical sync should be idempotent.

### Evidence

- f2306b5c0986
- 23acb6fa9858
- capture/sync.py
- tests/unit/test_multi_asset_sync.py

---

## 10. Seed story: Claim grounding is stronger than having a citation edge

### Original structural strength

The Claude data model already had:

~~~text
Claim → ClaimEvidence → EvidenceUnit → Source
~~~

That is much better than source-level citations.

### Hidden weakness

A model could still attach the wrong semantic content to a real edge. For example, a claim “人均80” could point at evidence saying “人均800”.

A valid foreign key proves identity integrity, not semantic support.

### Imported GPT behavior and strengthened implementation

The integrated implementation adopted the GPT branch's hard-grounding idea but made it robust for CJK/ASR:

- evidence must belong to the expected source;
- evidence span is normalized with NFKC/punctuation/whitespace handling;
- literal/numeric values must be present;
- common Chinese numerals can ground Arabic normalized values;
- normalized vocabulary predicates such as cuisine/located_in validate against known surface forms.

### Reject vs downgrade

Hard semantic contradictions are rejected.

A missing/hallucinated quoted span with an otherwise plausible assertion is stored as downgraded audit state, but is not assertable in Wiki or answers.

This separation matters because retaining the model's bad output helps diagnose extraction quality without allowing it to become user-facing “knowledge”.

### Invariant

> Provenance is not just a graph edge. The assertion carried by the edge must be supportable by the referenced evidence.

---

## 11. Seed story: Structured Retrieval makes hard constraints structural

The canonical motivating query is equivalent to:

> Which saved Mong Kok Japanese restaurants cost less than 100 per person?

A pure vector/FTS approach can rank a 150-per-person restaurant highly because it strongly matches the words “Mong Kok” and “Japanese”.

The project therefore does not represent price/location/cuisine as ranking preferences. A validated QueryPlan is executed first.

~~~text
Natural language
  ↓
validated QueryPlan
  ↓
deterministic qualification
  ↓
qualifying entity/source allow-list
  ↓
FTS/vector ranking only inside the allow-list
  ↓
Claim/Evidence verification
~~~

The core invariant from DEC-020 is:

> Hard structured constraints choose the candidate set. Similarity may only order that set.

This is more robust than giving structured constraints a large score weight. A scoring weight can always be overwhelmed by later tuning; an entity absent from the candidate set cannot be resurrected by relevance.

The executor also preserves conflicting current claims. If one source says 80 and another says 120, an entity may qualify for <100 because 80 is supported, but the answer should preserve the 120 conflict rather than collapsing the entity into a single “price=80” fact.

Unsupported semantics are refused rather than approximated when approximation could invert the query. DEC-021 documents examples such as negated cuisine/location and multiple incompatible price conditions.

Known P2 limits at this baseline include:

- MAX_CANDIDATES=500 before qualification, with order-dependent pre-cap selection;
- ranking is source-level rather than entity-evidence-pair-level;
- some QueryPlan fields are stored but not executed;
- narrow structured vocabulary/field coverage;
- negation is refused rather than executed.

---

## 12. Seed story: “cited” output can still be ungrounded

Stage 3C exposed several layers where correct retrieval still produced an incorrect user-facing answer.

### Failure A — citation syntax was validated, assertions were not

The old validator checked whether numeric markers referred to real citation ordinals.

That did not catch:

- pseudo-markers such as [第五步];
- factual sentences with no marker at all.

Removing an invalid marker would also be unsafe: deleting [99] from an unsupported sentence leaves the unsupported sentence intact.

### Final behavior

conversation/grounding.py validates the answer as a whole.

If the model violates the grounding contract, the generated answer is rejected and replaced with a deterministic answer composed from citable claims/evidence.

Invariant:

> Completeness may degrade. Grounding may not.

### Failure B — prompt evidence lost source co-origin

A flat list of evidence markers hid whether two chunks came from the same video or two different videos.

The prompt now groups evidence by source.

### Failure C — evidence budget favored redundant metadata

BM25 length normalization could rank short title/caption chunks above a longer ASR chunk containing the actual answer. A fixed hydration prefix then spent the evidence budget on redundant metadata.

The fix defers duplicate metadata from one source behind distinct evidence.

### Failure D — qualifying is not the same as provably qualifying

A structured executor could correctly qualify an entity based on a price claim, while the current CitationSet did not contain that claim. The renderer could then say “2 results match” while omitting the uncitable price line.

The final renderer only presents a match as qualifying when every required claim-derived constraint has citable satisfying support. Withheld executor matches remain in diagnostics.

### Failure E — RetrievalChunk was confused with EvidenceUnit

The real query “手抓饼多少钱” retrieved the right chunk. The chunk held the answer and linked 56 evidence units, but CitationBuilder projected it to one arbitrary unit, which happened to contain unrelated speech.

The fix made chunk-to-evidence projection plural and deterministic:

- evidence ordered by timestamp/id;
- identify strongest consecutive query-overlap run;
- extend a bounded context window;
- mint citations per EvidenceUnit;
- reuse ordinals when the same evidence appears again.

Invariant:

> A retrieval context can be many citation units. Never invent a single “primary evidence” when the data model says the relationship is plural.

Primary commits:

- e6dd0ba82e35
- 52208685c821
- 2f6be242e21d
- e6afd9f8d173

Primary tests:

- tests/test_grounded_answer_contract.py
- tests/test_citable_answer_contract.py
- tests/test_provable_qualification.py
- tests/test_evidence_projection.py

---

## 13. Seed story: long-media ASR reliability is a provider-adapter concern

### Original blocker

Before segmentation, the repository recorded a real long-media source that failed nondeterministically in one full-length WebSocket session:

- one attempt ended with WebSocket 1006 / keepalive ping timeout after only part of the audio/result stream progressed;
- another sent the full audio but ended with upstream code 45000081, “Timeout waiting next packet”.

The media itself was healthy and ffmpeg decoded it through EOF. Shorter controlled probes succeeded. The correct conclusion was therefore not “Doubao has an 841-second hard limit”; the evidence only justified that one long provider session was not reliable enough for this pipeline.

### Production segmentation at b08

The latest real validation at:

~~~text
b08efb04f975641b41c0a368eba0a35bf88245bd
~~~

used:

~~~text
source: src_a0e0631c136e141dff7008
prepared WAV: 841.721 s

production segmentation:
0: core 0–300 / coverage 0–301
1: core 300–600 / coverage 299–601
2: core 600–841.721 / coverage 599–841.721
~~~

Real result:

~~~text
all 3 Doubao sessions succeeded
retries = 0
151 TranscriptSegments
4734 transcript chars
timestamps 0.680–837.590 s
no timestamp reset
no out-of-range timestamps
~~~

This is enough to mark three claims as **REAL VALIDATED**:

1. segmented provider sessions solve the observed long-session reliability blocker for this real source;
2. restoring provider-local timestamps onto the original media timeline works in the production segmentation path;
3. the bounded-session architecture is operationally sound enough to continue Stage 3D validation.

It is **not** evidence that cross-boundary transcript reconciliation is solved.

### The midpoint-only reconciliation hypothesis failed real validation

The implementation at b08 uses overlapping coverage and assigns each returned utterance to one segment according to the midpoint of that utterance relative to non-overlapping core intervals.

Unit tests established useful arithmetic properties:

- core intervals tile the media;
- coverage intervals overlap only for context;
- a timestamp has one temporal owner under the midpoint rule;
- provider-local timestamps can be shifted back to the original media timeline.

Those properties are true, but the real run exposed a stronger problem that the tests did not model.

Around the 300-second boundary:

~~~text
295.640–300.880
299.320–308.400

both repeat: “到了十分价钱一分货”
~~~

Around the 600-second boundary:

~~~text
593.190–600.830
599.080–602.600

both repeat: “家网红的猫头鹰”
~~~

The adjacent provider sessions did not return the same speech with the same utterance boundaries. The recognizer segmented the shared underlying audio differently in each session.

Therefore:

> audio overlap != ASR utterance-boundary overlap

A temporal midpoint rule can give every **returned utterance** one owner, but it cannot guarantee that two differently bounded utterances are not two renderings of the same underlying speech.

This disproves the previous retrospective wording that “midpoint ownership deduplicates”.

### Current status

~~~text
segmented sessions             REAL VALIDATED
global timestamp restoration   REAL VALIDATED
bounded-session architecture   REAL VALIDATED
midpoint-only reconciliation   BLOCKED / insufficient
downstream EvidenceUnit run    NOT RUN
~~~

The downstream EvidenceUnit run was deliberately not continued after this validation because the transcript still contained cross-boundary duplicate speech. Persisting that output would turn a known reconciliation defect into durable evidence.

### Post-baseline engineering update: conservative boundary stitching (CODE + TEST EVIDENCE, NOT REAL VALIDATED)

The b08 run remains the last real source-level validation recorded above: provider sessions and timestamps succeeded, midpoint-only reconciliation failed, and the downstream EvidenceUnit run was **NOT RUN**.

Since that run, commits `4142ae8` and `638585f` added `_stitch_boundaries` after midpoint ownership, and **both commits are now ancestors of `integration/v1` HEAD `e1af20e`**. Thus boundary stitching is **CODE INTEGRATED + UNIT/CI TESTED**, but remains a regression-tested hypothesis about provider output quality, **not** proof of a post-fix real E2E result. No subsequent live Doubao retranscription or downstream EvidenceUnit validation is established in the reviewed repository evidence.

The rule is deliberately narrow. It proposes a stitch only for two utterances from **adjacent provider sessions** that intersect in time, are near the shared boundary (the current backstop is 5,000 ms), and have a sufficiently long exact normalized suffix/prefix overlap (minimum 7 characters, chosen because the real 600 s duplicate is seven characters). Punctuation and spacing are removed for comparison, with a map from normalized characters back to raw-text offsets so deduplication does not erase unrelated original characters. If an utterance participates in more than one possible match, **no stitch is made**.

Unlike whole-utterance earlier-wins/later-wins, an accepted stitch retains both sides' unique speech, removes only the repeated right-side prefix, and uses the union of their time intervals. Candidate pairing is decided before emission; otherwise a right-side utterance that sorts first could be output twice, both alone and as part of a merged pair. These are **design/code statements**, not provider-quality guarantees.

The new `backend/tests/unit/test_doubao_boundary_stitching.py` encodes both observed real boundary pairs at ~300 s and ~600 s and checks that the repeated phrase occurs once while unique text on each side survives. It also exercises non-stitch/ambiguity properties. Real boundary **fixtures** are valuable regression evidence, but do not turn this into **REAL VALIDATED**: until a fresh provider run and downstream validation confirm quality, cross-boundary reconciliation remains **PENDING REAL VALIDATION**.

The underlying invariant learned from the failure is unchanged:

> audio overlap != ASR utterance-boundary overlap

The overlap/core arithmetic is deterministic. ASR utterance boundaries are not. The safe tradeoff is asymmetric: a missed stitch leaves a visible duplicate; an incorrect stitch silently deletes speech and damages evidence.

Evidence:

- `fix/stage3d-boundary-stitching` origin, now integrated via `638585f` ancestry into `integration/v1@e1af20e`: `backend/src/douyin_knowledge/ai/adapters/doubao_adapter.py`, `_reconcile`, `_stitch_boundaries`, `_stitch_candidates`;
- `backend/tests/unit/test_doubao_boundary_stitching.py` — real-failure-shaped unit fixtures and guard tests;
- b08efb04f975641b41c0a368eba0a35bf88245bd — original real-run evidence, **not** post-fix validation.

### Failure semantics that remain valid

Each provider segment still has a bounded retry budget with deterministic backoff.

If one segment exhausts retries, the whole transcription must fail rather than return a silently incomplete long-media transcript.

Returning 11 minutes of a 14-minute video as though it were complete would create invisible gaps in the knowledge base, which is worse than a visible processing failure.

### Security detail

Transport exception objects may carry requests containing X-Api-Key. Failure context therefore whitelists known-safe attributes instead of serializing exception/request objects.

### Accepted invariants at this validation point

1. Long media may be split into bounded provider sessions without exposing segmentation to downstream interfaces.
2. Provider-local timestamps must be restored to the original media timeline before any durable evidence is written.
3. A bounded session policy is an operational reliability control, not a claim about a documented provider duration limit.
4. Partial long-media transcription must never masquerade as complete.
5. Retry is bounded and reserved for transient classes.
6. Diagnostics must preserve useful provider codes without leaking secrets.
7. Cross-boundary duplicate removal is **not yet an accepted invariant**; midpoint-only ownership is insufficient on real ASR output.
8. No downstream EvidenceUnit should be persisted from a validation run once transcript reconciliation is known to be incorrect.

Evidence:

- b08efb04f975641b41c0a368eba0a35bf88245bd
- backend/src/douyin_knowledge/ai/adapters/doubao_adapter.py
- backend/tests/unit/test_doubao_long_media_asr.py
- real Stage 3D validation of src_a0e0631c136e141dff7008


---


## 13A. Confirmed V1 product decisions — decision record only

These are now confirmed product decisions. This section intentionally does **not** describe them as implemented or historically validated yet. Implementation status and engineering history should be added only when code/commit/real-run evidence exists.

### Capture Scope

V1 should support:

~~~text
default 收藏 → 视频
one named collection
multiple named collections
~~~

This expands the product requirement beyond the currently documented Stage 3 named-collection validation path. Do not infer from this decision that the default 收藏 path is already supported by the current DTK integration.

### Daily incremental sync

V1 product behavior:

~~~text
default local schedule: 03:00
missed run: catch up on next application startup
manual sync: always available
~~~

This subsection records the original product decision. Stage 3F subsequently implemented the scheduling workflow, and [PR #3](https://github.com/guoohhh/douyin-knowledge/pull/3) **merged into `integration/v1` on 2026-10-12**. See §13D for implementation, CI evidence and remaining real-operation limits. Merging into integration is not equivalent to shipping on `main`, nor to real Douyin scheduling E2E.

### Provider configuration

V1 should allow the user to choose independently:

~~~text
main AI provider / model
ASR provider / model
~~~

Security requirement:

> Secret keys must never be echoed to the user or logged as plaintext.

The current architecture already has provider-role separation and secret-aware configuration patterns, but this subsection records only the newly confirmed V1 product decision. Detailed implementation history should be added later from code and tests.

---

## 13B. Stage 3E Capture Scope: selection is not observation and observation is not knowledge

**Updated integration status (2026-10-13):** [PR #2](https://github.com/guoohhh/douyin-knowledge/pull/2) **MERGED on 2026-10-12** into `integration/v1` at merge SHA `1c494b7ade015de031acb898ff8bb2fdf2f447bf`, with Stage 3E feature head `80870062859af90eb360a9f12b14f4887b6a5067`. This supersedes the earlier “Draft / not merged” account for `88739d8`. Stage 3E is CODE INTEGRATED and CI TESTED; the PR's stated verification used fixtures and paid no AI calls. Its merge alone does **not** establish real E2E or multi-identity isolation. Separately, the coordinator **accepted the Codex Stage 3E-6 full real E2E report** after an isolated test-account/browser run; see the explicitly qualified evidence ledger below. The remaining unverified areas are second-page pagination, multiple named folders, full multi-identity cross-checks, and long-term operation.

### Problem and architectural boundary

V1 has to observe (a) default `收藏 → 视频`, (b) one named folder, or (c) several named folders, without treating them as mutually exclusive or treating a user's choice of capture targets as a Processing Policy rule. A source can appear in multiple targets. A target disappearing from the selection is not proof that the source has been deleted or that past evidence has become invalid.

Three lifecycles must remain distinct:

1. **Selection / Capture Scope:** which upstream lists the user currently asks us to observe.
2. **Observation / Membership:** which sources appeared in which list at a successful observation time.
3. **Knowledge / Processing Policy:** whether and how an observed source is processed and made eligible for answers.

Only the first is directly edited by the user choosing capture targets. A deselection must not implicitly delete Source, observations, EvidenceUnits, Claims, or Processing Policy decisions. This matters to Personal External Memory: stopping future observation must not silently destroy one's past memory.

### First implementation and why default favorites is not a fake Collection

Commit `e5ee8e9` introduced a non-secret, versioned `app_settings` entry (`capture.scope.douyin`) with:

~~~yaml
schema_version: 1
platform: douyin
default_favorites: false
named_collection_ids: []
~~~

The list is normalized, deduplicated and nonempty-ID validated. The default target is **off unless selected**. For legacy databases, initial selection is inferred once from existing local named Collection rows; an explicit saved empty selection must stay empty. Migrations create observation tables but do not infer user intent.

Default favorites is **not** a named folder. It uses a separate provider operation `list_default_favorite_sources` (the Stage 3E adapter targets `GET /api/v1/douyin/user/bookmarks` through a separately validated DTK fork), and separate `SourceDefaultFavoriteObservation` / `DefaultFavoritesSyncState` tables. Named folders retain their `SourceCollectionMembership` history. The same video is one `Source` under multiple observations, rather than copies per target. Sidecar session identity/credentials remain outside this non-secret selection setting.

**Historical distinction:** merely wiring this fork route was not proof of live compatibility at the original PR review point. A later Stage 3E-6 isolated account test was **reported and accepted** (§13B evidence below), while its original local artifacts remain unarchived. This newer evidence does not erase the original DTK limitation in §14.1.

### Durable target jobs and the prune invariant

`SYNC_CAPTURE_SCOPE` fans out `SYNC_DEFAULT_FAVORITES` and a `SYNC_NAMED_COLLECTION` for each selected named ID. Each target has its own dedupe key, attempts, failure history and completion status. The fan-out itself does not fetch provider content. A target handler reloads Capture Scope at execution time: if deselected before starting, it skips without a provider call. Once a page walk starts it is completed coherently rather than being interrupted by mid-walk selection changes.

This retains the older foundational safety property:

> A missing item is evidence of absence **only after a complete, authoritative walk of that target**.

On partial pagination / a broken cursor, already-seen sources can be upserted but unseen memberships are not marked absent. The named target's `last_synced_at_ms` and the default target's `last_completed_at_ms` advance only on completed walks. Deselecting a target simply stops new observation.

The older `SYNC_COLLECTIONS` and `SYNC_COLLECTION_SOURCES` job names remain compatibility handlers. A single-target sync also preserves `auto_process` and the API accepts the frontend's `collection_id` alias for `collection_external_id`; migration cannot quietly break already shipped clients.

### CI-exposed fresh-database regression

The initial `e5ee8e9` PR checks had **Backend CI failure** and **Integration Smoke failure**, while Frontend CI passed. The follow-up `e50e871` commit explicitly repaired *fresh fixture Capture Scope initialization*.

Why that case matters: a fresh fixture database has no previously captured Collection rows. Bootstrapping selection **only** from local persisted collections therefore produces an empty scope; a subsequent demo sync fans out to no targets. This can look correct in a previously populated development DB while breaking a clean installation.

The repair `initialize_capture_scope` distinguishes three cases:

- existing captured Collection rows: use those identities first;
- fresh **fixture** DB with no selection or captured collections: ask the configured **local fixture provider** for its named IDs, honoring `DK_CAPTURE_FIXTURE_DIR`;
- fresh **real Douyin** DB: persist an empty selection without upstream reads or guessing which targets the user wants.

Once an explicit setting exists, even if empty, it is never silently replenished. Legacy fixture IDs are not sent to a real sidecar connection. Tests cover fresh packaged fixture, configured fixture directory, existing-row precedence, explicit empty scope, fresh real Douyin without provider reads, and CLI demo sync from a clean DB.

**Evidence strength:** the commit and tests identify the initialization mismatch; the CI failure-to-green sequence independently shows regression and repair at the workflow level. This is not evidence that the real-account default favorites endpoint worked.

### Discovery is an optional read-only projection, not a selection rewrite

Commit `065376f` added `capture/discovery.py`, `GET /api/sources/capture-scope/targets`, and scriptable `dk capture scope|discover|set-default|set-collections`.

A discovery request reads upstream **named-target metadata**, not target contents, and joins it with saved scope and local last-sync observations. It preserves selected but currently undiscoverable target IDs rather than erasing them. Discovery failures return `discovery.state=error` with a safe error code **alongside the saved scope and local history**; they are not disguised as a legitimate empty upstream list. CLI discovery exits nonzero on such a failure. Editing selection does not discover, sync, or touch Processing Policy.

The endpoint's `default_favorites.available` is a configured-provider capability indication, **not a successful runtime sidecar probe**. It must not be described as live availability validation.

A small but important late fix hardens named-folder discovery pagination: `has_more=true` without a cursor now raises instead of returning the first page as if complete.

### Frontend operational semantics: saving a preference is not running a job (f80ed33, 33c10b0)

**Problem.** The backend had separate discovery, selection, sync and policy concepts, but the original Collections page still exposed a direct `api.sync({auto_process: true})` action. That bypassed the new user-facing scope workflow and silently opted into potentially paid processing. Also, a successful enqueue HTTP response could be mistaken for completed sync, and the queue's dedupe behavior means a second request with different `auto_process` does **not** necessarily change an already queued/running job.

**Implemented.** `f80ed33` introduced a Settings-page Capture Scope editor backed by typed `GET/PUT /api/sources/capture-scope`, read-only `GET /capture-scope/targets` and `POST /api/sources/sync`. It keeps a separate saved state and editable draft, blocks Sync when there are unsaved edits or no selected target, preserves saved IDs missing from discovery, and displays upstream discovery errors without treating them as empty collections. `PUT` saves selection only; a distinct manual button requests sync. Default `autoProcess=false` is deliberate: an explicit opt-in is needed before *new* sync jobs can trigger Processing Policy and potentially paid AI. `CaptureScopeSection` sequences saved-scope GET before target discovery to avoid racing first-read fixture bootstrap on the same unique settings key.

**Follow-up correction.** `33c10b0` removed the old Collections-page direct sync button and routed users to `/settings#capture-scope-title`. It also changed the displayed acceptance semantics: `JobAccepted.created=true` means a new job was accepted, not completed; `created=false` means an existing job was reused, and the newly submitted `auto_process` flag does not mutate the old job's options. An existing job may still perform paid processing even if the user's *new* request has the checkbox off. This is a **queue contract**, not just copywriting.

**Tests and limits.** `frontend/scripts/test-capture-scope.ts` checks saved/draft helpers, missing discovered IDs, mock HTTP contract, default `auto_process=false`, new-vs-reused acceptance messages and a source-text assertion that the Collections page no longer calls sync. Frontend CI runs this Node/esbuild script, typecheck and build; integration smoke probes a live **local fixture API**. Those **mock/CI checks alone** did not establish browser-level interaction quality, authenticated Douyin sync or paid-provider cost behavior. Later, the accepted Codex Stage 3E-6 report describes actual browser and test-account workflows (separate evidence level below). The remaining risk is the user interpreting a new checkbox as cancellation of an already-running job; the UI warns but does not implement cancellation/reconfiguration.

**Invariant:** Persisting intent, submitting a task, reusing a task and completing work are four distinct state transitions. UI wording must not collapse them.

### Identity pinning: prevent cross-account provenance corruption (88739d8)

**Problem.** A sidecar can hold more than one identity. Before `88739d8`, `DouyinCaptureProvider._get` only attached `identity` when configured; without one, DTK could choose a different identity from its pool. This was especially dangerous once Stage 3E added several account-scoped read paths (named collection discovery, collection items, default favorites, single-video fetch). Correct-looking imported data from the *wrong account* would violate provenance, not merely confidentiality.

**Repair.** The shared `_get` path now raises `AuthenticationRequired` for absent/whitespace `DK_DOUYIN_SIDECAR_IDENTITY` **before any account-content HTTP request**, and forces the configured identity into the outgoing query even if a caller supplies a conflicting `identity` parameter. A bad pinned identity is not retried unpinned. `/readyz` remains available for sidecar health checks without an identity. Missing identity does **not** make local `GET/PUT` saved Capture Scope inaccessible: target discovery returns an explicit `authentication_required` error and retains saved/local targets. Target sync jobs fail before writing Sources, Collections or processing jobs.

**Why centralizing matters.** The previous default-favorites-specific check protected only that method. Moving the guard to the transport entry point makes it cover all account-scoped reads, including future ones that reuse `_get`. This is a defense-in-depth contract for *account-scoped content*, not a blanket rule that health probes require authentication.

**Evidence.** `backend/src/douyin_knowledge/capture/douyin_provider.py`; `backend/tests/unit/test_douyin_identity_safety.py` parametrizes all four read operations for missing/blank pins and checks zero HTTP requests, forced pin on every request, non-overridable identity, health-only unpinned readiness, and no fallback on invalid pins. `tests/unit/test_capture_scope.py`, `test_capture_discovery.py` and `test_api.py` check job-level fail-closed behavior and preservation of local settings/history. This is **CODE IMPLEMENTED + UNIT/CI TESTED**; it is **not REAL VALIDATED** against a live multi-identity sidecar, nor proof that the configured identity itself belongs to the intended human account.

**Invariant:** Every imported Douyin fact must be attributable to the explicitly configured identity. If account provenance cannot be established, content capture must fail closed while local selection/history remain inspectable.

### Stage 3E-6: accepted full real-account and browser E2E report (not independently archived)

**Evidence provenance and limit:** **REAL E2E REPORTED BY CODEX · ACCEPTED BY PROJECT COORDINATOR · ORIGINAL LOCAL ARTIFACTS NOT INDEPENDENTLY ARCHIVED.** The coordinator supplied the accepted Stage 3E-6 Full Real E2E Report after Codex ran against an **isolated DTK instance**, a **real test Douyin account**, an **application database**, and a **real browser**. This maintainer did **not** reproduce the run or inspect its original local logs, browser recordings or database. The counts below are *report evidence*, not direct GitHub-archived measurements.

~~~text
real account listing:         default favorites 16; one named collection 16
overlap:                      16 (same items observed from both targets)
final Sources:                16
default-favorite observations:16
named memberships:           16
duplicate Sources:            0
duplicate memberships:        0
duplicate observation keys:   0
Collections:                  1
Processing Jobs:              0
final application Jobs:       3 succeeded
SQLite integrity_check:      ok
foreign-key violations:       0
~~~

The browser report covers discovery, default/named selection, save/reload, manual sync, deselection/reselection, repeat sync, offline discovery retaining saved settings, blocking sync with unsaved edits, AI default-off, and correct new-job versus reused-job messaging. The 0 Processing Jobs count is consistent with the no-paid-AI intent; it does **not** validate downstream processing quality. The tests did **not** modify Douyin account content, shared DTK, or the Stage 3D corpus, and performed no paid AI calls.

**Upstream contract observation:** default favorites `count=50` produced `business_error`; `count=20` returned 16 items with `has_more=false`. Named collection `count=50` returned 16 with `has_more=false`. This drove `80870062859af90eb360a9f12b14f4887b6a5067` and its regression test. The report supports a **real one-page full-walk + persistence/overlap + browser workflow** for this account, **not** default favorites' real second page or broad-corpus behavior. Original HTTP/SQL/browser artifacts still need durable archiving for independent reproduction.

**Still UNKNOWN / NOT REAL VALIDATED:** multiple distinct named collections selected and synchronized together; actual second-page default-favorites cursor behavior; cross-account identity selection tested against multiple live identities; multi-day/long-running reliability. These are targeted next tests, not reasons to relabel the completed Stage 3E-6 run as “not tested”.

### Evidence and validation ledger

| Item | Status | Source |
| --- | --- | --- |
| Persisted selection, observation tables, per-target queue, deselection behavior | CODE IMPLEMENTED | e5ee8e9; scope.py; sync.py; handlers.py; migration 0006 |
| Fresh fixture bootstrap / explicit empty / real-mode separation | CODE + TEST PROVEN | e50e871; test_capture_scope.py; test_cli.py |
| Read-only target discovery and API/CLI projection | CODE + TEST PROVEN | 065376f; discovery.py; test_capture_discovery.py; test_api.py |
| Explicit frontend selection/save/sync, safe default, job-reuse wording | CODE + MOCK/CI TESTED; not browser E2E | f80ed33, 33c10b0; CaptureScopeSection.tsx, captureScope.ts, test-capture-scope.ts |
| Required pinned identity on all account-content reads | CODE + MOCK/CI TESTED; not live multi-identity E2E | 88739d8; douyin_provider.py; test_douyin_identity_safety.py |
| Latest feature-branch CI at 88739d8 | CI PASS (2026-10-12 01:52 China time; GitHub completion 2026-10-11 17:52 UTC) | Python 3.10, Python 3.12, lint/types, frontend build, local-API integration smoke |
| Default favorites + named overlap, one-page full sync, real browser workflow | **REAL E2E REPORTED BY CODEX; ACCEPTED BY PROJECT COORDINATOR; ORIGINAL LOCAL ARTIFACTS NOT INDEPENDENTLY ARCHIVED** | Stage 3E-6 accepted report: 16/16 overlap → 16 Sources, 3 jobs succeeded, DB checks clean; not a self-run |
| Second page / multiple named folders / live multi-identity / long-term reliability | **NOT REAL VALIDATED** | Explicit Stage 3E-6 coverage limits |
| Daily 03:00 scheduling, missed-run catch-up on startup | V1 DECIDED; NOT established implemented here | §13A, not this feature's implementation |
| User-facing main AI / ASR model selection and key management | V1 DECIDED; not established shipped here | §13A |

PR #2 reports 76 focused tests and 887 backend tests passing (4 skipped) at an earlier local validation point; these figures are historical, **not** the current head's total. The five GitHub check runs for historical head `88739d8` are independently all green. A frontend client exercised against a live **local fixture API** in smoke CI is **not**, by itself, proof of a live Douyin provider call or real browser E2E; the later **separately reported** Stage 3E-6 real-run results above have a different, explicitly labeled provenance.

Owning modules: `capture/scope.py`, `capture/discovery.py`, `capture/douyin_provider.py`, `capture/pagination.py`, `capture/sync.py`, `jobs/handlers.py`, `api/routes/sources.py`, `cli/commands_capture.py`; migration `0006_capture_scope_observations.py`; frontend `src/pages/CaptureScopeSection.tsx`, `src/pages/CollectionsPage.tsx`, `src/lib/captureScope.ts`, `src/api/client.ts`. Main tests: `tests/unit/test_capture_scope.py`, `tests/unit/test_capture_discovery.py`, `tests/unit/test_douyin_identity_safety.py`, `tests/test_api.py`, `tests/test_cli.py`, frontend `scripts/test-capture-scope.ts`.

### Tradeoffs and interview story

- A one-time bootstrap from **existing local observations** protects upgrade behavior, but a new database has no observations. Fresh fixture and fresh real account require intentionally different defaults.
- A target's saved selection and the provider's live discovery may diverge; surfacing both is more truthful than letting a transient upstream failure erase a preference.
- The system deliberately preserves stopped targets' historical observations and knowledge. It does **not** automatically interpret deselection as “forget my content.”
- An authenticated **one-page complete walk and persistence for the 16-item test account** are now reported in accepted Stage 3E-6 E2E. Independent artifact preservation, real second-page pagination, additional named folders and long-term stability remain risks; CI alone could not have established the account behavior.
- UI safety includes cost intent: a default-off checkbox cannot change an already deduplicated job. Distinguish submitted, reused and completed work, and consider explicit cancellation/status polling later.
- Identity pinning is part of provenance correctness. Local mock tests establish fail-closed transport behavior; real multi-identity sidecar validation remains outstanding.

**Interview version:** “Adding a multi-source import setting forced us to separate user intent, historical membership and knowledge eligibility. Our first version passed in populated databases but broke clean fixture installation. We fixed one-time bootstrap semantics, made target work independently retryable and guarded every absence claim on a complete provider walk. The UI then exposed another class of correctness bugs: saving a selection, accepting/reusing a job and completing a sync were different facts, while account-scoped reads had to be pinned to one identity to prevent plausible but wrong-account data.”

---

## 13C. Small but important Stage 3D/3D4 outcome semantics (CODE + TEST)

Two later fixes on the real-sidecar line are separate from the successful b08 segmented-session experiment. They are **not** proof that boundary stitching passed new real validation.

1. **Reserved logging fields can turn successful entity creation into a failure.** Commit `ddfbbb91` changed structured log `extra={"name": ...}` to `extra={"entity_name": ...}` because `name` is a reserved Python `LogRecord` attribute. The regression test enables INFO logging while creating a real `Entity`. Lesson: observability code can participate in business-path correctness, and a test with a quiet logger may miss a production logging exception.
2. **“ASR returned successfully” does not imply “usable timestamped transcript exists.”** Commit `423dee12` records `asr_empty_result` when a successful provider response has neither usable segments nor nonblank full text; `asr_no_usable_segments` when full text exists but no usable timestamped segments can be persisted. A thrown provider exception stays `asr_failed`. The corresponding tests assert `achieved_level=1` rather than falsely claiming L2 for a successful-but-empty response, no fabricated ASR EvidenceUnits, and a persisted job event carrying the run id and notes. The overall processing run may still be `succeeded` at L1; **run execution status, requested level and achieved knowledge level are different facts**.

Modules/tests: `extraction/entity_resolver.py`, `tests/test_extraction.py`; `extraction/orchestrator.py`, `tests/unit/test_media_asr_closes_loop.py`. Both are CODE + TEST observations; live reproduction circumstances, if any, need separate run artifacts.

---

## 13D. Stage 3E pagination and Stage 3F daily sync — real-contract observation, then durable local scheduling

### What changed, and what the evidence does not yet show

- `80870062859af90eb360a9f12b14f4887b6a5067` closes a specific default-favorites page-size failure, as reported in commit evidence from real upstream probing.
- `ba8371304ef181bc36d75e5a5c2928ace6121557` is the isolated Stage 3F implementation commit on `feature/stage3f-daily-sync`. [PR #3](https://github.com/guoohhh/douyin-knowledge/pull/3) **MERGED on 2026-10-12** into `integration/v1`, merge SHA `e1af20e5f839ec069d4a06adb42aa790bc876dd9`. Its original PR description still refers to PR #2 being unmerged, but that text is now historical; the actual merge order was PR #2 followed by PR #3. The new integration HEAD does not change the feature's live-validation evidence level.
- All five Stage 3F head check runs completed successfully: Python 3.10, Python 3.12, lint/type checking, frontend build and local-API integration smoke. Neither passing CI nor a live local API proves the daily scheduler operated against a real Douyin account. The PR explicitly states Stage 3F tests used local fixtures, without paid AI or real Douyin calls.

### Default favorites page size: REST acceptance != business acceptance

**Observed problem (accepted Codex Stage 3E-6 report, original artifacts not independently archived):** the sidecar's REST schema allowed up to `count=50`, but the real upstream default-favorites request at 50 returned `business_error`. At `count=20`, the default-favorites page returned **16 items / `has_more=false`**. The named collection at `count=50` returned **16 items / `has_more=false`**. The accepted E2E report also records overlap/persistence (16 unique Sources), three succeeded application jobs and a real browser exercise (§13B). Thus this is **REAL E2E REPORTED BY CODEX / ACCEPTED BY PROJECT COORDINATOR**, **not** a maintainer-reproduced run or independently archived raw request/response. The single-page data do not validate a real second-page walk.

**Fix / invariant:** `DouyinCaptureProvider.list_default_favorite_sources` now clamps to its own `MAX_DEFAULT_FAVORITES_PAGE_SIZE=20`, not the general 50. `backend/tests/unit/test_douyin_provider_transport.py` asserts a request for 50 sends 20 and a smaller request remains smaller. A cursor belongs to the paging query that generated it: changing count mid-walk would undermine the complete-walk/pruning guarantee.

**Engineering lesson:** an HTTP/OpenAPI maximum is a transport/interface constraint, not necessarily the real upstream business limit. A successful first page also says nothing about safely completing a multi-page walk and reconciling observed absences.

### Daily scheduler is durable intent, not a second ingestion pipeline

**Product problem:** V1's requirement is scheduled local 03:00 capture, startup catch-up, and manual sync, without silently starting paid AI processing. A naïve fixed 24-hour timer ignores calendar days and DST; an in-memory timer misses sessions while the machine sleeps; spawning another capture pipeline duplicates logic and retry semantics.

**Implemented design at `ba83713`:**

1. `capture/daily_sync.py` stores enabled state, HH:MM, IANA timezone, effective-after moment, last due slot, last attempt, root/target job ids and last known outcome in one `AppSetting` (`capture.daily_sync`). Defaults are **disabled / 03:00 local time**. The continually running worker checks roughly every 30 seconds *between jobs*; on restart it can enqueue the latest overdue slot once. It cannot run while the worker/machine is off.
2. Time uses calendar days in the configured zone, not UTC+24 hours. Spring-forward nonexistent times are mapped onto a valid post-transition instant; fall-back ambiguity uses a single occurrence. Changing/enabling a schedule starts with the next future slot; it does not replay all earlier missed days.
3. `tick` schedules the existing `SYNC_CAPTURE_SCOPE` with `auto_process=false`; if another capture job is queued/running, it defers rather than reuse that job's possibly paid `auto_process=true` payload. Empty Capture Scope records `skipped_empty_scope` without contacting Douyin.
4. Checkpointing uses the existing SQLite transaction plus `BEGIN IMMEDIATE` to serialize competing schedule checks. Once attempted, a due slot is consumed **even if its job fails**; the user can choose manual retry rather than the scheduler hammering a failing account every tick.
5. A root job succeeding means it **fanned out** target jobs, not that remote content was synchronized. `record_targets` and `refresh_outcome` track per-target terminal states, distinguishing queued/running, success, failed, no_targets and completed_with_skips. This preserves the Stage 3E truthfulness contract at the new scheduler layer.
6. `GET/PUT /api/admin/daily-sync` and `frontend/src/pages/DailySyncSection.tsx` expose explicit enable/save/timezone/outcome controls. The UI states that worker shutdown/sleep suspends execution and that automatic capture cannot trigger paid processing.

**Test evidence:** `backend/tests/unit/test_daily_sync.py` covers disabled defaults, catch-up/deduplication, active manual-job deferral, empty-scope no-request, terminal outcomes, scope changes, calendar-day/DST behavior, SQLite dual-tick races, restart persistence, fixture sync without processing, missing identity, and bounded target failure. Frontend settings and client contracts have corresponding tests. The local implementation report states **933 passed, 4 skipped** (reported; not an independently rerun count). The post-merge `integration/v1@e1af20e` GitHub workflows were independently checked as **SUCCESS**: [Backend](https://github.com/guoohhh/douyin-knowledge/actions/runs/38280275793), [Frontend](https://github.com/guoohhh/douyin-knowledge/actions/runs/38280275887), [Integration](https://github.com/guoohhh/douyin-knowledge/actions/runs/38280276038). This establishes **CODE IMPLEMENTED / UNIT TESTED / CI VALIDATED**; **REAL SCHEDULED E2E NOT TESTED**, particularly real-account cross-day runs.

### Why SourceSnapshot fingerprinting was revised

Repeated captures can differ only in volatile signed URLs, observed fetch timestamps, or engagement counts, while the underlying content/meaning has not changed. If every raw JSON variation generates a durable `SourceSnapshot`, daily sync turns ephemeral transport churn into fake historical content revisions.

Stage 3F changes `CaptureSyncService._payload_hash` to fingerprint selected meaningful metadata and normalized URL identity (scheme/host/path without query or fragment). The full raw provider payload is preserved when a *meaningful* new snapshot is emitted; current signed URLs are refreshed on Source/Asset even when no new snapshot is added. A targeted test varies signatures, fetch time and counts (expect no new snapshot) and then varies caption (expect a new snapshot).

**Comparison evidence (different datasets; do not calculate an improvement rate):** an earlier Stage 3E real-account state was reported as **16 Sources / 128 SourceSnapshots** under the old behavior. The new Stage 3F fingerprint was checked in a **simulation with 8 Sources / 8 Snapshots**, and a repeated simulated sync still had **8 Snapshots**. These are **REPORTED PRIOR REAL-ACCOUNT STATE** vs **SIMULATED POST-FIX BEHAVIOR**, **not** a before/after measurement of the same 16-source real corpus. The repo unit test independently pins the signed-URL/fetch-time invariants but does not prove the eight-source simulation was a real Douyin run.

**Tradeoff/P2:** CDN host/path changes can still create noise; post-fix fingerprint behavior on the real 16-source corpus remains **NOT MEASURED**. Conversely, deliberately excluding transient engagement counts means those metrics do not create immutable revision points. This distinction belongs in the product's versioning contract, not only in a hash implementation.

### Invariants, status and interview use

- Capture scheduling cannot silently opt into paid processing.
- A due slot is durable, claimed at most once in normal operation, and never inferred complete from root fan-out alone.
- Restarts catch up the **latest** overdue day; neither unlimited backlog replay nor execution with the worker shut down is promised.
- A successful provider page is not proof of a complete authoritative target walk; only complete walks may reconcile absence.
- Snapshot versions represent meaningful source changes, not every signed URL or observation timestamp.

**Evidence ledger:** Stage 3E `8087006` = real upstream one-page 50/20 failure/correction **REPORTED BY CODEX + ACCEPTED** (not independently archived) + fix CODE/TESTED; Stage 3E [PR #2](https://github.com/guoohhh/douyin-knowledge/pull/2) = **MERGED** into `integration/v1` at `1c494b7`; Stage 3F `ba83713` = **CODE IMPLEMENTED + UNIT TESTED + CI VALIDATED**, [PR #3](https://github.com/guoohhh/douyin-knowledge/pull/3) **MERGED** into `integration/v1` at `e1af20e5`, **REAL SCHEDULED E2E NOT TESTED**. See `docs/DECISIONS.md` DEC-022, `backend/src/douyin_knowledge/capture/daily_sync.py`, `backend/src/douyin_knowledge/jobs/worker.py`, `backend/tests/unit/test_daily_sync.py`, and `backend/tests/unit/test_capture_snapshot_fingerprint.py`.

**Interview story:** “An automation feature is not a timer. We had to define what a daily slot means across time zones/restarts, prevent a background operation from inheriting a paid-processing request, and make success mean completed target work rather than queued fan-out. Once capture became daily, we also had to stop signed URL churn from fabricating immutable knowledge history.”

---

## 14. Topics with enough code evidence but incomplete historical evidence

### 14.1 Default/all-favorites API limitation

The original b08 capture code was built around:

- /{platform}/user/collections
- /{platform}/collection/posts with an explicit collection_id

The provider comments also document that the folder-list endpoint is session-scoped and takes no author.

This supported the original **named collection** capture path. Stage 3E later added a separate default-favorites operation targeting a DTK fork. PR #2's implementation tests used fixtures, **but the subsequently accepted Codex Stage 3E-6 report describes real default-favorites + named one-page capture/persistence on the isolated test account** (§13B). The original upstream limitation and later fork capability are **different historical periods and evidence states**; preserve both rather than presenting the original limitation as the current tested-account capability.

However, the repository at b08 does **not** contain the original probe/log that established that the deployed DTK version could not expose the user's default/all-favorites feed through the required API.

Therefore the retrospective should currently state:

> **Historical limitation at the original b08-era DTK integration:** the validated path used named collections, with the exact upstream absence probe not archived. **Later Stage 3E-6 on the isolated DTK fork:** default favorites + named one-page synchronization is REAL E2E REPORTED BY CODEX, accepted by the coordinator, without independently archived original artifacts; see §13B.

Backfill needed:

- DTK version;
- endpoint(s) tested;
- request/response or OpenAPI excerpt;
- whether limitation was sidecar API surface, Douyin upstream behavior or authentication scope;
- resulting decision to use a named test folder.

### 14.2 SQLAlchemy 2.1 dependency drift

What the repository proves:

- commit a588e565b1ff changed SQLAlchemy from >=2.0.36 to >=2.0.36,<2.1;
- current pyproject keeps that bound.

What the repository does not prove:

- exact failing SQLAlchemy 2.1 version;
- traceback;
- which API/behavior changed;
- why adapting code was rejected in favor of the 2.0-series support bound.

Do not invent this root cause later. Recover it from CI logs, terminal history or the original agent conversation if possible.

### 14.3 Real-account Stage 3B E2E

The repository contains credential-gated live sidecar integration tests and real-provider adapters, but it does not yet contain a durable ledger of every real-account E2E run.

The retrospective should eventually record for each accepted real validation:

~~~text
date
branch/SHA
source id (redacted if necessary)
media type/duration
provider/model
pipeline command
result counts
evidence timestamps checked
retrieval query
citation checked against source
latency/cost if available
failure notes
~~~

### 14.4 Why Doubao and DeepSeek were selected

The code proves how they are integrated, not the complete product/economic decision that selected them.

Backfill:

- alternatives considered;
- cost/latency/quality assumptions at the time;
- whether provider choice was temporary for validation or intended default;
- exact model versions/resource ids used in real tests.

---

## 15. Current known tradeoffs / P2 backlog that belongs in the retrospective

These are not all bugs. They are places where the current design knowingly buys simplicity.

- Structured Retrieval considers at most MAX_CANDIDATES=500 and pre-cap candidate order can matter.
- Structured ranking is source-level, not entity-evidence-pair-level.
- text_requirements and several QueryPlan fields are recorded but not live ranking/execution inputs.
- Entity extraction heuristics miss some plausible names.
- KnowledgeItem has schema but no writer.
- Wiki documents are indexed but HybridRetriever does not retrieve Wiki pages.
- Alias resolution lacks a dedicated test.
- Wiki conflict handling lacks confidence weighting/user override.
- Failed processing runs roll back partial derived output; retries can redo paid work. True checkpoint/resume is not implemented.
- Media retention policy is coarse rather than per-collection.
- The exact SQLAlchemy 2.1 incompatibility is not retained in repository history.
- Stage 3D full-corpus robustness is ongoing, so corpus-wide failure rates are not yet frozen.
- Stage 3E default-favorites + one named collection **were** exercised in the accepted Codex Stage 3E-6 real-account E2E report (16/16 overlap; 16 Sources); original artifacts are not independently archived. A second default-favorites page, multiple named folders and broader corpus behavior remain unverified.
- Stage 3E browser selection/save/sync/reuse/error workflows were **reported real-E2E tested** by Codex and accepted, but the original browser artifacts are not archived; real **multiple-identity cross-account provenance** remains unverified, and mock CI cannot establish it.
- Reused queued/running sync jobs keep their original AI processing option; UI warns but does not cancel or reconfigure the existing job.
- Stage 3E Capture Scope discovery capability is not a live provider-health proof; display saved selection and provider discovery status independently.
- Snapshot evidence spans unlike samples: pre-Stage 3F reported real state **16 Sources / 128 Snapshots**, versus simulated new-fingerprint **8 Sources / 8 Snapshots** remaining eight after repeated simulation sync. The new fingerprint has **not** been measured against the same real 16-source corpus.
- Stage 3F 03:00 local scheduled sync and restart catch-up are merged into `integration/v1` with CODE/UNIT/CI evidence, but lack real-account operation validation; user-facing provider/model configuration remains separate V1 work.

Source: docs/DECISIONS.md “Known gaps” plus current code at the baseline SHA.

---

## 16. Missing historical information to recover

The following questions are worth answering from old conversations, terminal logs, CI artifacts or future reproducible probes. They should not be filled from memory without a label.

| Missing history | Why it matters |
| --- | --- |
| Exact default/all-favorites DTK probe | Explains why named collections became a product constraint rather than an arbitrary implementation choice |
| Stage 3A first successful real sync command/output | Establishes first true boundary crossing from mock/contract tests to account data |
| Real collection/source/asset counts at each accepted SHA | Lets us distinguish bugs caused by data shape from bugs caused by scale |
| SQLAlchemy 2.1 traceback and resolved version | Turns a dependency pin into a real dependency-drift story |
| First three accepted Doubao E2E examples | Proves media→ASR→Evidence beyond unit tests |
| DeepSeek real structured-extraction examples | Lets us evaluate schema adherence, grounding and failure modes |
| Provider cost/latency observations | Important for architecture/operational tradeoffs |
| Rejected ASR/model alternatives | Needed for a complete “why this vendor” explanation |
| Stage 3C real prompt/answer/citation artifacts | Makes grounding stories reproducible rather than commit-message-only |
| Full-corpus Stage 3D statistics | Needed before declaring robustness complete |
| Archived original Stage 3E-6 real E2E artifacts (HTTP/SQL/browser/identity) | Codex report of 16 default + 16 named / 16 Sources is accepted, but raw local evidence is not independently archived; essential for reproducibility |
| Stage 3E real default-favorites second page + multiple named folders | Accepted account E2E covered only 16 items on one page and one named folder; assess safe cursor walking and overlap across >1 named ID |
| Post-Stage 3F real-corpus SourceSnapshot measurement | Earlier real 16/128 and simulated new 8/8 are not a same-corpus before/after comparison |
| Stage 3E real multi-identity sidecar proof | Confirms pinning against actual DTK identity-pool behavior and prevents wrong-account ingestion |
| Stage 3E-6 browser recordings and extended behavior cases | Selection/save/reload/reuse/offline/AI-off flows are accepted Codex real E2E **report evidence**; archive browser artifacts and separately test actual multi-identity and multiple named folders |
| Stage 3F daily scheduler real operation | Merged `e1af20e`; unit and integrated CI pass, but cross-day real-account/worker-restart target outcomes remain REAL SCHEDULED E2E NOT TESTED |
| Any manual DB repair commands used during Phase 3 | Important for migration/recovery story and future runbook |
| CI run URLs for milestone SHAs | Independent evidence that a state was green |

---

## 17. Initial interview-story backlog

These are promising interview stories; they are not yet polished scripts.

### Story A — “Fixtures passed, the external contract was still wrong”

Talk about sidecar cursor shape, task states, fake health check, incomplete-walk pruning, and why contract-derived tests differ from client-derived fixtures.

### Story B — “A schema allowed multiple assets, but the algorithm still assumed one”

Talk about real image albums, per-item replacement, authoritative-set reconciliation, reactivation semantics and the 99-asset database.

### Story C — “A citation foreign key did not mean the claim was grounded”

Talk about ClaimEvidence vs semantic support, Chinese ASR normalization, numeric grounding, reject/downgrade and one shared eligibility predicate.

### Story D — “Retrieval was correct; the answer disappeared during citation projection”

Talk about a chunk linked to 56 evidence units, arbitrary single evidence projection, multi-evidence windows and the distinction between retrieval granularity and citation granularity.

### Story E — “Long audio required separating transport reliability from transcript reconciliation”

Talk about the original nondeterministic long-session failures, successful bounded-session validation on the real 841.721 s source, correct global timestamp restoration, and the later discovery that temporal midpoint ownership was insufficient because adjacent ASR sessions chose different utterance boundaries for the same speech. The key lesson is that solving provider-session reliability did not automatically solve semantic cross-boundary reconciliation.

### Story F — “Using two coding agents as an engineering experiment”

Talk about frozen independent implementations, data-migration cost vs vertical-slice completeness, integration by invariant rather than merge, and adversarial review.

---

## 18. Appendix A — first-pass code map

### Capture / real Douyin

- backend/src/douyin_knowledge/capture/douyin_provider.py
- backend/src/douyin_knowledge/capture/pagination.py
- backend/src/douyin_knowledge/capture/sync.py
- backend/src/douyin_knowledge/capture/registry.py
- backend/tests/integration/test_douyin_provider_integration.py

### Processing Policy

- backend/src/douyin_knowledge/policy/evaluator.py
- backend/src/douyin_knowledge/policy/reconciler.py
- backend/src/douyin_knowledge/policy/signal.py
- backend/src/douyin_knowledge/policy/triage.py
- backend/src/douyin_knowledge/knowledge/eligibility.py

### Media / ASR

- backend/src/douyin_knowledge/media/store.py
- backend/src/douyin_knowledge/media/downloader.py
- backend/src/douyin_knowledge/media/service.py
- backend/src/douyin_knowledge/ai/adapters/doubao_adapter.py

### Extraction / grounding

- backend/src/douyin_knowledge/extraction/orchestrator.py
- backend/src/douyin_knowledge/extraction/entity_extractor.py
- backend/src/douyin_knowledge/extraction/claim_extractor.py
- backend/src/douyin_knowledge/extraction/entity_resolver.py
- backend/src/douyin_knowledge/extraction/grounding.py
- backend/src/douyin_knowledge/ai/adapters/deepseek_adapter.py

### Retrieval

- backend/src/douyin_knowledge/retrieval/query_plan.py
- backend/src/douyin_knowledge/retrieval/query_parser.py
- backend/src/douyin_knowledge/retrieval/structured.py
- backend/src/douyin_knowledge/retrieval/retriever.py
- backend/src/douyin_knowledge/retrieval/keyword_search.py
- backend/src/douyin_knowledge/retrieval/vector_store.py

### Answer / citations

- backend/src/douyin_knowledge/conversation/conversation_manager.py
- backend/src/douyin_knowledge/conversation/citation_builder.py
- backend/src/douyin_knowledge/conversation/grounding.py
- backend/src/douyin_knowledge/conversation/answer_generator.py

### Wiki

- backend/src/douyin_knowledge/wiki/builder.py
- backend/src/douyin_knowledge/wiki/composer.py
- backend/src/douyin_knowledge/wiki/updater.py

### Core persistence

- backend/src/douyin_knowledge/db/models/capture.py
- backend/src/douyin_knowledge/db/models/processing.py
- backend/src/douyin_knowledge/db/models/entities.py
- backend/src/douyin_knowledge/db/models/userstate.py
- backend/src/douyin_knowledge/db/models/wiki.py

---

## 19. Appendix B — milestone commit map

This list is deliberately selective. It maps engineering stories, not every commit.

- ee98fc7 — implementation handoff baseline complete
- ecfd784 — frozen GPT V1 final
- 5a8fba6 — frozen Claude V1 final / self-audit fixes
- f54ee22 — real sidecar contract + safe pruning
- 16604f9 — local media acquisition closes real ASR path
- 9612a28 — reversible/retroactive Processing Policy
- 1b5fcd0 — Claim grounding validation
- e8e1f71 — shared knowledge eligibility
- 8139daa — Structured Retrieval vertical slice
- 48a324b / 9be3760 / 62b4f25 — structured/provenance/refusal hardening
- f5fd038 — pinned Douyin identity in capture
- f2306b5 — set-based multi-asset reconciliation
- 23acb6f — asset reactivation + unchanged-payload reconciliation
- a588e56 — SQLAlchemy supported-series pin
- 2dc9d3a — Doubao Seed-ASR provider
- 3fee8ef — DeepSeek provider
- e6dd0ba — whole-answer grounding + source grouping + evidence budget
- 5220868 — deterministic fallback only renders citable knowledge
- 2f6be24 — provable structured qualification
- e6afd9f — multi-evidence citation projection
- b08efb0 — bounded segmented long-media ASR
- 4142ae8 / 638585f — conservative boundary stitching and order independence (CODE + TEST; not real validated)
- ddfbbb9 — reserved LogRecord field guard during entity creation
- 423dee1 — distinguish successful empty ASR from failed and usable evidence
- e5ee8e9 — persistent Stage 3E Capture Scope and durable per-target sync
- e50e871 — repair fresh fixture scope bootstrap after CI regression
- 065376f — read-only target discovery API/CLI and pagination safety
- f80ed33 — Capture Scope settings UI, explicit save/sync, mock frontend contract coverage
- 33c10b0 — remove implicit paid-processing sync path; clarify reused job semantics
- 88739d8 — fail-closed identity pinning for all Douyin account-content reads
- 8087006 — default-favorites page-size cap from reported real upstream 50/20 probe
- 1c494b7 — merge Stage 3E PR #2 into integration/v1 (2026-10-12)
- ba83713 — Stage 3F daily scheduler, durable due/checkpoint/outcomes, meaningful source fingerprint
- e1af20e — Stage 3F PR #3 merged into integration/v1 (2026-10-12); not live scheduled E2E

---

## 20. Next retrospective work

The next pass should not broaden randomly. Recommended order:

1. fully reconstruct Stage 3A: sidecar identity, named collection constraint, first real sync, asset reconciliation and SQLAlchemy drift;
2. fully reconstruct Stage 3B: Doubao + DeepSeek + real media/extraction E2E;
3. fully reconstruct Stage 3C as one end-to-end “retrieval was not enough; provenance had to survive every projection” story;
4. finish Stage 3D only after full-corpus robustness reaches a stable milestone;
5. then backfill Processing Policy, Wiki and Structured Retrieval chapters from the already-strong integration history;
6. finally derive polished resume bullets and interview narratives from the factual chapters, never the reverse.

