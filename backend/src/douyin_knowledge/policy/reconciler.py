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
from douyin_knowledge.policy.models import HIDDEN_ACTIONS, RuleType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from douyin_knowledge.policy.repository import PolicyRepository

logger = get_logger(__name__)

# `HIDDEN_ACTIONS` now lives in `policy.models`, the dependency-free module, so that this
# one can drive a wiki recompile without forming
# reconciler -> wiki.builder -> knowledge.eligibility -> reconciler. It is imported above
# and re-exported in `__all__` because the retriever, indexer and existing tests already
# reach for it at this path.


@dataclass(frozen=True)
class ReconcileSummary:
    """What a reconciliation pass did, for the API, CLI and logs."""

    reevaluated: int = 0
    changed: int = 0
    now_hidden: int = 0
    now_visible: int = 0
    #: Current wiki pages recomposed so they stop presenting a newly hidden source (or
    #: start presenting a newly visible one). Reported because "the rule applied but the
    #: wiki still shows it" is otherwise indistinguishable from a bug.
    wiki_pages_recompiled: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "reevaluated": self.reevaluated,
            "changed": self.changed,
            "now_hidden": self.now_hidden,
            "now_visible": self.now_visible,
            "wiki_pages_recompiled": self.wiki_pages_recompiled,
        }


class PolicyReconciler:
    """Re-applies current policy to sources that already exist."""

    def __init__(
        self,
        session: Session,
        repository: PolicyRepository,
        *,
        evaluator: PolicyEvaluator | None = None,
    ) -> None:
        self.session = session
        self.repository = repository
        # Accepting an evaluator lets callers hand in one built by `policy.factory`, so a
        # reconciliation reaches the same verdict the processing gate would. The default
        # keeps the module usable without settings, and deliberately has no triage model:
        # a whole-corpus reconcile is the last place to start issuing per-source model
        # calls unasked.
        self.evaluator = evaluator or PolicyEvaluator(repository, session)

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
        reevaluated = changed = now_hidden = now_visible = pages_recompiled = 0
        # Only sources whose visibility actually moved need a wiki recompile. Recomposing
        # for every re-evaluated source would rewrite the whole wiki on any rule edit.
        changed_source_ids: list[str] = []

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
                changed_source_ids.append(source_id)
            elif was_hidden and not is_hidden:
                now_visible += 1
                changed_source_ids.append(source_id)

        self.session.flush()
        if changed_source_ids:
            pages_recompiled = self._reproject_wiki(changed_source_ids)
        summary = ReconcileSummary(
            reevaluated=reevaluated,
            changed=changed,
            now_hidden=now_hidden,
            now_visible=now_visible,
            wiki_pages_recompiled=pages_recompiled,
        )
        if changed:
            logger.info("policy reconciled", extra=summary.as_dict())
        return summary

    # ------------------------------------------------------- wiki projection

    def _reproject_wiki(self, source_ids: list[str]) -> int:
        """Recompose the current wiki pages the changed sources could appear on.

        Retrieval and the Knowledge API recompute from the spine on every read, so their
        policy filter bites immediately. The wiki does not: a committed ``WikiRevision``
        keeps its text and its ``WikiSupport`` rows until something recompiles it, so
        without this a newly excluded source stayed on the page as *current* knowledge,
        cited, after it had already vanished from Ask and from entity pages.

        Recompiling is chosen over marking pages stale because the composer already applies
        the eligibility rule, so the correct page is one `build_for_entities` call away,
        whereas a stale flag would need every wiki read path to learn about it and would
        leave the user with a blank page instead of a correct shorter one. Composition is
        deterministic and local -- no model calls -- and the set is narrowed to the pages
        the changed sources actually mention, not the whole wiki.

        History is untouched: `build_for_entities` commits a *new* revision and demotes the
        previous one, so the old text remains in `wiki_revisions` for the audit trail.
        """
        from douyin_knowledge.db.models.entities import EntityMention
        from douyin_knowledge.wiki.builder import WikiBuilder

        entity_ids = list(
            dict.fromkeys(
                row[0]
                for row in self.session.execute(
                    select(EntityMention.resolved_entity_id).where(
                        EntityMention.source_id.in_(source_ids),
                        EntityMention.resolved_entity_id.is_not(None),
                    )
                )
                if row[0]
            )
        )
        if not entity_ids:
            return 0

        stats = WikiBuilder(self.session).build_for_entities(entity_ids)
        recompiled = len([o for o in stats.outcomes if o.page_id])
        logger.info(
            "policy reprojected onto wiki",
            extra={"entities": len(entity_ids), "pages": recompiled},
        )
        return recompiled

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
