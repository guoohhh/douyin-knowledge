"""Long media must transcribe reliably, and must not admit that it was segmented.

The failure this covers, measured against a real source
(``src_a0e0631c136e141dff7008``, 841.767 s, 98,997,915 bytes, healthy H.264/AAC that
ffmpeg decodes through EOF):

    180 s -> success      480 s -> success      full ~841.7 s -> failed 2/2
    300 s -> success      600 s -> success

and the two full-media failures were not the same failure twice:

    attempt 1: audio sent to ~216.8 s, results to ~167.45 s, WebSocket 1006, ping timeout
    attempt 2: all audio sent, last result ~744.44 s, WebSocket 1000, upstream 45000081
               "Timeout waiting next packet; session has ended"

So: a non-deterministic provider-side session failure, with a transport failure secondary.
No fixed official duration limit was found, and nothing here asserts one -- the segment
length is an operational choice with margin below the only length actually observed to
fail, and these tests are written against *configured* values rather than constants so they
keep meaning if the policy is retuned.

The invariant that matters downstream is narrower than "long videos work": an
`EvidenceUnit.start_ms` has to point at the moment of the original video. Segment-relative
timestamps would still produce a plausible transcript, plausible chunks, plausible
citations -- and every citation on a long video would point minutes away from what it
quotes, which is worse than a missing transcript because it looks fine.
"""

from __future__ import annotations

import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from douyin_knowledge.ai.adapters.doubao_adapter import (
    DEFAULT_SEGMENT_S,
    AudioSegment,
    DoubaoASRProvider,
    plan_segments,
)
from douyin_knowledge.core.errors import ASRFailed, ConfigurationError

#: The real source's duration, to the millisecond.
REAL_DURATION_MS = 841_767

SEGMENT_S = 300.0
OVERLAP_S = 1.0


def _wav(path: Path, duration_ms: int) -> Path:
    """A 16 kHz mono PCM WAV of exactly `duration_ms`, so slicing is frame-exact.

    Silence is fine: no test here asserts anything about recognition quality, only about
    which bytes reach the provider and what happens to the timestamps that come back.
    """
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * (16 * duration_ms))
    return path


def _utterance(text: str, start_ms: int, end_ms: int, **extra: object) -> dict[str, object]:
    return {"text": text, "start_time": start_ms, "end_time": end_ms, **extra}


def _response(*utterances: dict[str, object], text: str = "") -> dict[str, object]:
    return {
        "code": 0,
        "is_last_package": True,
        "payload_msg": {
            "result": {
                "text": text or "".join(str(u["text"]) for u in utterances),
                "utterances": list(utterances),
            }
        },
    }


class Recorder:
    """A fake transport that records what each session was actually given.

    Records the sliced audio's duration rather than its path, because the duration is the
    part under test: a segment that receives the whole file, or 1 s of it, is a segmentation
    bug that a path or a call count cannot see.
    """

    def __init__(self, replies: list[list[dict[str, object]]] | None = None) -> None:
        self.durations_ms: list[int] = []
        self.languages: list[str | None] = []
        self._replies = replies

    def __call__(self, path: Path, language: str | None) -> list[dict[str, object]]:
        with wave.open(str(path), "rb") as audio:
            self.durations_ms.append(int(audio.getnframes() * 1000 / audio.getframerate()))
        self.languages.append(language)
        if self._replies is None:
            return [_response()]
        return self._replies[len(self.durations_ms) - 1]


