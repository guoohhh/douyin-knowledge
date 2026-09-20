"""Retroactive policy reconciliation: make a rule change reach sources already processed.

:class:`~douyin_knowledge.policy.evaluator.PolicyEvaluator` is a *gate*. It answers "may I
spend model calls on this source?" at the moment work is about to happen, which is the
right question for the pipeline and the wrong one for the library. Once a source has been
processed, the gate has nothing left to stop: the claims exist, the wiki cites them, and
retrieval answers from them. Excluding that source afterwards had no observable effect --
``SourceProcessingState.current_policy_action`` and ``current_policy_decision_id`` existed
in the schema from the first migration and nothing read or wrote either one (DEC-015).

This module closes that gap by projecting the evaluator's verdict onto the one mutable row
per source, which retrieval, indexing and wiki composition then filter on. Two properties
matter more than the mechanism:

*Exclusion is a visibility change, not a delete.* Historical ``ProcessingRun``,
``EvidenceUnit`` and ``Claim`` rows are never touched, so the run pointer still names the
last successful run and reversal is a matter of re-evaluating, not re-processing. This is
the "disappearance is not deletion" rule (AGENTS s4) applied to the user's own policy
rather than to the platform's availability.

*Reconciliation is narrow by default.* A per-source rule reconciles one source; only a
matcher-based rule, which by construction has no target id, walks the corpus. Re-evaluating
every source on every rule edit would be correct and would also make the policy UI
unusable on a large library.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.capture import Source, SourceCollectionMembership
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.observability import get_logger
from douyin_knowledge.policy.evaluator import PolicyEvaluator
from douyin_knowledge.policy.models import PolicyAction, RuleType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from douyin_knowledge.policy.repository import PolicyRepository

logger = get_logger(__name__)

#: Actions under which a source's knowledge must not reach normal retrieval, the wiki, or
#: current answers. ``metadata_only`` is deliberately *not* here: it caps how deep
#: extraction goes, and whatever was legitimately extracted stays citable.
HIDDEN_ACTIONS = frozenset({PolicyAction.EXCLUDE.value})


@dataclass(frozen=True)
class ReconcileSummary:
    """What a reconciliation pass did, for the API, CLI and logs."""

    reevaluated: int = 0
    changed: int = 0
    now_hidden: int = 0
    now_visible: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "reevaluated": self.reevaluated,
            "changed": self.changed,
            "now_hidden": self.now_hidden,
            "now_visible": self.now_visible,
        }


class PolicyReconciler:
    """Re-applies current policy to sources that already exist."""

    def __init__(self, session: Session, repository: PolicyRepository) -> None:
        self.session = session
        self.repository = repository
        self.evaluator = PolicyEvaluator(repository, session)

    # ------------------------------------------------------------------ public

    def affected_source_ids(self, rule_id: str) -> list[str]:
        """Which sources could a change to this rule possibly alter?

        Deliberately computed from the rule's *target*, not from its action: disabling an
        exclude rule affects exactly the sources it used to name. Callers that are about to
        delete a rule must call this **first**, because afterwards the target is gone.

        A rule id that no longer resolves returns every source: the only way a caller sees
        that is by asking after a delete, and in that case a full re-evaluation is correct
        rather than merely safe.
        """
        rule = self.repository.get_rule(rule_id)
        if rule is None:
            return self._all_source_ids()

        if rule.rule_type == RuleType.SOURCE:
            return [rule.target_source_id] if rule.target_source_id else []

        if rule.rule_type == RuleType.CREATOR:
            if not rule.target_creator_id:
                return []
            return list(
                self.session.scalars(
                    select(Source.id).where(Source.creator_id == rule.target_creator_id)
                )
            )

        if rule.rule_type == RuleType.COLLECTION:
            if not rule.target_collection_id:
                return []
            # Past members are included on purpose: an item removed from the folder must
            # also stop being governed by the folder's rule, and that needs a re-evaluation
            # too. `_is_member_of` in the evaluator is what decides the outcome.
            return list(
                self.session.scalars(
                    select(SourceCollectionMembership.source_id).where(
                        SourceCollectionMembership.collection_id == rule.target_collection_id
                    )
                )
            )

        # METADATA and SEMANTIC rules match on content, so the affected set is unbounded.
        return self._all_source_ids()

    def reconcile_rule(self, rule_id: str) -> ReconcileSummary:
        """Re-evaluate everything a change to ``rule_id`` could have altered."""
        return self.reconcile_sources(self.affected_source_ids(rule_id))

    def reconcile_all(self) -> ReconcileSummary:
        """Re-evaluate the whole corpus. For `dk policy reconcile` and repair."""
        return self.reconcile_sources(self._all_source_ids())

    def reconcile_sources(self, source_ids: list[str]) -> ReconcileSummary:
        """Project current policy onto each source's state row.

        A decision is recorded only when the action actually changes. Appending one every
        pass would bury the answer to "why is this hidden?" under a run of identical
        "still eligible" rows, and every rule edit reconciles more sources than it changes.
        """
        reevaluated = changed = now_hidden = now_visible = 0

        for source_id in dict.fromkeys(source_ids):
            source = self.session.get(Source, source_id)
            if source is None:
                continue
            reevaluated += 1

            state = self._state_for(source_id)
            previous = state.current_policy_action
            decision = self.evaluator.evaluate(source, persist=False)
            action = decision.action.value

            if action == previous:
                continue

            persisted = self.repository.record_decision(decision)
            state.current_policy_action = action
            state.current_policy_decision_id = persisted.id
            state.updated_at_ms = now_ms()
            changed += 1

            was_hidden = previous in HIDDEN_ACTIONS
            is_hidden = action in HIDDEN_ACTIONS
            if is_hidden and not was_hidden:
                now_hidden += 1
            elif was_hidden and not is_hidden:
                now_visible += 1

        self.session.flush()
        summary = ReconcileSummary(
            reevaluated=reevaluated,
            changed=changed,
            now_hidden=now_hidden,
            now_visible=now_visible,
        )
        if changed:
            logger.info("policy reconciled", extra=summary.as_dict())
        return summary

    # ----------------------------------------------------------------- helpers

    def _state_for(self, source_id: str) -> SourceProcessingState:
        """The state row, created if a source has never been processed.

        An unprocessed source still needs a current action: the user can exclude something
        before it is ever looked at, and the pipeline reads this row.
        """
        state = self.session.get(SourceProcessingState, source_id)
        if state is None:
            state = SourceProcessingState(source_id=source_id)
            self.session.add(state)
            self.session.flush()
        return state

    def _all_source_ids(self) -> list[str]:
        return list(
            self.session.scalars(
                select(Source.id).where(Source.locally_deleted_at_ms.is_(None))
            )
        )


__all__ = ["HIDDEN_ACTIONS", "PolicyReconciler", "ReconcileSummary"]
