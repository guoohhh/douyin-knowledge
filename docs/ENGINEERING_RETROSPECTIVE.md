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

The Phase 3 branch diverged from integration/v1 at 62b4f25656ed4e33452d534804d0c7c5afacbf7b and currently reaches b08efb04f975641b41c0a368eba0a35bf88245bd.

The current working stages are:

- Stage 3A — real metadata synchronization
- Stage 3B — real media → Doubao ASR → Evidence
- Stage 3C — real retrieval → Answer → Citation
- Stage 3D — full-corpus robustness, currently ongoing at this baseline

The Draft PR is #1, “Phase 3: validate real Douyin sidecar integration”.

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

Since that run, branch `fix/stage3d-boundary-stitching` adds `_stitch_boundaries` after midpoint ownership. This is a **new implementation and a regression-tested hypothesis**, not proof of an accepted real E2E outcome. No subsequent live Doubao retranscription or EvidenceUnit validation is established by the code inspected here.

The rule is deliberately narrow. It proposes a stitch only for two utterances from **adjacent provider sessions** that intersect in time, are near the shared boundary (the current backstop is 5,000 ms), and have a sufficiently long exact normalized suffix/prefix overlap (minimum 7 characters, chosen because the real 600 s duplicate is seven characters). Punctuation and spacing are removed for comparison, with a map from normalized characters back to raw-text offsets so deduplication does not erase unrelated original characters. If an utterance participates in more than one possible match, **no stitch is made**.

Unlike whole-utterance earlier-wins/later-wins, an accepted stitch retains both sides' unique speech, removes only the repeated right-side prefix, and uses the union of their time intervals. Candidate pairing is decided before emission; otherwise a right-side utterance that sorts first could be output twice, both alone and as part of a merged pair. These are **design/code statements**, not provider-quality guarantees.

The new `backend/tests/unit/test_doubao_boundary_stitching.py` encodes both observed real boundary pairs at ~300 s and ~600 s and checks that the repeated phrase occurs once while unique text on each side survives. It also exercises non-stitch/ambiguity properties. Real boundary **fixtures** are valuable regression evidence, but do not turn this into **REAL VALIDATED**: until a fresh provider run and downstream validation confirm quality, cross-boundary reconciliation remains **PENDING REAL VALIDATION**.

The underlying invariant learned from the failure is unchanged:

> audio overlap != ASR utterance-boundary overlap

The overlap/core arithmetic is deterministic. ASR utterance boundaries are not. The safe tradeoff is asymmetric: a missed stitch leaves a visible duplicate; an incorrect stitch silently deletes speech and damages evidence.

Evidence:

- `fix/stage3d-boundary-stitching`: `backend/src/douyin_knowledge/ai/adapters/doubao_adapter.py`, `_reconcile`, `_stitch_boundaries`, `_stitch_candidates`;
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

This is a product scheduling contract, not yet an implementation-history claim in this retrospective.

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

## 14. Topics with enough code evidence but incomplete historical evidence

### 14.1 Default/all-favorites API limitation

Current capture code is built around:

- /{platform}/user/collections
- /{platform}/collection/posts with an explicit collection_id

The provider comments also document that the folder-list endpoint is session-scoped and takes no author.

This supports the current **named collection** capture path.

However, the repository at b08 does **not** contain the original probe/log that established that the deployed DTK version could not expose the user's default/all-favorites feed through the required API.

Therefore the retrospective should currently state:

> Operationally known limitation: the validated Stage 3 path uses named collections. The historical experiment proving the default/all-favorites API gap needs a retained artifact before we describe its exact upstream behavior.

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

---

## 20. Next retrospective work

The next pass should not broaden randomly. Recommended order:

1. fully reconstruct Stage 3A: sidecar identity, named collection constraint, first real sync, asset reconciliation and SQLAlchemy drift;
2. fully reconstruct Stage 3B: Doubao + DeepSeek + real media/extraction E2E;
3. fully reconstruct Stage 3C as one end-to-end “retrieval was not enough; provenance had to survive every projection” story;
4. finish Stage 3D only after full-corpus robustness reaches a stable milestone;
5. then backfill Processing Policy, Wiki and Structured Retrieval chapters from the already-strong integration history;
6. finally derive polished resume bullets and interview narratives from the factual chapters, never the reverse.

