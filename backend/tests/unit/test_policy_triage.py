"""Cheap triage and semantic policy rules (DEC-016).

The claim under test is not only "the right label comes back". It is that the label is
reached *cheaply*: no model call unless a semantic rule needs one, no reclassification of
unchanged metadata, and no exclusion on a label triage is not confident about. Those are
the properties that make content-type policy save money instead of moving the spend.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.ai.providers import StructuredResponse
from douyin_knowledge.core.text import content_hash
from douyin_knowledge.db.models.capture import Creator, Source, SourceSnapshot
from douyin_knowledge.policy.evaluator import PolicyEvaluator
from douyin_knowledge.policy.models import PolicyAction, PolicyPhase, ProcessingRule, RuleType
from douyin_knowledge.policy.repository import PolicyRepository
from douyin_knowledge.policy.signal import build_signal
from douyin_knowledge.policy.triage import CheapTriage, ContentType, TriageMethod
from douyin_knowledge.policy.triage_service import TriageService


class FakeModel:
    """Minimal `StructuredModel`: returns a canned label and counts how often it is asked."""

    def __init__(self, label: str = "variety_clip", confidence: float = 0.9) -> None:
        self.label = label
        self.confidence = confidence
        self.calls = 0

    def extract(
        self, prompt: str, schema: dict[str, Any], *, temperature: float = 0.0
    ) -> StructuredResponse:
        self.calls += 1
        return StructuredResponse(
            data={"content_type": self.label, "confidence": self.confidence},
            model="fake-triage",
            usage=None,
            raw_text="{}",
        )


@pytest.fixture
def creator(session: Session) -> Creator:
    row = Creator(platform="douyin", external_creator_id="c_triage", display_name="综艺搬运工")
    session.add(row)
    session.flush()
    return row


def _source(
    session: Session,
    creator: Creator,
    *,
    external_id: str = "s_triage",
    title: str | None = "香港美食攻略",
    caption: str | None = "五家店",
    duration_ms: int = 90_000,
) -> Source:
    row = Source(
        platform="douyin",
        external_id=external_id,
        source_type="video",
        creator_id=creator.id,
        title=title,
        caption_raw=caption,
        duration_ms=duration_ms,
    )
    session.add(row)
    session.flush()
    return row


def _snapshot(session: Session, source: Source, hashtags: list[str]) -> SourceSnapshot:
    row = SourceSnapshot(
        source_id=source.id,
        fetched_at_ms=1_000,
        content_hash=content_hash("snap", *hashtags),
        raw_json={"hashtags": hashtags},
    )
    session.add(row)
    session.flush()
    source.latest_snapshot_id = row.id
    session.flush()
    return row


def _rule(**kwargs: Any) -> ProcessingRule:
    defaults: dict[str, Any] = dict(
        id="",
        name=None,
        is_enabled=True,
        rule_type=RuleType.SEMANTIC,
        action=PolicyAction.EXCLUDE,
        priority=0,
    )
    defaults.update(kwargs)
    return ProcessingRule(**defaults)


# --------------------------------------------------------------------- signal


class TestSourceSignal:
    def test_hashtags_come_from_the_snapshot_not_the_caption(
        self, session: Session, creator: Creator
    ) -> None:
        """The bug DEC-016 names: hashtags are captured but never stored as a column.

        Nothing in the spine holds them, so a matcher reading `Source.caption_raw` could
        not see a tag the provider parsed out of the description.
        """
        source = _source(session, creator, caption="五家店都好吃")
        _snapshot(session, source, ["美食", "#香港"])

        signal = build_signal(session, source)

        assert signal.hashtags == ("美食", "#香港")
        assert "美食" in signal.haystack
        assert "#美食" in signal.haystack  # emitted both bare and prefixed

    def test_fingerprint_ignores_a_creator_rename(
        self, session: Session, creator: Creator
    ) -> None:
        """A renamed account has not changed what its videos are about.

        Including the name would invalidate every cached label across the corpus the
        first time a creator changed their handle, which is common on Douyin.
        """
        source = _source(session, creator)
        before = build_signal(session, source).fingerprint

        creator.display_name = "完全不同的名字"
        session.flush()

        assert build_signal(session, source).fingerprint == before

    def test_fingerprint_changes_when_the_title_changes(
        self, session: Session, creator: Creator
    ) -> None:
        source = _source(session, creator)
        before = build_signal(session, source).fingerprint

        source.title = "综艺现场笑点合集"
        session.flush()

        assert build_signal(session, source).fingerprint != before


# ---------------------------------------------------------------------- cues


class TestCueClassification:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("综艺片段 笑点合集", ContentType.VARIETY_CLIP),
            ("电影解说 经典台词", ContentType.MOVIE_CLIP),
            ("翻唱 现场演唱", ContentType.MUSIC_CLIP),
            ("沙雕 整活 笑死", ContentType.MEME),
            ("进球 集锦", ContentType.SPORTS_HIGHLIGHT),
            ("明星 娱乐八卦", ContentType.OTHER_ENTERTAINMENT),
            ("香港美食攻略 避坑", ContentType.KNOWLEDGE),
        ],
    )
    def test_labels_from_cues_alone(
        self, session: Session, creator: Creator, text: str, expected: ContentType
    ) -> None:
        source = _source(session, creator, title=text, caption=None)
        result = CheapTriage().classify(build_signal(session, source))

        assert result.content_type is expected
        assert result.method is TriageMethod.CUES
        assert result.cues

    def test_no_text_is_unknown_not_a_guess(self, session: Session, creator: Creator) -> None:
        source = _source(session, creator, title=None, caption=None)
        result = CheapTriage().classify(build_signal(session, source))

        assert result.content_type is ContentType.UNKNOWN
        assert result.method is TriageMethod.NONE
        assert result.confidence == 0.0

    def test_a_tie_breaks_toward_processing(self, session: Session, creator: Creator) -> None:
        """One cue each for 综艺 and 教程: the safe resolution is to read it, not skip it.

        Wrongly skipping loses knowledge silently; wrongly processing wastes one call.
        """
        source = _source(session, creator, title="综艺", caption="教程")
        result = CheapTriage().classify(build_signal(session, source))

        assert result.content_type is ContentType.KNOWLEDGE
        assert "variety_clip" in result.runners_up

    def test_more_cues_means_more_confidence(self, session: Session, creator: Creator) -> None:
        one = _source(session, creator, external_id="s_one", title="综艺", caption=None)
        many = _source(
            session, creator, external_id="s_many", title="综艺 综艺剪辑 真人秀", caption="脱口秀"
        )
        triage = CheapTriage()

        weak = triage.classify(build_signal(session, one))
        strong = triage.classify(build_signal(session, many))

        assert strong.confidence > weak.confidence
        assert strong.confidence <= 0.92


# ------------------------------------------------------------------- caching


class TestTriageCaching:
    def test_an_unchanged_source_is_not_reclassified(
        self, session: Session, creator: Creator
    ) -> None:
        source = _source(session, creator, title="综艺片段")
        model = FakeModel()
        service = TriageService(session, CheapTriage(model=model, model_name="fake"))  # type: ignore[arg-type]

        first = service.classify(source, allow_model=True)
        second = service.classify(source, allow_model=True)

        assert first.content_type is second.content_type
        assert model.calls == 0  # cues were conclusive, so nothing was ever billed

    def test_edited_metadata_invalidates_the_cached_label(
        self, session: Session, creator: Creator
    ) -> None:
        source = _source(session, creator, title="香港美食攻略")
        service = TriageService(session)

        assert service.classify(source).content_type is ContentType.KNOWLEDGE

        source.title = "综艺片段 真人秀"
        session.flush()

        assert service.classify(source).content_type is ContentType.VARIETY_CLIP

    def test_a_cached_unknown_escalates_once_a_model_appears(
        self, session: Session, creator: Creator
    ) -> None:
        """Otherwise the first inconclusive pass pins the source as unknown forever.

        Configuring a provider later would then have no effect on anything already synced,
        which is exactly the kind of silent staleness the cache exists to avoid.
        """
        source = _source(session, creator, title="随手拍", caption="没什么说明")
        cheap = TriageService(session)
        assert cheap.classify(source).content_type is ContentType.UNKNOWN

        model = FakeModel(label="variety_clip")
        escalating = TriageService(session, CheapTriage(model=model, model_name="fake"))  # type: ignore[arg-type]
        result = escalating.classify(source, allow_model=True)

        assert model.calls == 1
        assert result.content_type is ContentType.VARIETY_CLIP
        assert result.method is TriageMethod.MODEL

        # And the escalated answer is itself cached: a second ask costs nothing more.
        escalating.classify(source, allow_model=True)
        assert model.calls == 1

    def test_a_model_that_returns_nonsense_degrades_to_unknown(
        self, session: Session, creator: Creator
    ) -> None:
        """A routing hint must not be able to crash the policy gate."""
        source = _source(session, creator, title="随手拍", caption=None)
        model = FakeModel(label="not_a_real_label")
        service = TriageService(session, CheapTriage(model=model, model_name="fake"))  # type: ignore[arg-type]

        result = service.classify(source, allow_model=True)

        assert result.content_type is ContentType.UNKNOWN


# ----------------------------------------------------------- semantic rules


class TestSemanticRules:
    @pytest.fixture
    def repository(self, session: Session) -> PolicyRepository:
        return PolicyRepository(session)

    def _evaluator(
        self, session: Session, repository: PolicyRepository, model: FakeModel | None = None
    ) -> PolicyEvaluator:
        triage = TriageService(
            session,
            CheapTriage(model=model, model_name="fake" if model else None),  # type: ignore[arg-type]
        )
        return PolicyEvaluator(
            repository, session, triage=triage, allow_triage_model=model is not None
        )

    def test_a_semantic_rule_excludes_a_matching_clip(
        self, session: Session, creator: Creator, repository: PolicyRepository
    ) -> None:
        source = _source(session, creator, title="综艺片段 真人秀 脱口秀")
        rule = repository.save_rule(
            _rule(matcher_json={"content_types": ["variety_clip"]}, name="不看综艺")
        )

        decision = self._evaluator(session, repository).evaluate(source)

        assert decision.action == PolicyAction.EXCLUDE
        assert decision.phase == PolicyPhase.SEMANTIC
        assert decision.rule_id == rule.id

    def test_a_knowledge_video_is_untouched_by_an_entertainment_rule(
        self, session: Session, creator: Creator, repository: PolicyRepository
    ) -> None:
        source = _source(session, creator, title="香港美食攻略 避坑指南")
        repository.save_rule(_rule(matcher_json={"content_types": ["variety_clip"]}))

        decision = self._evaluator(session, repository).evaluate(source)

        assert decision.action == PolicyAction.PROCESS
        assert decision.reason_code == "default_policy"

    def test_unknown_never_satisfies_a_semantic_rule(
        self, session: Session, creator: Creator, repository: PolicyRepository
    ) -> None:
        """A classifier miss must not become silent data loss.

        `unknown` means triage could not tell. Letting it match -- even a rule that names
        the string -- would turn every failure to classify into a skipped video.
        """
        source = _source(session, creator, title=None, caption=None)
        repository.save_rule(
            _rule(matcher_json={"content_types": ["unknown", "variety_clip"]})
        )

        decision = self._evaluator(session, repository).evaluate(source)

        assert decision.action == PolicyAction.PROCESS

    def test_min_confidence_is_a_floor_a_weak_label_cannot_clear(
        self, session: Session, creator: Creator, repository: PolicyRepository
    ) -> None:
        source = _source(session, creator, title="综艺", caption=None)  # one cue only
        repository.save_rule(
            _rule(matcher_json={"content_types": ["variety_clip"], "min_confidence": 0.95})
        )

        decision = self._evaluator(session, repository).evaluate(source)

        assert decision.action == PolicyAction.PROCESS

    def test_an_empty_semantic_matcher_matches_nothing(
        self, session: Session, creator: Creator, repository: PolicyRepository
    ) -> None:
        source = _source(session, creator, title="综艺片段 真人秀")
        repository.save_rule(_rule(matcher_json={}))

        assert self._evaluator(session, repository).evaluate(source).action == PolicyAction.PROCESS

    def test_no_semantic_rule_means_no_classification_at_all(
        self, session: Session, creator: Creator, repository: PolicyRepository
    ) -> None:
        """The cost control, asserted directly (DEC-016).

        Pass B must not run when nothing would consult its answer -- otherwise every sync
        on a library with no semantic rules pays for triage it never reads.
        """
        source = _source(session, creator, title="随手拍", caption=None)
        model = FakeModel()
        evaluator = self._evaluator(session, repository, model)

        decision = evaluator.evaluate(source)

        assert decision.action == PolicyAction.PROCESS
        assert decision.phase == PolicyPhase.METADATA
        assert model.calls == 0
        assert evaluator.triage.cached(source.id) is None

    def test_a_metadata_rule_still_wins_before_anything_is_classified(
        self, session: Session, creator: Creator, repository: PolicyRepository
    ) -> None:
        """Pass A decides first, so a deterministic rule never pays for triage."""
        source = _source(session, creator, title="综艺片段 真人秀", duration_ms=5_000)
        repository.save_rule(
            _rule(
                rule_type=RuleType.METADATA,
                matcher_json={"max_duration_ms": 10_000},
                name="太短",
            )
        )
        repository.save_rule(_rule(matcher_json={"content_types": ["variety_clip"]}))

        model = FakeModel()
        evaluator = self._evaluator(session, repository, model)
        decision = evaluator.evaluate(source)

        assert decision.phase == PolicyPhase.METADATA
        assert model.calls == 0
        assert evaluator.triage.cached(source.id) is None


# ------------------------------------------------------- metadata matchers


class TestKeywordMatching:
    @pytest.fixture
    def repository(self, session: Session) -> PolicyRepository:
        return PolicyRepository(session)

    def test_keywords_are_or_where_title_contains_is_and(
        self, session: Session, creator: Creator, repository: PolicyRepository
    ) -> None:
        source = _source(session, creator, title="香港美食攻略", caption="五家店")
        evaluator = PolicyEvaluator(repository, session)

        # OR: only the second word appears, and that is enough.
        assert evaluator._matcher_matches(
            build_signal(session, source), {"keywords": ["台北", "香港"]}
        )
        # AND: one missing needle is fatal.
        assert not evaluator._matcher_matches(
            build_signal(session, source), {"title_contains": ["香港", "台北"]}
        )

    def test_a_hashtag_rule_fires_on_a_tag_only_the_snapshot_holds(
        self, session: Session, creator: Creator, repository: PolicyRepository
    ) -> None:
        """The documented rule from PROCESSING_POLICY.md 5.5, which could not fire before."""
        source = _source(session, creator, title="片段", caption="没有标签文本")
        _snapshot(session, source, ["综艺"])

        evaluator = PolicyEvaluator(repository, session)

        assert evaluator._matcher_matches(build_signal(session, source), {"hashtags": ["综艺"]})
        assert not evaluator._matcher_matches(build_signal(session, source), {"hashtags": ["美食"]})
