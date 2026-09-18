"""PolicyEvaluator tests.

These run against real rows and a real repository rather than a MagicMock. The previous
version mocked the repository and passed a `CapturedSource` DTO, which meant it could not
have caught the identity bug it was ostensibly covering: rules point at `sources.id`, the
DTO only carries the provider's `external_id`, so per-source rules matched in the test and
never in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.db.models.capture import (
    Collection,
    Creator,
    Source,
    SourceCollectionMembership,
)
from douyin_knowledge.policy.evaluator import PolicyEvaluator
from douyin_knowledge.policy.models import PolicyAction, PolicyPhase, ProcessingRule, RuleType
from douyin_knowledge.policy.repository import PolicyRepository


@pytest.fixture
def creator(session: Session) -> Creator:
    row = Creator(platform="douyin", external_creator_id="creator_456", display_name="老王探店")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def source(session: Session, creator: Creator) -> Source:
    row = Source(
        platform="douyin",
        external_id="source_123",
        source_type="video",
        creator_id=creator.id,
        title="香港美食清单",
        caption_raw="五家店 #美食 #香港",
        duration_ms=90_000,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def repository(session: Session) -> PolicyRepository:
    return PolicyRepository(session)


@pytest.fixture
def evaluator(repository: PolicyRepository) -> PolicyEvaluator:
    return PolicyEvaluator(repository)


def _rule(**kwargs) -> ProcessingRule:
    defaults = dict(
        id="",
        name=None,
        is_enabled=True,
        rule_type=RuleType.SOURCE,
        action=PolicyAction.EXCLUDE,
        priority=0,
    )
    defaults.update(kwargs)
    return ProcessingRule(**defaults)  # type: ignore[arg-type]


class TestDefaults:
    def test_no_rules_means_process(self, evaluator: PolicyEvaluator, source: Source) -> None:
        decision = evaluator.evaluate(source)
        assert decision.action == PolicyAction.PROCESS
        assert decision.phase == PolicyPhase.METADATA
        assert decision.reason_code == "default_policy"
        assert decision.rule_id is None
        assert decision.source_id == source.id, "the FK must point at the row, not the provider id"

    def test_decision_is_persisted_by_default(
        self, evaluator: PolicyEvaluator, repository: PolicyRepository, source: Source
    ) -> None:
        evaluator.evaluate(source)
        assert repository.get_latest_decision(source.id) is not None

    def test_persist_false_leaves_no_trace(
        self, evaluator: PolicyEvaluator, repository: PolicyRepository, source: Source
    ) -> None:
        evaluator.evaluate(source, persist=False)
        assert repository.get_latest_decision(source.id) is None

    def test_disabled_rules_are_ignored(
        self, evaluator: PolicyEvaluator, repository: PolicyRepository, source: Source
    ) -> None:
        repository.save_rule(
            _rule(target_source_id=source.id, is_enabled=False, priority=100)
        )
        assert evaluator.evaluate(source).action == PolicyAction.PROCESS


class TestTargetedRules:
    def test_source_rule_matches_on_internal_id(
        self, evaluator: PolicyEvaluator, repository: PolicyRepository, source: Source
    ) -> None:
        saved = repository.save_rule(_rule(name="skip this one", target_source_id=source.id))
        decision = evaluator.evaluate(source)
        assert decision.action == PolicyAction.EXCLUDE
        assert decision.rule_id == saved.id
        assert decision.reason_code == "source_rule_matched"

    def test_a_rule_cannot_target_an_external_id(
        self, repository: PolicyRepository, source: Source
    ) -> None:
        """Guards the bug this rewrite fixed, at the strongest available layer.

        `processing_rules.target_source_id` is a foreign key, so the provider id the old
        evaluator compared against cannot even be stored. The identity mismatch is now
        unrepresentable rather than merely unmatched.
        """
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            repository.save_rule(_rule(target_source_id=source.external_id))
        # The failed INSERT leaves the session unusable; without this the fixture's
        # `session_scope` commit raises on teardown and reports as a second failure.
        repository.session.rollback()

    def test_creator_rule_matches(
        self,
        evaluator: PolicyEvaluator,
        repository: PolicyRepository,
        source: Source,
        creator: Creator,
    ) -> None:
        saved = repository.save_rule(
            _rule(
                rule_type=RuleType.CREATOR,
                action=PolicyAction.ALWAYS_PROCESS,
                target_creator_id=creator.id,
            )
        )
        decision = evaluator.evaluate(source)
        assert decision.action == PolicyAction.ALWAYS_PROCESS
        assert decision.rule_id == saved.id
        assert decision.explanation["matched_on"]["creator_name"] == "老王探店"

    def test_collection_rule_matches_present_membership(
        self,
        evaluator: PolicyEvaluator,
        repository: PolicyRepository,
        session: Session,
        source: Source,
    ) -> None:
        collection = Collection(
            platform="douyin", external_collection_id="c1", name="美食"
        )
        session.add(collection)
        session.flush()
        session.add(
            SourceCollectionMembership(source_id=source.id, collection_id=collection.id)
        )
        session.flush()

        repository.save_rule(
            _rule(rule_type=RuleType.COLLECTION, target_collection_id=collection.id)
        )
        assert evaluator.evaluate(source).action == PolicyAction.EXCLUDE

    def test_removed_membership_stops_matching(
        self,
        evaluator: PolicyEvaluator,
        repository: PolicyRepository,
        session: Session,
        source: Source,
    ) -> None:
        """Membership is soft-removed, so a stale row must not keep applying its rule."""
        collection = Collection(
            platform="douyin", external_collection_id="c2", name="旧合集"
        )
        session.add(collection)
        session.flush()
        session.add(
            SourceCollectionMembership(
                source_id=source.id, collection_id=collection.id, is_present=0
            )
        )
        session.flush()

        repository.save_rule(
            _rule(rule_type=RuleType.COLLECTION, target_collection_id=collection.id)
        )
        assert evaluator.evaluate(source).action == PolicyAction.PROCESS


class TestPrecedence:
    def test_higher_priority_wins_within_a_type(
        self, evaluator: PolicyEvaluator, repository: PolicyRepository, source: Source
    ) -> None:
        repository.save_rule(
            _rule(
                name="low",
                action=PolicyAction.METADATA_ONLY,
                priority=10,
                target_source_id=source.id,
            )
        )
        high = repository.save_rule(
            _rule(name="high", action=PolicyAction.EXCLUDE, priority=100, target_source_id=source.id)
        )
        assert evaluator.evaluate(source).rule_id == high.id

    def test_per_source_beats_a_higher_priority_creator_rule(
        self,
        evaluator: PolicyEvaluator,
        repository: PolicyRepository,
        source: Source,
        creator: Creator,
    ) -> None:
        """An explicit decision about this item outranks any pattern."""
        repository.save_rule(
            _rule(
                rule_type=RuleType.CREATOR,
                action=PolicyAction.EXCLUDE,
                priority=999,
                target_creator_id=creator.id,
            )
        )
        pinned = repository.save_rule(
            _rule(action=PolicyAction.ALWAYS_PROCESS, priority=0, target_source_id=source.id)
        )
        decision = evaluator.evaluate(source)
        assert decision.rule_id == pinned.id
        assert decision.action == PolicyAction.ALWAYS_PROCESS

    def test_always_process_beats_exclude_on_the_same_source(
        self, evaluator: PolicyEvaluator, repository: PolicyRepository, source: Source
    ) -> None:
        repository.save_rule(
            _rule(action=PolicyAction.EXCLUDE, priority=100, target_source_id=source.id)
        )
        pinned = repository.save_rule(
            _rule(action=PolicyAction.ALWAYS_PROCESS, priority=1, target_source_id=source.id)
        )
        assert evaluator.evaluate(source).rule_id == pinned.id


class TestMetadataMatcher:
    def _match(self, evaluator: PolicyEvaluator, repo: PolicyRepository, source: Source, matcher):
        repo.save_rule(_rule(rule_type=RuleType.METADATA, matcher_json=matcher))
        return evaluator.evaluate(source).action == PolicyAction.EXCLUDE

    def test_title_contains(self, evaluator, repository, source) -> None:
        assert self._match(evaluator, repository, source, {"title_contains": "香港"})

    def test_title_contains_is_all_of(self, evaluator, repository, source) -> None:
        assert not self._match(
            evaluator, repository, source, {"title_contains": ["香港", "东京"]}
        )

    def test_hashtag_match(self, evaluator, repository, source) -> None:
        assert self._match(evaluator, repository, source, {"hashtags": ["美食"]})

    def test_duration_bounds(self, evaluator, repository, source) -> None:
        assert self._match(evaluator, repository, source, {"max_duration_ms": 120_000})

    def test_duration_bounds_exclude(self, evaluator, repository, source) -> None:
        assert not self._match(evaluator, repository, source, {"min_duration_ms": 600_000})

    def test_creator_name_contains(self, evaluator, repository, source) -> None:
        assert self._match(evaluator, repository, source, {"creator_name_contains": "探店"})

    def test_empty_matcher_never_matches(self, evaluator, repository, source) -> None:
        """A rule saved with no conditions is a half-finished edit, not "match all"."""
        assert not self._match(evaluator, repository, source, {})

    def test_semantic_rules_stay_inert(
        self, evaluator: PolicyEvaluator, repository: PolicyRepository, source: Source
    ) -> None:
        repository.save_rule(_rule(rule_type=RuleType.SEMANTIC, priority=100))
        assert evaluator.evaluate(source).action == PolicyAction.PROCESS


class TestShouldProcess:
    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            (PolicyAction.PROCESS, True),
            (PolicyAction.ALWAYS_PROCESS, True),
            (PolicyAction.METADATA_ONLY, False),
            (PolicyAction.EXCLUDE, False),
        ],
    )
    def test_blocking_actions(
        self,
        evaluator: PolicyEvaluator,
        repository: PolicyRepository,
        source: Source,
        action: PolicyAction,
        expected: bool,
    ) -> None:
        repository.save_rule(_rule(action=action, target_source_id=source.id))
        allowed, decision = evaluator.should_process(source)
        assert allowed is expected
        assert decision.action == action
