# Douyin Knowledge — Processing Policy

Status: Living document
Phase: Design, plus an as-built record in section 14A
Version: 0.2
Last updated: 2026-09-18

---

## 1. Purpose

Not every saved Douyin video should become knowledge.

Users may save movie clips, variety-show fragments, music, memes, entertainment clips, or other content simply because they want to watch it later. Running ASR, OCR, vision, extraction, embeddings, and knowledge indexing on those items would waste compute, storage, and attention.

Douyin Knowledge therefore needs a first-class **Processing Policy** layer that decides whether a synchronized source should enter the knowledge-processing pipeline.

Core principle:

> Capture broadly, process selectively, and keep every decision reversible.

---

## 2. Default Meaning of “Skip”

In V1, `skip` should normally mean:

```text
Douyin 收藏
↓
Metadata Sync
↓
Policy Decision = metadata_only / excluded
↓
STOP
```

The system still stores a lightweight `Source` record containing enough metadata to identify the item and remember why it was skipped.

It should **not** by default:

- download full video;
- run ASR;
- run OCR;
- extract keyframes;
- call multimodal understanding;
- generate KnowledgeItem / Claims / Entities;
- create vector embeddings;
- include the item in normal knowledge retrieval.

This is preferable to completely ignoring the item because it:

1. prevents repeated rediscovery/reprocessing on every sync;
2. allows the user to reverse the rule later;
3. preserves original collection membership and source identity;
4. provides transparent statistics about what was skipped.

---

## 3. Policy Actions

V1 should support at least these actions.

### `process`

Normal adaptive knowledge-processing pipeline.

```text
metadata → triage → ASR/OCR/Vision as needed → extraction → indexing
```

### `metadata_only`

Store source metadata but stop before knowledge processing.

This is the default action for user-excluded entertainment/watch-later content.

### `always_process`

Explicit override forcing a source or matching rule into processing even if a broader skip rule would otherwise match.

Useful for exceptions.

### Future optional action: `archive_only`

Could preserve/download media without knowledge processing, but this is **not required for V1**. Douyin Knowledge is not primarily a media archiver.

---

## 4. Where Policy Evaluation Happens

Policy evaluation should happen in two stages because some rules can be resolved without AI while others require cheap classification.

```text
COLLECTION SYNC
    ↓
SOURCE METADATA
    ↓
POLICY PASS A — deterministic metadata rules
    │
    ├── excluded → metadata_only → STOP
    │
    └── undecided/process
            ↓
       CHEAP TRIAGE
            ↓
POLICY PASS B — cheap semantic/classification rules
    │
    ├── excluded → metadata_only → STOP
    │
    └── process
            ↓
       ADAPTIVE AI PIPELINE
```

This ensures that creator- or collection-based exclusions can avoid even cheap AI calls, while semantic exclusions such as “movie/variety clips” can still be detected before expensive processing.

---

## 5. Rule Types

### 5.1 Per-source override

The user can explicitly mark one item:

```text
Do not process this item
```

or:

```text
Always process this item
```

This is the most specific rule.

---

### 5.2 Creator rules

Example:

```text
All videos from creator X → metadata_only
```

This directly supports the requirement:

> “某个博主的视频不走流程。”

Creator identity should prefer a stable platform creator ID rather than display name alone.

---

### 5.3 Original Douyin collection rules

Example:

```text
收藏夹「待看影视」 → metadata_only
```

This is useful when the user already separates entertainment/watch-later content inside Douyin.

Original platform collection names remain user metadata, not canonical AI taxonomy.

---

### 5.4 Content domain / form rules

Examples:

```text
domain = entertainment → metadata_only
form = clip → metadata_only
```

or more specifically:

```text
entertainment + movie_clip → metadata_only
entertainment + variety_clip → metadata_only
```

These rules require cheap classification before they can be applied.

The classifier should be allowed to return an exclusion-oriented semantic label such as:

```text
movie_clip
variety_clip
music_clip
meme
sports_highlight
other_entertainment
```

These labels are routing hints, not the canonical knowledge taxonomy.

---

### 5.5 Keyword / hashtag rules

Examples:

```text
title contains "电影片段" → metadata_only
hashtag contains "综艺" → metadata_only
```

Useful for simple deterministic filters, but these should not replace semantic classification.

---

### 5.6 Future rule dimensions

Potential later additions:

- duration range;
- source language;
- media type;
- explicit user rating/status;
- platform source;
- combinations of conditions.

Do not overbuild the rule engine for V1.

---

## 6. Rule Precedence

Rules need deterministic precedence so users can understand why something was skipped.

Recommended order:

