"""Policy evaluator: decide whether to process a source, and at what level.

Evaluation order (PROCESSING_POLICY.md, ARCHITECTURE.md section 18):

1. per-source ``always_process``
2. per-source ``exclude``
3. creator / collection explicit rules
4. metadata rules (matcher over title, caption, hashtags, duration)
5. semantic rules -- deferred, they need triage classification
6. default: process

The evaluator takes the **persisted** ``Source`` row, not the provider DTO. The earlier
version took a ``CapturedSource``, which is why it could never be called from the
processing path: by the time anything decides whether to spend a model call, the source
has been persisted and what exists is a row with internal ids, collection memberships and
snapshot metadata. Matching a rule's ``target_source_id`` (a FK to ``sources.id``) against
a provider's ``external_id`` meant per-source rules could never fire at all.

Every evaluation produces a decision, including the default one. A user asking "why was
this video skipped?" -- or "why was this one processed?" -- must get an answer from the
record rather than from someone reading the rule table by hand (POL-004).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.models.capture import (
    Creator,
    Source,
    SourceCollectionMembership,
)
from douyin_knowledge.policy.models import PolicyAction, PolicyDecision, PolicyPhase, RuleType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from douyin_knowledge.policy.models import ProcessingRule
    from douyin_knowledge.policy.repository import PolicyRepository


# Actions that stop the pipeline before any model call.
_BLOCKING = (PolicyAction.EXCLUDE, PolicyAction.METADATA_ONLY)


class PolicyEvaluator:
    """Decides processing policy for persisted sources."""

    def __init__(self, repository: PolicyRepository, session: Session | None = None) -> None:
        self.repository = repository
        # The repository already holds the session; accepting one is a convenience for
        # callers that construct the evaluator directly.
        self.session = session or repository.session

    # ------------------------------------------------------------------ public

    def evaluate(self, source: Source, *, persist: bool = True) -> PolicyDecision:
        """Decide whether and how to process ``source``.

        The decision is recorded by default. A decision that is computed and thrown away
        is worse than none at all: the pipeline acts on it, so the audit trail would show
        an unexplained skip.
        """
        rules = self.repository.list_rules(enabled_only=True)
        matched = self._find_matching_rule(source, rules)

        if matched is not None:
            action = matched.action
            reason_code = f"{matched.rule_type}_rule_matched"
            explanation: dict[str, Any] = {
                "rule_id": matched.id,
                "rule_name": matched.name,
                "rule_type": str(matched.rule_type),
                "rule_priority": matched.priority,
                "matched_on": self._match_description(source, matched),
            }
            rule_id: str | None = matched.id
        else:
            action = PolicyAction.PROCESS
            reason_code = "default_policy"
            explanation = {
                "note": "no enabled rule matched; the default is to process",
                "rules_considered": len(rules),
            }
            rule_id = None

        decision = PolicyDecision(
            id=new_id("pol"),
            source_id=source.id,
            rule_id=rule_id,
            phase=PolicyPhase.METADATA,
            action=action,
            reason_code=reason_code,
            explanation=explanation,
            created_at_ms=now_ms(),
        )
        return self.repository.record_decision(decision) if persist else decision

    def should_process(self, source: Source, *, persist: bool = True) -> tuple[bool, PolicyDecision]:
        """Convenience for the job handler: `(may spend model calls, why)`."""
        decision = self.evaluate(source, persist=persist)
        return decision.action not in _BLOCKING, decision

    # ----------------------------------------------------------------- matching

    def _find_matching_rule(
        self, source: Source, rules: list[ProcessingRule]
    ) -> ProcessingRule | None:
        """First match wins, in precedence order.

        Per-source rules are checked ahead of everything else regardless of priority: an
        explicit decision about *this* video is a stronger statement of intent than any
        pattern, and a user who excluded one item should not have it silently pulled back
        in by a broad creator rule.
        """
        ordered = sorted(rules, key=lambda r: (-r.priority, r.created_at_ms))

        by_type = {
            RuleType.SOURCE: [r for r in ordered if r.rule_type == RuleType.SOURCE],
            RuleType.CREATOR: [r for r in ordered if r.rule_type == RuleType.CREATOR],
            RuleType.COLLECTION: [r for r in ordered if r.rule_type == RuleType.COLLECTION],
            RuleType.METADATA: [r for r in ordered if r.rule_type == RuleType.METADATA],
        }

        # Within per-source rules, always_process beats exclude: the user who pinned this
        # one item did so to override a broader block.
        source_rules = sorted(
            by_type[RuleType.SOURCE],
            key=lambda r: 0 if r.action == PolicyAction.ALWAYS_PROCESS else 1,
        )

        for group in (
            source_rules,
            by_type[RuleType.CREATOR],
            by_type[RuleType.COLLECTION],
            by_type[RuleType.METADATA],
        ):
            for rule in group:
                if self._rule_matches(source, rule):
                    return rule
        return None

    def _rule_matches(self, source: Source, rule: ProcessingRule) -> bool:
        if rule.rule_type == RuleType.SOURCE:
            return bool(rule.target_source_id) and rule.target_source_id == source.id

        if rule.rule_type == RuleType.CREATOR:
            return bool(rule.target_creator_id) and rule.target_creator_id == source.creator_id

        if rule.rule_type == RuleType.COLLECTION:
            if not rule.target_collection_id:
                return False
            return self._is_member_of(source.id, rule.target_collection_id)

        if rule.rule_type == RuleType.METADATA:
            return self._matcher_matches(source, rule.matcher_json or {})

        # SEMANTIC needs a triage classification that only exists after a model call, so
        # it cannot participate in the pre-processing gate. Returning False keeps such a
        # rule inert rather than accidentally excluding everything.
        return False

    def _is_member_of(self, source_id: str, collection_id: str) -> bool:
        """Only *present* memberships count.

        Membership is soft-removed, so a collection the user has since emptied would
        otherwise keep applying its rule to items no longer in it.
        """
        return (
            self.session.scalar(
                select(SourceCollectionMembership.source_id).where(
                    SourceCollectionMembership.source_id == source_id,
                    SourceCollectionMembership.collection_id == collection_id,
                    SourceCollectionMembership.is_present == 1,
                )
            )
            is not None
        )

    def _matcher_matches(self, source: Source, matcher: dict[str, Any]) -> bool:
        """All present keys must match (AND).

        An empty matcher returns False rather than matching everything: a rule saved with
        no conditions is a half-finished edit, and treating it as "match all" would let a
        stray `exclude` rule silently switch off the entire pipeline.
        """
        if not matcher:
            return False

        haystack = " ".join(
            part for part in (source.title, source.caption_raw) if part
        ).lower()

        for needle in self._as_list(matcher.get("title_contains")):
            if needle.lower() not in haystack:
                return False

        excludes = self._as_list(matcher.get("title_not_contains"))
        if any(needle.lower() in haystack for needle in excludes):
            return False

        hashtags = self._as_list(matcher.get("hashtags"))
        if hashtags and not any(f"#{tag.lstrip('#')}".lower() in haystack for tag in hashtags):
            return False

        if (source_type := matcher.get("source_type")) and source.source_type != source_type:
            return False

        if (min_ms := matcher.get("min_duration_ms")) is not None:
            if source.duration_ms is None or source.duration_ms < int(min_ms):
                return False

        if (max_ms := matcher.get("max_duration_ms")) is not None:
            if source.duration_ms is None or source.duration_ms > int(max_ms):
                return False

        if creator_name := matcher.get("creator_name_contains"):
            name = self._creator_name(source)
            if not name or creator_name.lower() not in name.lower():
                return False

        return True

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]

    def _creator_name(self, source: Source) -> str | None:
        if source.creator is not None:
            return source.creator.display_name
        if not source.creator_id:
            return None
        creator = self.session.get(Creator, source.creator_id)
        return creator.display_name if creator else None

    def _match_description(self, source: Source, rule: ProcessingRule) -> dict[str, Any]:
        """What the UI shows next to "why was this skipped?"."""
        if rule.rule_type == RuleType.SOURCE:
            return {"source_id": source.id}
        if rule.rule_type == RuleType.CREATOR:
            return {"creator_id": source.creator_id, "creator_name": self._creator_name(source)}
        if rule.rule_type == RuleType.COLLECTION:
            return {"collection_id": rule.target_collection_id}
        return {"matcher": rule.matcher_json}


__all__ = ["PolicyEvaluator"]