def _provider(transport: object, **overrides: object) -> DoubaoASRProvider:
    kwargs: dict[str, object] = {
        "api_key": "not-a-real-key",
        "transport": transport,
        "segment_s": SEGMENT_S,
        "overlap_s": OVERLAP_S,
        # Tests never wait out a real backoff; the schedule itself is asserted where it
        # matters rather than slept through everywhere.
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    return DoubaoASRProvider(**kwargs)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def real_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One shared 841.767 s WAV for every test that needs the real shape.

    Session-scoped because it is 26.9 MB of silence and each of these tests wants the same
    file; only the transport script differs. Writing it per test put ~800 MB into pytest's
    tmp directory per run, which eventually exhausted the disk and made tmp_path allocation
    fail in *other* modules -- an error that points nowhere near the cause.
    """
    return _wav(tmp_path_factory.mktemp("longmedia") / "real.wav", REAL_DURATION_MS)


class TestShortMediaKeepsTheSimplePath:
    """Most sources are seconds long. That case never failed and must not change."""

    def test_a_short_clip_uses_one_session(self, tmp_path: Path) -> None:
        recorder = Recorder()
        _provider(recorder).transcribe(str(_wav(tmp_path / "a.wav", 20_000)))
        assert len(recorder.durations_ms) == 1

    def test_a_short_clip_sends_the_whole_file_unsliced(self, tmp_path: Path) -> None:
        recorder = Recorder()
        _provider(recorder).transcribe(str(_wav(tmp_path / "a.wav", 20_000)))
        assert recorder.durations_ms == [20_000]

    def test_exactly_one_segment_long_is_still_one_session(self, tmp_path: Path) -> None:
        """The boundary is inclusive: a file of exactly one segment needs no second one."""
        recorder = Recorder()
        duration = int(SEGMENT_S * 1000)
        _provider(recorder).transcribe(str(_wav(tmp_path / "a.wav", duration)))
        assert recorder.durations_ms == [duration]

    def test_the_provider_text_field_is_still_preferred_when_present(
        self, tmp_path: Path
    ) -> None:
        """Short media keeps using the provider's own full transcript, not a rejoin.

        The segmented path has to rebuild `full_text` from kept utterances because the
        provider's per-segment text would duplicate overlap. Short media has no overlap, so
        it keeps deferring to the provider -- which is what preserves punctuation and
        spacing the utterance list does not carry.
        """
        recorder = Recorder([[_response(_utterance("八元。", 10, 900), text="八元!!")]])
        result = _provider(recorder).transcribe(str(_wav(tmp_path / "a.wav", 5_000)))
        assert result.full_text == "八元!!"


class TestLongMediaSplitsIntoBoundedSessions:
    def test_the_real_841_second_shape_needs_several_sessions(self, real_wav: Path) -> None:
        """The source that failed 2/2 as a single session."""
        recorder = Recorder()
        _provider(recorder).transcribe(str(real_wav))
        assert len(recorder.durations_ms) == 3

    def test_no_session_exceeds_the_segment_policy_plus_its_overlap(
        self, real_wav: Path
    ) -> None:
        """The reliability property, stated as a bound rather than as a count.

        Asserted against the configured policy, not against 300 s: this must keep holding if
        the policy is retuned, because the number is an operational guess and the bound is
        the actual requirement.
        """
        recorder = Recorder()
        _provider(recorder).transcribe(str(real_wav))
        ceiling = int((SEGMENT_S + 2 * OVERLAP_S) * 1000)
        assert recorder.durations_ms
        assert max(recorder.durations_ms) <= ceiling

    def test_no_session_receives_the_whole_file(self, real_wav: Path) -> None:
        recorder = Recorder()
        _provider(recorder).transcribe(str(real_wav))
        assert REAL_DURATION_MS not in recorder.durations_ms

    def test_the_sessions_cover_the_media_with_only_overlap_to_spare(
        self, real_wav: Path
    ) -> None:
        """Nothing is skipped. A silently unsent minute is a silently missing minute.

        Total audio sent is the media plus exactly the overlap between adjacent segments;
        anything less means a gap, anything more means a segment was sent twice.
        """
        recorder = Recorder()
        _provider(recorder).transcribe(str(real_wav))
        overlaps = (len(recorder.durations_ms) - 1) * 2 * int(OVERLAP_S * 1000)
        assert sum(recorder.durations_ms) == REAL_DURATION_MS + overlaps

    def test_the_language_reaches_every_session(self, real_wav: Path) -> None:
        recorder = Recorder()
        _provider(recorder).transcribe(
            str(real_wav), language="zh-CN"
        )
        assert recorder.languages == ["zh-CN"] * len(recorder.durations_ms)

    def test_the_segment_policy_is_configurable(self, real_wav: Path) -> None:
        """Because it is an operational choice about a provider that may change.

        A shorter segment length has to produce more sessions with no other change, which is
        what makes lowering it a usable response to sessions starting to fail in production.
        """
        recorder = Recorder()
        _provider(recorder, segment_s=120.0).transcribe(
            str(real_wav)
        )
        assert len(recorder.durations_ms) == 8

    def test_the_default_policy_is_below_the_only_failing_length(self) -> None:
        """Not an assertion that 600 s is a limit -- that is explicitly not claimed.

        Only that the shipped default sits under the shortest duration actually observed to
        fail, with margin. 841.8 s failed; 300 s succeeded with room.
        """
        assert DEFAULT_SEGMENT_S * 1000 < REAL_DURATION_MS


class TestTimestampsAreOriginalMediaTimestamps:
    """Doubao timestamps each segment from zero. Downstream, zero is the video's start.

    This is the non-negotiable one. `EvidenceUnit.start_ms` feeds citation labels directly,
    so a segment-relative timestamp does not fail -- it produces a citation that says
    ``@ 00:30`` for speech at ``@ 05:31`` and invites the user to check a moment where
    nothing was said.
    """

    def test_a_second_segment_utterance_becomes_a_global_timestamp(
        self, real_wav: Path
    ) -> None:
        recorder = Recorder(
            [
                [_response(_utterance("第一段。", 1_000, 2_000))],
                # 30 s into segment 2, whose audio starts at 299 s (300 s core minus 1 s
                # overlap). So 329 s on the video's timeline, not 30.
                [_response(_utterance("第二段。", 30_000, 31_000))],
                [_response()],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert [(s.start_ms, s.end_ms) for s in result.segments] == [
            (1_000, 2_000),
            (329_000, 330_000),
        ]

    def test_the_offset_is_coverage_not_core(self, real_wav: Path) -> None:
        """The subtle way to get this wrong: offset by core start and lose the overlap.

        Segment 2's audio begins 1 s *before* its core, so its local 1 s is 300 s globally,
        not 301 s. Offsetting by the core would shift every utterance of every segment after
        the first later by exactly the overlap -- a second of drift that no single citation
        looks wrong enough to catch.

        Note what this does *not* test: an utterance at local 0 ms would be at 299 s
        globally, which is inside segment 1's core, so segment 2 does not own it and it never
        reaches the output. That is the ownership rule working, not a lost utterance --
        segment 1 reported the same speech and segment 1 keeps it.
        """
        recorder = Recorder(
            [
                [_response()],
                [_response(_utterance("开头。", 1_000, 3_000))],
                [_response()],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert [(s.start_ms, s.end_ms) for s in result.segments] == [(300_000, 302_000)]

    def test_a_final_segment_utterance_lands_inside_the_media(self, real_wav: Path) -> None:
        """A timestamp past the end of the video is a citation nobody can check."""
        recorder = Recorder(
            [
                [_response()],
                [_response()],
                [_response(_utterance("结尾。", 240_000, 241_000))],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert result.segments
        assert all(s.end_ms <= REAL_DURATION_MS for s in result.segments)

    def test_confidence_and_speaker_survive_segmentation(self, real_wav: Path) -> None:
        recorder = Recorder(
            [
                [_response()],
                [
                    _response(
                        _utterance(
                            "有人说话。",
                            10_000,
                            11_000,
                            confidence=0.91,
                            additions={"speaker_id": "2"},
                        )
                    )
                ],
                [_response()],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert [(s.confidence, s.speaker_id) for s in result.segments] == [(0.91, "2")]


class TestOverlapDoesNotDuplicate:
    """Overlap is context for the recogniser, never a second copy of the evidence.

    Both neighbours transcribe the overlapping audio, and they do not transcribe it
    identically -- punctuation shifts, a word is heard differently with less context after
    it. So ownership is temporal: the segment whose core interval holds an utterance's
    midpoint keeps it, and exactly one does.
    """

    def test_speech_in_the_overlap_is_kept_once(self, real_wav: Path) -> None:
        # 299.5 s, i.e. inside segment 1's core and inside segment 2's overlap. Both
        # sessions hear it and both report it; one keeps it.
        recorder = Recorder(
            [
                [_response(_utterance("边界上的话。", 299_500, 300_200))],
                [_response(_utterance("边界上的话，", 500, 1_200))],
                [_response()],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert len(result.segments) == 1

    def test_the_owner_is_decided_by_midpoint_not_by_who_heard_it_first(
        self, real_wav: Path
    ) -> None:
        """An utterance mostly inside segment 2 belongs to segment 2, even though 1 heard it.

        Starts at 299.8 s and runs 3 s, so its midpoint is past the 300 s boundary. Awarding
        it by start time would give it to the segment that caught only its first syllable.
        """
        recorder = Recorder(
            [
                [_response(_utterance("第一段听到的。", 299_800, 302_800))],
                [_response(_utterance("第二段听到的。", 800, 3_800))],
                [_response()],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert [s.text for s in result.segments] == ["第二段听到的。"]

    def test_differing_punctuation_across_the_overlap_does_not_produce_two_copies(
        self, real_wav: Path
    ) -> None:
        """Deliberately unequal strings: text comparison would keep both, or need a threshold.

        The point of temporal ownership is that this needs no similarity heuristic at all.
        """
        recorder = Recorder(
            [
                [_response(_utterance("这个多少钱", 298_000, 299_000))],
                [_response(_utterance("这个多少钱？", 0, 1_000))],
                [_response()],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert [s.text for s in result.segments] == ["这个多少钱"]

    def test_an_answer_at_a_boundary_is_retained_exactly_once(self, real_wav: Path) -> None:
        """The failure mode that matters: losing or doubling the line that answers a question.

        手抓饼。/ 多少钱？ falls just before the cut and 八元。 just after. All three must
        survive, in order, once each -- a deduplicator that dropped 八元。 as "already seen"
        would delete the answer and leave the question.
        """
        recorder = Recorder(
            [
                [
                    _response(
                        _utterance("手抓饼。", 298_000, 298_900),
                        _utterance("多少钱？", 299_000, 299_800),
                        # Heard in segment 1's trailing audio, owned by segment 2.
                        _utterance("八元。", 300_100, 300_900),
                    )
                ],
                [
                    _response(
                        _utterance("多少钱？", 0, 800),
                        _utterance("八元。", 1_100, 1_900),
                    )
                ],
                [_response()],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert [s.text for s in result.segments] == ["手抓饼。", "多少钱？", "八元。"]
        assert [s.start_ms for s in result.segments] == [298_000, 299_000, 300_100]


class TestOrderingAndFullText:
    def test_segments_are_sorted_on_the_global_timeline(self, real_wav: Path) -> None:
        recorder = Recorder(
            [
                [_response(_utterance("三。", 200_000, 201_000), _utterance("一。", 1_000, 2_000))],
                [_response(_utterance("四。", 10_000, 11_000))],
                [_response(_utterance("五。", 5_000, 6_000))],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        starts = [s.start_ms for s in result.segments]
        assert starts == sorted(starts)

    def test_full_text_is_the_reconciled_transcript(self, real_wav: Path) -> None:
        recorder = Recorder(
            [
                [_response(_utterance("前半句，", 298_000, 299_000))],
                [_response(_utterance("前半句，", 0, 1_000), _utterance("后半句。", 2_000, 3_000))],
                [_response()],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert result.full_text == "前半句，后半句。"

    def test_full_text_does_not_repeat_overlap_text(self, real_wav: Path) -> None:
        """Concatenating the provider's per-segment `text` fields would say it twice."""
        recorder = Recorder(
            [
                [_response(_utterance("重复的话。", 299_000, 299_900))],
                [_response(_utterance("重复的话。", 0, 900))],
                [_response()],
            ]
        )
        result = _provider(recorder).transcribe(str(real_wav))
        assert result.full_text == "重复的话。"

    def test_the_response_looks_like_any_other_asr_response(self, real_wav: Path) -> None:
        """No caller should be able to tell segmentation happened."""
        recorder = Recorder()
        result = _provider(recorder).transcribe(
            str(real_wav), language="zh-CN"
        )
        assert result.model == "doubao-seed-asr-2.0"
        assert result.language == "zh-CN"


class TestPerSegmentRetry:
    """Each segment is its own request, so each segment gets its own bounded retry.

    Both real failures were transient and differed from each other, which is the case retry
    is for. The budget is what stops one failing provider from pinning a worker.
    """

    def test_a_transient_segment_failure_succeeds_on_retry(self, real_wav: Path) -> None:
        calls: list[int] = []

        def transport(path: Path, _language: str | None) -> list[dict[str, object]]:
            calls.append(1)
            if len(calls) == 2:
                raise OSError("ping timeout")  # The attempt-1 shape: 1006 after a ping timeout.
            return [_response(_utterance("说了句话。", 1_000, 2_000))]

        result = _provider(transport).transcribe(
            str(real_wav)
        )
        # Three segments, one of which needed two attempts.
        assert len(calls) == 4
        assert len(result.segments) == 3

    def test_the_upstream_session_failure_is_retried(self, real_wav: Path) -> None:
        """Upstream 45000081 is the attempt-2 shape: a 1000 close carrying a session error."""
        calls: list[int] = []

        def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
            calls.append(1)
            if len(calls) == 1:
                return [
                    {
                        "code": 45000081,
                        "is_last_package": True,
                        "payload_msg": {
                            "message": "Timeout waiting next packet; session has ended"
                        },
                    }
                ]
            return [_response()]

        _provider(transport).transcribe(str(real_wav))
        assert len(calls) == 4

    def test_a_truncated_session_is_retried(self, real_wav: Path) -> None:
        """Results that stop before the final package: attempt 1 against the real source."""
        calls: list[int] = []

        def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
            calls.append(1)
            if len(calls) == 1:
                return [{"code": 0, "is_last_package": False, "payload_msg": None}]
            return [_response()]

        _provider(transport).transcribe(str(real_wav))
        assert len(calls) == 4

    def test_the_backoff_is_bounded_and_deterministic(self, real_wav: Path) -> None:
        slept: list[float] = []

        def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
            raise OSError("ping timeout")

        provider = _provider(transport, sleep=slept.append)
        with pytest.raises(ASRFailed):
            provider.transcribe(str(real_wav))
        # 2 s then 4 s, then it stops. No sleep after the last attempt.
        assert slept == [2.0, 4.0]

    def test_retries_are_bounded_per_segment(self, real_wav: Path) -> None:
        calls: list[int] = []

        def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
            calls.append(1)
            raise OSError("ping timeout")

        provider = _provider(transport, max_attempts=2)
        with pytest.raises(ASRFailed):
            provider.transcribe(str(real_wav))
        # Stops at the first segment's budget rather than working through the other two.
        assert len(calls) == 2


class TestPartialTranscriptsCannotMasqueradeAsSuccess:
    """A missing middle is invisible downstream, so it must not be returned."""

    def test_retry_exhaustion_on_one_segment_fails_the_whole_call(
        self, real_wav: Path
    ) -> None:
        def transport(path: Path, _language: str | None) -> list[dict[str, object]]:
            with wave.open(str(path), "rb") as audio:
                duration = int(audio.getnframes() * 1000 / audio.getframerate())
            if duration < 250_000:  # The short final segment: 241.8 s core + 1 s overlap.
                raise OSError("ping timeout")
            return [_response(_utterance("前面都成功了。", 1_000, 2_000))]

        provider = _provider(transport)
        with pytest.raises(ASRFailed):
            provider.transcribe(str(real_wav))

    def test_no_response_object_escapes_a_failed_segment(self, real_wav: Path) -> None:
        """Belt and braces: the raise must not be reachable around.

        Written as "nothing was returned" rather than "an exception was raised" because the
        dangerous regression is a well-meaning `except` that logs the segment and returns
        what it has -- 11 minutes of a 14-minute video, indistinguishable from a video that
        only says 11 minutes' worth.
        """
        calls: list[int] = []

        def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
            calls.append(1)
            if len(calls) == 1:
                return [_response(_utterance("第一段成功。", 1_000, 2_000))]
            raise OSError("ping timeout")

        returned = None
        with pytest.raises(ASRFailed):
            returned = _provider(transport).transcribe(
                str(real_wav)
            )
        assert returned is None


class TestNonTransientFailuresAreNotRetried:
    def test_authentication_failure_is_not_retried(self, real_wav: Path) -> None:
        """A rejected key will still be rejected in two seconds.

        Retrying it burns the budget and buries the one error message that says what to fix.
        """
        calls: list[int] = []

        def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
            calls.append(1)
            raise ConfigurationError("Doubao ASR authentication was rejected")

        with pytest.raises(ConfigurationError):
            _provider(transport).transcribe(str(real_wav))
        assert len(calls) == 1

    def test_a_malformed_response_is_not_retried(self, real_wav: Path) -> None:
        """Protocol errors are deterministic: three attempts is one failure, slower."""
        calls: list[int] = []

        def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
            calls.append(1)
            return [_response(_utterance("缺少时间戳。", 1_000, None))]  # type: ignore[arg-type]

        with pytest.raises(ASRFailed):
            _provider(transport).transcribe(str(real_wav))
        assert len(calls) == 1

    def test_a_bad_segment_policy_is_a_configuration_error(self, real_wav: Path) -> None:
        """Overlap as long as a segment cannot tile anything; that is a config bug, not ASR."""
        provider = _provider(Recorder(), overlap_s=SEGMENT_S)
        with pytest.raises(ConfigurationError):
            provider.transcribe(str(real_wav))


class TestDiagnosticsAreSafeAndSpecific:
    """The old adapter collapsed transport detail into one string plus an error type.

    That is what made the real diagnosis expensive: "which segment, how far in, which
    attempt, what did the socket say" all had to be reconstructed from log prose. These
    fields belong in `DKError.context`, structured.
    """

    def _failure(self, real_wav: Path) -> ASRFailed:
        class Closed(OSError):
            def __init__(self) -> None:
                super().__init__("no close frame received or sent")
                self.code = 1006
                self.reason = "keepalive ping timeout"

        def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
            raise Closed()

        with pytest.raises(ASRFailed) as info:
            _provider(transport).transcribe(str(real_wav))
        return info.value

    def test_the_failing_segment_is_identified(self, real_wav: Path) -> None:
        context = self._failure(real_wav).context
        assert context["segment_index"] == 0
        assert context["segment_start_ms"] == 0
        assert context["segment_end_ms"] == 300_000

    def test_the_attempt_count_is_recorded(self, real_wav: Path) -> None:
        context = self._failure(real_wav).context
        assert context["attempt"] == 3
        assert context["max_attempts"] == 3

    def test_the_websocket_close_code_and_reason_survive(self, real_wav: Path) -> None:
        """1006 + "keepalive ping timeout" is the whole of attempt 1's diagnosis."""
        context = self._failure(real_wav).context
        assert context["websocket_close_code"] == 1006
        assert context["websocket_close_reason"] == "keepalive ping timeout"

    def test_the_upstream_business_code_and_message_survive(self, real_wav: Path) -> None:
        """45000081 is what classified this as PROVIDER_SERVER_FAILURE rather than a local bug."""

        def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
            return [
                {
                    "code": 45000081,
                    "is_last_package": True,
                    "payload_msg": {"message": "Timeout waiting next packet; session has ended"},
                }
            ]

        with pytest.raises(ASRFailed) as info:
            _provider(transport).transcribe(str(real_wav))
        assert info.value.context["upstream_code"] == 45000081
        assert "Timeout waiting next packet" in info.value.context["upstream_message"]

    def test_no_secret_reaches_the_error_context(self, real_wav: Path) -> None:
        """SEC-001 where it is easiest to lose: an exception carrying the request that made it.

        The API key is a constructor argument and the auth header is on the failing request,
        so anything that serialised the exception wholesale would leak it into a log.
        """
        rendered = repr(self._failure(real_wav).to_dict())
        assert "not-a-real-key" not in rendered
        assert "X-Api-Key" not in rendered


class TestTheSegmentPlanIsDeterministic:
    """The plan is a pure function of duration and policy: no clock, no provider state."""

    def test_the_same_inputs_give_the_same_plan(self) -> None:
        first = plan_segments(REAL_DURATION_MS, segment_ms=300_000, overlap_ms=1_000)
        second = plan_segments(REAL_DURATION_MS, segment_ms=300_000, overlap_ms=1_000)
        assert first == second

    def test_core_intervals_tile_the_media_exactly(self) -> None:
        """No gap and no overlap between cores -- the property ownership depends on."""
        plan = plan_segments(REAL_DURATION_MS, segment_ms=300_000, overlap_ms=1_000)
        assert plan[0].core_start_ms == 0
        assert plan[-1].core_end_ms == REAL_DURATION_MS
        assert all(
            later.core_start_ms == earlier.core_end_ms
            for earlier, later in zip(plan, plan[1:], strict=False)
        )

    def test_every_millisecond_has_exactly_one_owner(self) -> None:
        """Stated as ownership rather than coverage, since coverage overlaps by design."""
        plan = plan_segments(REAL_DURATION_MS, segment_ms=300_000, overlap_ms=1_000)
        for moment in (0, 299_999, 300_000, 500_000, REAL_DURATION_MS - 1):
            owners = [
                s
                for s in plan
                if s.owns(moment, moment, is_last=s.index == len(plan) - 1)
            ]
            assert len(owners) == 1, moment

    def test_the_first_segment_has_no_leading_overlap(self) -> None:
        """There is no audio before zero to give it."""
        plan = plan_segments(REAL_DURATION_MS, segment_ms=300_000, overlap_ms=1_000)
        assert plan[0].coverage_start_ms == 0

    def test_the_last_segment_has_no_trailing_overlap(self) -> None:
        plan = plan_segments(REAL_DURATION_MS, segment_ms=300_000, overlap_ms=1_000)
        assert plan[-1].coverage_end_ms == REAL_DURATION_MS

    def test_a_sliver_remainder_is_absorbed_rather_than_sent_alone(self) -> None:
        """A 0.4 s session exists only as a rounding artefact of the segment length.

        Its core would be too short to own an utterance midpoint, so it would upload audio
        and contribute nothing.
        """
        plan = plan_segments(300_400, segment_ms=300_000, overlap_ms=1_000)
        assert len(plan) == 1
        assert plan[0].core_end_ms == 300_400

    def test_empty_audio_fails_rather_than_planning_nothing(self) -> None:
        with pytest.raises(ASRFailed):
            plan_segments(0, segment_ms=300_000, overlap_ms=1_000)

    def test_an_utterance_midpoint_on_a_boundary_goes_to_the_later_segment(self) -> None:
        """Half-open intervals, so a midpoint on the boundary is owned once, not twice."""
        plan = plan_segments(600_000, segment_ms=300_000, overlap_ms=1_000)
        first, second = plan
        assert not first.owns(299_000, 301_000, is_last=False)
        assert second.owns(299_000, 301_000, is_last=True)

    def test_a_midpoint_at_the_media_end_is_still_owned(self) -> None:
        """The final segment closes its interval; otherwise the last word can fall out."""
        plan = plan_segments(600_000, segment_ms=300_000, overlap_ms=1_000)
        assert plan[-1].owns(600_000, 600_000, is_last=True)


class TestAudioIsPreparedOnce:
    def test_the_original_media_is_transcoded_once_not_per_segment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-running ffmpeg per segment would cost minutes and reintroduce seek error.

        Counted at the conversion boundary rather than by timing: the reason to do this once
        is partly cost, but mostly that every segment's zero point must come from the same
        decode, or the offsets stop lining up with each other.
        """
        source = _wav(tmp_path / "prepared.wav", REAL_DURATION_MS)
        video = tmp_path / "video.mp4"
        video.write_bytes(b"not-really-media")

        conversions: list[int] = []
        provider = _provider(Recorder())

        @contextmanager
        def fake_prepared(_self: object, _src: Path) -> Iterator[Path]:
            conversions.append(1)
            yield source

        monkeypatch.setattr(
            DoubaoASRProvider, "_prepared_wav", fake_prepared, raising=True
        )
        provider.transcribe(str(video))
        assert conversions == [1]

    def test_slices_are_frame_exact(self, tmp_path: Path) -> None:
        """16 kHz mono makes ms -> frames exact, so offsets are exact rather than close.

        A slice one frame short per segment would drift the third segment's timestamps by a
        few ms -- individually invisible, and permanently wrong.
        """
        recorder = Recorder()
        _provider(recorder).transcribe(str(_wav(tmp_path / "a.wav", 700_000)))
        assert recorder.durations_ms == [301_000, 302_000, 101_000]


class TestTheSegmentTypeSaysWhatItMeans:
    def test_coverage_and_core_are_distinct_fields(self) -> None:
        """Collapsing them is the mistake the offset test above catches at runtime."""
        segment = AudioSegment(
            index=1,
            core_start_ms=300_000,
            core_end_ms=600_000,
            coverage_start_ms=299_000,
            coverage_end_ms=601_000,
        )
        assert segment.coverage_start_ms < segment.core_start_ms
        assert segment.coverage_end_ms > segment.core_end_ms