```text
1. Per-source explicit override
2. always_process rule
3. specific creator / collection rule
4. semantic type/domain/form exclusion rule
5. keyword/metadata rule
6. default = process
```

The system should preserve the winning rule and reason.

Example:

```yaml
processing_policy:
  action: metadata_only
  matched_rule_id: rule_creator_12
  reason: "creator excluded by user"
  evaluated_at: 2026-09-17T19:00:00+08:00
```

### Important principle

A user should never have to guess why an item was not processed.

---

## 7. Reversibility

Skipping must not be destructive.

If the user later deletes or changes a rule:

```text
metadata_only source
↓
policy reevaluation
↓
process
↓
enter normal AI pipeline
```

A source can therefore transition between:

```text
pending
process
metadata_only
processing
ready
```

without losing source identity.

---

## 8. Example: Movie / Variety Clips

User preference:

```text
I often save movie and variety-show clips just to watch later.
Do not turn them into knowledge.
```

Possible rules:

```yaml
rules:
  - id: skip_movie_clips
    condition:
      semantic_type: movie_clip
    action: metadata_only

  - id: skip_variety_clips
    condition:
      semantic_type: variety_clip
    action: metadata_only
```

A newly synchronized source then flows as:

```text
metadata
↓
cheap classifier
↓
semantic_type = variety_clip (0.94)
↓
matched skip_variety_clips
↓
metadata_only
```

No full transcription, OCR, visual reasoning, or embedding is required.

---

## 9. Example: Creator Exclusion with Exception

User rule:

```text
Creator A → do not process
```

Later, one particularly useful video from Creator A should be included.

Rules:

```text
creator A → metadata_only
specific video 123 → always_process
```

The per-source override wins.

---

## 10. User Experience

The feature should be easy to configure without exposing a complex enterprise-style rules engine.

Possible V1 Settings area:

```text
Processing Rules

Never process:
- Creators
- Douyin collections
- Content types
- Keywords

Exceptions / Always process
```

On a source card or detail page, actions may include:

```text
Skip this video
Always process this video
Never process videos from this creator
Do not process this kind of content
```

When the user chooses “this kind of content”, the UI should show the inferred category before creating a broad rule.

---

## 11. AI-suggested Rules

The system may later notice repeated manual skips and suggest, but should not silently create broad exclusions.

Example:

```text
You skipped 8 recent videos classified as variety clips.
Would you like future variety clips to stay metadata-only?
```

User confirmation is required before a persistent broad rule is created.

This protects against accidental loss of useful knowledge.

---

## 12. Retrieval Behavior for Excluded Sources

By default, `metadata_only` sources should **not** appear in normal AI knowledge answers because their content was never actually understood.

However, basic metadata search may still allow queries such as:

```text
Show me videos I skipped from creator X
Show my unprocessed entertainment saves
```

If the user explicitly asks the AI about an excluded source, the system should explain that the item has not been processed and may offer to process that item once.

An explicit one-time process request should not automatically delete the user's broad exclusion rule.

---

## 13. Historical Import Behavior

Processing Policy must apply before bulk historical enrichment.

Recommended historical import order:

```text
1. sync all metadata
2. evaluate deterministic rules
3. cheap-classify unresolved sources when needed
4. mark excluded sources metadata_only
5. prioritize AI processing only for remaining sources
```

This can substantially reduce first-import cost for users with large entertainment-heavy collections.

---

## 14. Minimal V1 Data Model

Conceptually, store rules separately from sources.

### `ProcessingRule`

Possible fields:

```text
rule_id
name
enabled
action
condition_type
condition_value
priority
created_by       # user / system_suggestion
created_at
updated_at
```

### Source policy state

Possible fields:

```text
policy_action
matched_rule_id
policy_reason
policy_evaluated_at
```

The design above is the target. Section 14A records what was actually built; where the two disagree, the code is authoritative.

---

## 14A. As Built

The policy layer is implemented and wired: every source that comes out of a sync is evaluated before any processing job runs, and every evaluation writes a row, including ones that resolve to the default. Read this section rather than inferring the schema from the sketch above.

**Rules** live in `processing_rules` (`db/models/policy.py:ProcessingRule`) and carry `rule_type`, `action`, `priority`, one of `target_source_id` / `target_creator_id` / `target_collection_id`, and a JSON `matcher_json`.

Rule types are `source`, `creator`, `collection`, `metadata`, `semantic`. Actions are `process`, `metadata_only`, `always_process`, `exclude`.

**Targets are internal ids, never display names.** A rule excluding a creator stores `cre_...`, not the creator's handle. Douyin display names change, and a rule keyed on a name would silently stop applying the moment someone renamed themselves — the user would see previously-excluded content start being processed with nothing to explain why.

