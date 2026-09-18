"""Policy repository: load and persist processing rules and decisions.

The policy layer speaks in frozen dataclasses (``policy.models``) while storage
speaks in ORM rows (``db.models.policy``). This module is the only place the two
vocabularies meet, so the evaluator can stay a pure function of its inputs and be
tested without a database.

Rules are ordered ``priority DESC, created_at_ms ASC`` in SQL as well as in the
evaluator: the evaluator's own sort is what decides precedence, but returning rows
in the same order keeps "first match wins" reproducible when two rules share a
priority and the caller skips the sort.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.dml import execute_rowcount
from douyin_knowledge.db.models.policy import PolicyDecision as PolicyDecisionRow
from douyin_knowledge.db.models.policy import ProcessingRule as ProcessingRuleRow
from douyin_knowledge.policy.models import (
    PolicyAction,
    PolicyDecision,
    PolicyPhase,
    ProcessingRule,
    RuleType,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def rule_to_domain(row: ProcessingRuleRow) -> ProcessingRule:
    return ProcessingRule(
        id=row.id,
        name=row.name,
        is_enabled=bool(row.is_enabled),
        rule_type=RuleType(row.rule_type),
        action=PolicyAction(row.action),
        priority=row.priority,
        target_source_id=row.target_source_id,
        target_creator_id=row.target_creator_id,
        target_collection_id=row.target_collection_id,
        matcher_json=row.matcher_json,
        origin=row.origin,
        created_at_ms=row.created_at_ms,
        updated_at_ms=row.updated_at_ms,
    )


def decision_to_domain(row: PolicyDecisionRow) -> PolicyDecision:
    return PolicyDecision(
        id=row.id,
        source_id=row.source_id,
        rule_id=row.rule_id,
        phase=PolicyPhase(row.phase),
        action=PolicyAction(row.action),
        reason_code=row.reason_code,
        explanation=row.explanation_json,
        model_name=row.model_name,
        created_at_ms=row.created_at_ms,
    )


class PolicyRepository:
    """Repository for processing rules and policy decisions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ rules

    def list_rules(self, *, enabled_only: bool = True) -> list[ProcessingRule]:
        stmt = select(ProcessingRuleRow).order_by(
            ProcessingRuleRow.priority.desc(), ProcessingRuleRow.created_at_ms
        )
        if enabled_only:
            stmt = stmt.where(ProcessingRuleRow.is_enabled == 1)
        return [rule_to_domain(row) for row in self.session.scalars(stmt)]

    def get_rule(self, rule_id: str) -> ProcessingRule | None:
        row = self.session.get(ProcessingRuleRow, rule_id)
        return rule_to_domain(row) if row is not None else None

    def save_rule(self, rule: ProcessingRule) -> ProcessingRule:
        """Insert or update, keyed on ``rule.id``.

        ``created_at_ms`` is preserved on update even if the caller passed 0: a rule's
        age participates in precedence, so letting a save reset it would silently
        reorder policy.
        """
        row = self.session.get(ProcessingRuleRow, rule.id) if rule.id else None
        stamp = now_ms()

        if row is None:
            # Only pass `id` when the caller actually supplied one: passing None
            # explicitly bypasses the column default and would insert a NULL primary key.
            kwargs: dict[str, object] = {"created_at_ms": rule.created_at_ms or stamp}
            if rule.id:
                kwargs["id"] = rule.id
            row = ProcessingRuleRow(**kwargs)
            self.session.add(row)

        row.name = rule.name
        row.is_enabled = 1 if rule.is_enabled else 0
        row.rule_type = str(rule.rule_type)
        row.action = str(rule.action)
        row.priority = rule.priority
        row.target_source_id = rule.target_source_id
        row.target_creator_id = rule.target_creator_id
        row.target_collection_id = rule.target_collection_id
        row.matcher_json = rule.matcher_json
        row.origin = rule.origin
        row.updated_at_ms = stamp

        self.session.flush()
        return rule_to_domain(row)

    def delete_rule(self, rule_id: str) -> bool:
        row = self.session.get(ProcessingRuleRow, rule_id)
        if row is None:
            return False
        # Decisions carry ON DELETE SET NULL for rule_id, so audit history survives
        # the rule that produced it.
        self.session.delete(row)
        self.session.flush()
        return True

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> ProcessingRule | None:
        row = self.session.get(ProcessingRuleRow, rule_id)
        if row is None:
            return None
        row.is_enabled = 1 if enabled else 0
        row.updated_at_ms = now_ms()
        self.session.flush()
        return rule_to_domain(row)

    # -------------------------------------------------------------- decisions

    def record_decision(self, decision: PolicyDecision) -> PolicyDecision:
        """Append a decision. Decisions are immutable audit rows, never updated.

        ``rule_id`` is dropped when it points at a rule that is not in the database
        (the evaluator can synthesize a default decision with no rule): the column is
        a foreign key, and writing a dangling reference would fail the insert and
        take the whole processing run down with it.
        """
        rule_id = decision.rule_id
        if rule_id is not None and self.session.get(ProcessingRuleRow, rule_id) is None:
            rule_id = None

        row = PolicyDecisionRow(
            source_id=decision.source_id,
            rule_id=rule_id,
            phase=str(decision.phase),
            action=str(decision.action),
            reason_code=decision.reason_code,
            explanation_json=decision.explanation,
            model_name=decision.model_name,
            created_at_ms=decision.created_at_ms or now_ms(),
        )
        self.session.add(row)
        self.session.flush()
        return decision_to_domain(row)

    def get_latest_decision(self, source_id: str) -> PolicyDecision | None:
        row = self.session.scalars(
            select(PolicyDecisionRow)
            .where(PolicyDecisionRow.source_id == source_id)
            .order_by(PolicyDecisionRow.created_at_ms.desc(), PolicyDecisionRow.id.desc())
            .limit(1)
        ).first()
        return decision_to_domain(row) if row is not None else None

    def list_decisions(
        self,
        source_id: str | None = None,
        *,
        limit: int = 100,
    ) -> list[PolicyDecision]:
        stmt = (
            select(PolicyDecisionRow)
            .order_by(PolicyDecisionRow.created_at_ms.desc(), PolicyDecisionRow.id.desc())
            .limit(limit)
        )
        if source_id is not None:
            stmt = stmt.where(PolicyDecisionRow.source_id == source_id)
        return [decision_to_domain(row) for row in self.session.scalars(stmt)]

    def purge_decisions(self, source_id: str) -> int:
        """Drop a source's decision history. Used by local delete (PRIV-003)."""
        removed = execute_rowcount(
            self.session,
            delete(PolicyDecisionRow).where(PolicyDecisionRow.source_id == source_id),
        )
        self.session.flush()
        return removed
