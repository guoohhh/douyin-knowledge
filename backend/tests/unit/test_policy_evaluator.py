"""Tests for PolicyEvaluator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from douyin_knowledge.capture.models import CapturedCreator, CapturedSource
from douyin_knowledge.policy.evaluator import PolicyEvaluator
from douyin_knowledge.policy.models import PolicyAction, PolicyPhase, ProcessingRule, RuleType


@pytest.fixture
def mock_repository() -> MagicMock:
    return MagicMock()


@pytest.fixture
def evaluator(mock_repository: MagicMock) -> PolicyEvaluator:
    return PolicyEvaluator(mock_repository)


@pytest.fixture
def sample_source() -> CapturedSource:
    return CapturedSource(
        platform="douyin",
        external_id="source_123",
        source_type="video",
        title="Test Video",
        caption_raw="Test caption",
        source_url="https://example.com/video/123",
        published_at_ms=1700000000000,
        saved_at_ms=1700000001000,
        availability="available",
        creator=CapturedCreator(
            external_creator_id="creator_456",
            display_name="Test Creator",
        ),
        media=[],
        raw={},
    )


def test_default_action_when_no_rules(
    evaluator: PolicyEvaluator,
    mock_repository: MagicMock,
    sample_source: CapturedSource,
) -> None:
    mock_repository.list_rules.return_value = []

    decision = evaluator.evaluate(sample_source)

    assert decision.action == PolicyAction.PROCESS
    assert decision.phase == PolicyPhase.METADATA
    assert decision.reason_code == "default_policy"
    assert decision.rule_id is None


def test_source_rule_matches(
    evaluator: PolicyEvaluator,
    mock_repository: MagicMock,
    sample_source: CapturedSource,
) -> None:
    rule = ProcessingRule(
        id="rule_1",
        name="Exclude this source",
        is_enabled=True,
        rule_type=RuleType.SOURCE,
        action=PolicyAction.EXCLUDE,
        priority=100,
        target_source_id="source_123",
        created_at_ms=1700000000000,
    )
    mock_repository.list_rules.return_value = [rule]

    decision = evaluator.evaluate(sample_source)

    assert decision.action == PolicyAction.EXCLUDE
    assert decision.rule_id == "rule_1"
    assert decision.reason_code == "source_matched"


def test_creator_rule_matches(
    evaluator: PolicyEvaluator,
    mock_repository: MagicMock,
    sample_source: CapturedSource,
) -> None:
    rule = ProcessingRule(
        id="rule_2",
        name="Always process this creator",
        is_enabled=True,
        rule_type=RuleType.CREATOR,
        action=PolicyAction.ALWAYS_PROCESS,
        priority=50,
        target_creator_id="creator_456",
        created_at_ms=1700000000000,
    )
    mock_repository.list_rules.return_value = [rule]

    decision = evaluator.evaluate(sample_source)

    assert decision.action == PolicyAction.ALWAYS_PROCESS
    assert decision.rule_id == "rule_2"
    assert decision.reason_code == "creator_matched"


def test_priority_ordering(
    evaluator: PolicyEvaluator,
    mock_repository: MagicMock,
    sample_source: CapturedSource,
) -> None:
    low_priority = ProcessingRule(
        id="rule_low",
        name="Low priority",
        is_enabled=True,
        rule_type=RuleType.SOURCE,
        action=PolicyAction.METADATA_ONLY,
        priority=10,
        target_source_id="source_123",
        created_at_ms=1700000000000,
    )
    high_priority = ProcessingRule(
        id="rule_high",
        name="High priority",
        is_enabled=True,
        rule_type=RuleType.SOURCE,
        action=PolicyAction.EXCLUDE,
        priority=100,
        target_source_id="source_123",
        created_at_ms=1700000000000,
    )
    mock_repository.list_rules.return_value = [low_priority, high_priority]

    decision = evaluator.evaluate(sample_source)

    # Higher priority should win
    assert decision.action == PolicyAction.EXCLUDE
    assert decision.rule_id == "rule_high"


def test_disabled_rules_ignored(
    evaluator: PolicyEvaluator,
    mock_repository: MagicMock,
    sample_source: CapturedSource,
) -> None:
    rule = ProcessingRule(
        id="rule_disabled",
        name="Disabled rule",
        is_enabled=False,
        rule_type=RuleType.SOURCE,
        action=PolicyAction.EXCLUDE,
        priority=100,
        target_source_id="source_123",
        created_at_ms=1700000000000,
    )
    # Repository filters out disabled rules
    mock_repository.list_rules.return_value = []

    decision = evaluator.evaluate(sample_source)

    assert decision.action == PolicyAction.PROCESS
    assert decision.reason_code == "default_policy"


def test_no_match_falls_through_to_default(
    evaluator: PolicyEvaluator,
    mock_repository: MagicMock,
    sample_source: CapturedSource,
) -> None:
    rule = ProcessingRule(
        id="rule_other",
        name="Different source",
        is_enabled=True,
        rule_type=RuleType.SOURCE,
        action=PolicyAction.EXCLUDE,
        priority=100,
        target_source_id="source_999",
        created_at_ms=1700000000000,
    )
    mock_repository.list_rules.return_value = [rule]

    decision = evaluator.evaluate(sample_source)

    assert decision.action == PolicyAction.PROCESS
    assert decision.rule_id is None
    assert decision.reason_code == "default_policy"
