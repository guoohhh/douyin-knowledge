"""Policy evaluator: decide whether to process a source and at what level.

Evaluation order (from ARCHITECTURE.md):
1. per-source always_process
2. per-source exclude
3. creator / collection explicit rules
4. semantic/domain rules (future: requires triage classification)
5. keyword rules (future: requires matcher implementation)
6. default: process

V1 implementation: metadata phase only, basic rule matching.
Semantic phase (triage classification) deferred to later implementation.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

from douyin_knowledge.policy.models import PolicyAction, PolicyDecision, PolicyPhase, RuleType

if TYPE_CHECKING:
    from douyin_knowledge.capture.models import CapturedSource
    from douyin_knowledge.policy.models import ProcessingRule
    from douyin_knowledge.policy.repository import PolicyRepository


class PolicyEvaluator:
    """Evaluates processing policy for sources."""

    def __init__(self, repository: PolicyRepository) -> None:
        self.repository = repository

    def evaluate(self, source: CapturedSource) -> PolicyDecision:
        """Decide whether and how to process a source.

        Args:
            source: Captured source with metadata

        Returns:
            PolicyDecision with action and matched rule
        """
        rules = self.repository.list_rules(enabled_only=True)

        # Sort by priority (descending) then creation time
        rules_sorted = sorted(
            rules,
            key=lambda r: (-r.priority, r.created_at_ms),
        )

        # Evaluate in precedence order
        matched_rule = self._find_matching_rule(source, rules_sorted)

        if matched_rule:
            action = matched_rule.action
            reason_code = f"{matched_rule.rule_type}_matched"
            explanation = {
                "rule_name": matched_rule.name,
                "rule_type": matched_rule.rule_type,
            }
            rule_id = matched_rule.id
        else:
            # Default: process
            action = PolicyAction.PROCESS
            reason_code = "default_policy"
            explanation = {"note": "no matching rules, using default"}
            rule_id = None

        return PolicyDecision(
            id=f"pd_{uuid4().hex[:16]}",
            source_id=source.external_id,
            rule_id=rule_id,
            phase=PolicyPhase.METADATA,
            action=action,
            reason_code=reason_code,
            explanation=explanation,
            created_at_ms=int(time.time() * 1000),
        )

    def _find_matching_rule(
        self,
        source: CapturedSource,
        rules: list[ProcessingRule],
    ) -> ProcessingRule | None:
        """Find the first matching rule in priority order."""
        for rule in rules:
            if self._rule_matches(source, rule):
                return rule
        return None

    def _rule_matches(self, source: CapturedSource, rule: ProcessingRule) -> bool:
        """Check if a rule matches a source.

        Implements precedence from ARCHITECTURE.md § 18:
        - SOURCE rules match on target_source_id
        - CREATOR rules match on target_creator_id
        - COLLECTION rules match on target_collection_id
        - METADATA rules check matcher_json (not implemented in V1)
        - SEMANTIC rules require triage (not implemented in V1)
        """
        if rule.rule_type == RuleType.SOURCE:
            return rule.target_source_id == source.external_id

        if rule.rule_type == RuleType.CREATOR:
            return rule.target_creator_id == source.creator.external_creator_id

        if rule.rule_type == RuleType.COLLECTION:
            # CapturedSource doesn't carry collection IDs in V1
            # Will need to load from source_collections join table when needed
            return False

        if rule.rule_type == RuleType.METADATA:
            # V1: matcher_json evaluation not implemented
            # Future: match on hashtags, duration, etc.
            return False

        if rule.rule_type == RuleType.SEMANTIC:
            # Requires triage classification, not implemented in V1
            return False

        return False