**Precedence** is resolved in application logic, not SQL (PP-004): source rules beat creator rules beat collection rules beat metadata rules, and within a type, higher `priority` wins. See `policy/evaluator.py:_rule_matches` and the grouping above it.

**Metadata matchers** support `title_contains`, `title_not_contains`, `keywords`, `hashtags`, `source_type`, `min_duration_ms`, `max_duration_ms`, `creator_name_contains`. Present keys are ANDed; within `title_contains` every needle must appear, while `keywords` matches if *any* needle does — the two exist because "skip clips of this show" and "skip anything mentioning any of these" are different requests. An empty matcher matches nothing rather than everything — a rule saved with no conditions is a half-finished edit, and reading it as "match all" would let one stray `exclude` rule switch off the whole pipeline (`policy/evaluator.py:_matcher_matches`).

**Matchers read an assembled `SourceSignal`, not a single column.** `policy/signal.py` gathers title, caption, hashtags, duration, source type, creator name and statistics — hashtags coming from the latest `SourceSnapshot.raw_json`, since they are captured but never persisted as a column. Before this, the documented `hashtags` matcher could not fire at all. A `hashtags` needle matches either the structured tag list or a literal `#tag` in the text, because captions do not reliably repeat their own tags (DEC-016).

**`semantic` rules match on a cached content-type label** produced by cheap triage (`policy/triage.py`, cached in `source_triage`). Matchers take `content_types` — any of `movie_clip`, `variety_clip`, `music_clip`, `meme`, `sports_highlight`, `other_entertainment`, `knowledge` — plus an optional `min_confidence` (default `0.6`). `unknown` is rejected at write time with a 422, along with unrecognised labels: a typo would otherwise be stored, listed, shown as enabled, and never match, so the user would believe they were skipping variety clips while paying to process every one.

Pass B runs **only** when pass A reached no verdict *and* an enabled semantic rule exists, so a library with no semantic rules never classifies anything. Classification is deterministic cue matching; it escalates to one structured model call only when cues are inconclusive and `DK_ENABLE_TRIAGE_MODEL_FALLBACK` is on (off by default, and never under the `mock` provider). A label of `unknown` never satisfies a matcher, so a classifier miss cannot silently drop a source. See DEC-016.

**Decisions** land in `policy_decisions` (`db/models/policy.py:PolicyDecision`) with `phase` (`metadata` / `semantic` / `manual_override`), `action`, `reason_code`, `explanation_json`, the `rule_id` that matched, and the `model_name` where a model was involved. This is what makes POL-004 real: `dk policy why <source_id>` and `GET /api/admin/policy/decisions` answer "why was this skipped?" from the audit trail rather than by re-deriving it from the rule table, which would be a different computation and could disagree.

**A forced reprocess still respects an `exclude` rule.** `dk process --force` re-evaluates; the job runs, decides, and records the exclusion without creating a processing run, so `current_processing_run_id` stays as it was. Covered by `backend/tests/test_cli.py:test_exclude_rule_blocks_a_forced_reprocess` and the API-level equivalent in `test_api.py`.

**Surfaces**: `GET/POST/DELETE /api/admin/policy/rules`, `GET /api/admin/policy/decisions`, `POST /api/admin/policy/reconcile`; the `triage` block on every source payload and the `?content_type=` list filter; `dk policy list|why|exclude|delete|reconcile|triage`; the Settings page in the frontend lists rules and recent decisions, and a source whose decision was not `process` shows the reason inline.

---

## 15. Confirmed Decisions

### PP-001 — Users can exclude content from knowledge processing

Exclusions can target individual sources, creators, original Douyin collections, semantic content types, and simple metadata rules.

### PP-002 — Skip defaults to `metadata_only`, not complete disappearance

This keeps decisions reversible and prevents repeated rediscovery.

### PP-003 — Processing policy is evaluated before expensive AI work

Creator/collection rules should be able to stop the pipeline before ASR/OCR/Vision.

### PP-004 — Semantic exclusion uses cheap triage, not deep processing

Content such as movie/variety clips may be identified with low-cost classification before the expensive pipeline.

### PP-005 — Explicit user overrides are strongest

Users can always force one item to process or skip.

### PP-006 — Broad exclusion rules must be explainable

The system stores which rule caused the decision.

### PP-007 — The system may suggest rules but must not silently create broad exclusions

Persistent broad rules require user control.

### PP-008 — Excluded sources are not part of normal knowledge retrieval

They can still be found through metadata-oriented views/searches.
