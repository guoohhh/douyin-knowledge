"""Cross-session boundary stitching for segmented Doubao ASR.

Segmented transport is real-validated: the 841.721 s source split into three sessions,
all three succeeded with zero retries, and produced 151 segments spanning 0.680-837.590 s
with no timestamp reset and nothing out of range. What that run also exposed is the
invariant this module exists for:

    audio overlap != ASR utterance-boundary overlap

Midpoint ownership assumes each piece of speech belongs to one utterance whose position is
stable across sessions. It is not. The same audio gets grouped into utterances whose
boundaries move by seconds depending on how much context followed, so at each production
boundary two *different* utterances -- one owned by each neighbour, each legitimately, each
containing unique speech found nowhere else -- share a repeated phrase in the middle.

Both real boundaries from that run are reproduced below verbatim. Neither utterance
contains the other, which is why no whole-utterance winner rule can be correct: dropping
either side deletes real speech. The answer is to stitch the pair into one segment and
remove only the duplicated prefix.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from douyin_knowledge.ai.adapters import doubao_adapter
from douyin_knowledge.ai.adapters.doubao_adapter import (
    MIN_STITCH_OVERLAP_CHARS,
    DoubaoASRProvider,
)
from douyin_knowledge.ai.providers import ASRResponse, TranscriptSegment

#: The real prepared WAV's duration, which produced the 3-segment plan below.
REAL_DURATION_MS = 841_721

SEGMENT_S = 300.0
OVERLAP_S = 1.0

# The real production plan for that duration:
#   segment 0  core      0-300 000   coverage      0-301 000
#   segment 1  core 300 000-600 000  coverage 299 000-601 000
#   segment 2  core 600 000-841 721  coverage 599 000-841 721
COVERAGE_START_MS = (0, 299_000, 599_000)

# ----------------------------------------------------------------- real boundary at 300 s
# Repeated phrase: 到了十分价钱一分货 -- 9 normalized characters.
LEFT_300 = "而且我再说一遍一分价钱一分货十分价钱一分货到了十分价钱一分货"
LEFT_300_SPAN = (295_640, 300_880)
RIGHT_300 = (
    "到了十分价钱一分货的这个阶段你是否喜欢这个厨师的处理方式"
    "就非常看你跟这个人的趣味是不是相吻合了"
)
RIGHT_300_SPAN = (299_320, 308_400)
REPEAT_300 = "到了十分价钱一分货"
#: Only in the left utterance, so a "later session wins" rule would lose it.
UNIQUE_300_LEFT = "而且我再说一遍"
#: Only in the right utterance, so an "earlier session wins" rule would lose it.
UNIQUE_300_RIGHT = "是不是相吻合了"

# ----------------------------------------------------------------- real boundary at 600 s
# Repeated phrase: 家网红的猫头鹰 -- 7 normalized characters.
LEFT_600 = "这家店各方面都很均衡更侧重于这个鱼的本味也要也是在尖沙咀而且离那家网红的猫头鹰"
LEFT_600_SPAN = (593_190, 600_830)
RIGHT_600 = "家网红的猫头鹰泡芙非常的近"
RIGHT_600_SPAN = (599_080, 602_600)
REPEAT_600 = "家网红的猫头鹰"
UNIQUE_600_LEFT = "这家店各方面都很均衡"
UNIQUE_600_RIGHT = "泡芙非常的近"


def _wav(path: Path, duration_ms: int) -> Path:
    """A 16 kHz mono PCM WAV of exactly `duration_ms`. Silence: only timing is under test."""
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * (16 * duration_ms))
    return path


def _utterance(text: str, start_ms: int, end_ms: int, **extra: object) -> dict[str, object]:
    """One provider utterance. `speaker` is nested under `additions`, as Doubao sends it."""
    payload: dict[str, object] = {"text": text, "start_time": start_ms, "end_time": end_ms}
    speaker = extra.pop("speaker", None)
    if speaker is not None:
        payload["additions"] = {"speaker_id": speaker}
    payload.update(extra)
    return payload


def _global(span: tuple[int, int], origin: int) -> tuple[int, int]:
    """A real global span expressed session-locally, the way the provider reports it.

    The transport is handed a coverage slice, so it answers in coverage-relative time. The
    spans above are the real *global* values, so converting here keeps the test data
    readable as the evidence it came from rather than as pre-offset arithmetic.
    """
    start, end = span
    return start - COVERAGE_START_MS[origin], end - COVERAGE_START_MS[origin]


def _local(text: str, span: tuple[int, int], origin: int, **extra: object) -> dict[str, object]:
    start, end = _global(span, origin)
    return _utterance(text, start, end, **extra)


def _response(*utterances: dict[str, object]) -> dict[str, object]:
    return {
        "code": 0,
        "is_last_package": True,
        "payload_msg": {
            "result": {
                "text": "".join(str(u["text"]) for u in utterances),
                "utterances": list(utterances),
            }
        },
    }


def _scripted(replies: list[list[dict[str, object]]]) -> object:
    """A transport that answers the nth session with the nth scripted reply."""
    calls = 0

    def transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
        nonlocal calls
        reply = replies[min(calls, len(replies) - 1)]
        calls += 1
        return reply

    return transport


def _provider(replies: list[list[dict[str, object]]], **overrides: object) -> DoubaoASRProvider:
    kwargs: dict[str, object] = {
        "api_key": "not-a-real-key",
        "transport": _scripted(replies),
        "segment_s": SEGMENT_S,
        "overlap_s": OVERLAP_S,
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    return DoubaoASRProvider(**kwargs)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def real_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One shared 841.721 s WAV for the whole module.

    Session-scoped because it is 26.9 MB of silence and every test here wants the identical
    file: only the scripted transport replies differ. Writing it per test filled the disk and
    made pytest's own tmp_path allocation fail, which surfaces as unrelated errors in other
    modules rather than as anything pointing here.
    """
    return _wav(tmp_path_factory.mktemp("boundary") / "real.wav", REAL_DURATION_MS)


def _transcribe(
    real_wav: Path, replies: list[list[dict[str, object]]], **overrides: object
) -> ASRResponse:
    return _provider(replies, **overrides).transcribe(str(real_wav))


def _three_sessions(
    seg0: list[dict[str, object]],
    seg1: list[dict[str, object]],
    seg2: list[dict[str, object]],
) -> list[list[dict[str, object]]]:
    return [[_response(*seg0)], [_response(*seg1)], [_response(*seg2)]]


#: A filler utterance safely inside a core interval, so a test can prove the ordinary path
#: is untouched without that filler ever being a stitch candidate.
def _filler(text: str, start_ms: int, origin: int) -> dict[str, object]:
    return _local(text, (start_ms, start_ms + 2_000), origin)


def _owned(
    text: str, start_ms: int, end_ms: int, origin: int
) -> doubao_adapter._OwnedUtterance:
    """An already-owned utterance, for the one invariant no real transcript can produce."""
    return doubao_adapter._OwnedUtterance(
        TranscriptSegment(text=text, start_ms=start_ms, end_ms=end_ms), origin
    )


def _overlap(
    left: doubao_adapter._OwnedUtterance, right: doubao_adapter._OwnedUtterance
) -> int:
    left_norm, _ = doubao_adapter._normalize(left.text)
    right_norm, _ = doubao_adapter._normalize(right.text)
    return doubao_adapter._overlap_chars(left_norm, right_norm)


def _texts(response: ASRResponse) -> list[str]:
    return [segment.text for segment in response.segments]


def _spans(response: ASRResponse) -> list[tuple[int, int]]:
    return [(segment.start_ms, segment.end_ms) for segment in response.segments]


def _real_300_boundary() -> list[list[dict[str, object]]]:
    """The 300 s boundary exactly as the real run produced it."""
    return _three_sessions(
        [_filler("先说一下这家店的位置", 120_000, 0), _local(LEFT_300, LEFT_300_SPAN, 0)],
        [_local(RIGHT_300, RIGHT_300_SPAN, 1)],
        [_filler("最后总结一下", 700_000, 2)],
    )


def _real_600_boundary() -> list[list[dict[str, object]]]:
    """The 600 s boundary exactly as the real run produced it."""
    return _three_sessions(
        [_filler("先说一下这家店的位置", 120_000, 0)],
        [_local(LEFT_600, LEFT_600_SPAN, 1)],
        [_local(RIGHT_600, RIGHT_600_SPAN, 2)],
    )


class TestTheReal300SecondBoundary:
    """295.640-300.880 against 299.320-308.400, sharing 到了十分价钱一分货."""

    def test_the_repeated_phrase_appears_once(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, _real_300_boundary())
        assert response.full_text.count(REPEAT_300) == 1

    def test_the_left_unique_speech_survives(self, real_wav: Path) -> None:
        """A later-session-wins rule would delete this. It is real speech."""
        response = _transcribe(real_wav, _real_300_boundary())
        assert UNIQUE_300_LEFT in response.full_text

    def test_the_right_unique_speech_survives(self, real_wav: Path) -> None:
        """An earlier-session-wins rule would delete this. It is also real speech."""
        response = _transcribe(real_wav, _real_300_boundary())
        assert UNIQUE_300_RIGHT in response.full_text

    def test_the_pair_becomes_one_segment(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, _real_300_boundary())
        stitched = [text for text in _texts(response) if REPEAT_300 in text]
        assert len(stitched) == 1

    def test_the_stitched_text_is_left_plus_the_unduplicated_right(
        self, real_wav: Path
    ) -> None:
        response = _transcribe(real_wav, _real_300_boundary())
        expected = LEFT_300 + RIGHT_300[len(REPEAT_300) :]
        assert expected in _texts(response)

    def test_the_span_is_the_union_of_the_two_real_intervals(self, real_wav: Path) -> None:
        """295.640-308.400: no invented timestamp for a trimmed fragment."""
        response = _transcribe(real_wav, _real_300_boundary())
        assert (LEFT_300_SPAN[0], RIGHT_300_SPAN[1]) in _spans(response)


class TestTheReal600SecondBoundary:
    """593.190-600.830 against 599.080-602.600, sharing 家网红的猫头鹰 -- exactly 7 chars."""

    def test_the_repeated_phrase_appears_once(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, _real_600_boundary())
        assert response.full_text.count(REPEAT_600) == 1

    def test_both_unique_sides_survive(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, _real_600_boundary())
        assert UNIQUE_600_LEFT in response.full_text
        assert UNIQUE_600_RIGHT in response.full_text

    def test_the_stitched_text_is_left_plus_the_unduplicated_right(
        self, real_wav: Path
    ) -> None:
        response = _transcribe(real_wav, _real_600_boundary())
        expected = LEFT_600 + RIGHT_600[len(REPEAT_600) :]
        assert expected in _texts(response)

    def test_the_span_is_the_union_of_the_two_real_intervals(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, _real_600_boundary())
        assert (LEFT_600_SPAN[0], RIGHT_600_SPAN[1]) in _spans(response)

    def test_a_seven_character_overlap_is_exactly_at_the_threshold(self) -> None:
        """The real 600 s evidence is what fixes the threshold, so it must not exclude it."""
        assert len(REPEAT_600) == MIN_STITCH_OVERLAP_CHARS


class TestStitchingIsBoundaryLocal:
    """The stitcher is not a deduplicator. It may only look where overlap is produced."""

    def test_neighbours_from_the_same_session_are_never_stitched(
        self, real_wav: Path
    ) -> None:
        """Two ordinary consecutive utterances that happen to share a phrase.

        Same session means the provider already decided they are two utterances. There is
        no duplicated audio here, so a shared phrase is just someone repeating themselves,
        and merging it would delete speech that was really said twice.

        The intervals deliberately intersect, so same-session origin is the *only* thing
        disqualifying this pair. A non-intersecting pair would pass this test for the wrong
        reason.
        """
        replies = _three_sessions(
            [
                _local(LEFT_600, (100_000, 107_640), 0),
                _local(RIGHT_600, (106_000, 110_000), 0),
            ],
            [_filler("中间的内容", 400_000, 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        assert LEFT_600 in _texts(response)
        assert RIGHT_600 in _texts(response)
        assert response.full_text.count(REPEAT_600) == 2

    def test_non_adjacent_sessions_are_never_stitched(self, real_wav: Path) -> None:
        """Minute 2 cannot stitch to minute 11 merely because a phrase recurs.

        Sessions 0 and 2 share no audio at all, so any text they share is coincidence or
        genuine repetition -- never the artefact this pass exists to remove.

        Non-adjacent sessions also cannot produce intersecting owned intervals, so the time
        rule already covers this. The test stays as a guard on that reasoning: if someone
        widens the candidate window later, adjacency has to keep holding the line.
        """
        replies = _three_sessions(
            [_local(LEFT_600, (100_000, 107_640), 0)],
            [_filler("中间的内容", 400_000, 1)],
            [_local(RIGHT_600, (700_000, 703_520), 2)],
        )
        response = _transcribe(real_wav, replies)
        assert response.full_text.count(REPEAT_600) == 2

    def test_an_identical_phrase_far_from_the_boundary_is_never_stitched(
        self, real_wav: Path
    ) -> None:
        """Adjacent sessions, but the two utterances are minutes apart in global time."""
        replies = _three_sessions(
            [_local(LEFT_600, (100_000, 107_640), 0)],
            [_local(RIGHT_600, (450_000, 453_520), 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        assert response.full_text.count(REPEAT_600) == 2


class TestConservativeRefusals:
    """Unresolved duplication beats deleting real speech. These cases must not stitch."""

    def test_time_disjoint_utterances_are_not_stitched(self, real_wav: Path) -> None:
        """A real suffix/prefix match, adjacent sessions, near the cut -- but no intersection.

        Without intersecting audio the shared phrase was not transcribed twice; it was said
        twice. Dropping one copy would be deleting speech.
        """
        replies = _three_sessions(
            [_filler("先说一下这家店的位置", 120_000, 0), _local(LEFT_600, (293_000, 298_000), 0)],
            [_local(RIGHT_600, (300_500, 304_020), 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        assert response.full_text.count(REPEAT_600) == 2

    def test_an_overlap_below_the_threshold_is_left_alone(self, real_wav: Path) -> None:
        """Six shared characters. Short matches are common words, not proof of duplication."""
        short = "非常的近"
        replies = _three_sessions(
            [_filler("先说一下这家店的位置", 120_000, 0), _local("这家店" + short, (296_000, 300_400), 0)],
            [_local(short + "走过去就到了", RIGHT_300_SPAN, 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        assert len(short) < MIN_STITCH_OVERLAP_CHARS
        assert response.full_text.count(short) == 2

    def test_ambiguous_candidates_are_left_untouched(self, real_wav: Path) -> None:
        """Two right-hand utterances both match the same left one.

        Picking one would be a guess, and a wrong guess deletes speech. Refusing leaves
        visible duplication, which a human can see and a later pass can revisit.
        """
        replies = _three_sessions(
            [_filler("先说一下这家店的位置", 120_000, 0)],
            [_local(LEFT_600, LEFT_600_SPAN, 1)],
            [
                _local(RIGHT_600, (599_080, 602_600), 2),
                _local(REPEAT_600 + "就在旁边", (599_200, 603_000), 2),
            ],
        )
        response = _transcribe(real_wav, replies)
        assert LEFT_600 in _texts(response)
        assert RIGHT_600 in _texts(response)


class TestNormalizationDoesNotCorruptTheText:
    """Matching may ignore punctuation. What comes back must still read correctly."""

    def test_punctuation_differences_still_match(self, real_wav: Path) -> None:
        """The provider punctuates the same phrase differently on each side of the cut.

        This is the normal case, not an edge case: with less following context the recogniser
        commits to different punctuation, which is exactly why raw equality is not enough.
        """
        left = "而且我再说一遍，一分价钱一分货。到了十分价钱一分货"
        right = "到了十分价钱，一分货的这个阶段，你是否喜欢"
        replies = _three_sessions(
            [_filler("先说一下这家店的位置", 120_000, 0), _local(left, LEFT_300_SPAN, 0)],
            [_local(right, RIGHT_300_SPAN, 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        # The seam itself: the differently-punctuated repeat must be gone from the right
        # side, and the right side's own text must survive intact after it.
        assert left + "的这个阶段，你是否喜欢" in _texts(response)
        assert "到了十分价钱" not in response.full_text.replace(left, "", 1)

    def test_the_kept_text_is_not_sliced_by_normalized_length(self, real_wav: Path) -> None:
        """The right side's punctuation makes raw and normalized lengths differ.

        Slicing the raw string by a normalized character count is the obvious shortcut and
        it silently eats real characters. Here the normalized overlap is 9 characters but the
        raw prefix is 10, so a count-based slice would leave a stray 货 at the front.
        """
        left = "而且我再说一遍到了十分价钱一分货"
        right = "到了十分价钱，一分货的这个阶段"
        replies = _three_sessions(
            [_filler("先说一下这家店的位置", 120_000, 0), _local(left, LEFT_300_SPAN, 0)],
            [_local(right, RIGHT_300_SPAN, 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        assert left + "的这个阶段" in _texts(response)

    def test_whitespace_around_the_seam_does_not_block_a_match(self, real_wav: Path) -> None:
        left = "而且我再说一遍 到了十分价钱一分货"
        right = " 到了十分价钱一分货 的这个阶段"
        replies = _three_sessions(
            [_filler("先说一下这家店的位置", 120_000, 0), _local(left, LEFT_300_SPAN, 0)],
            [_local(right, RIGHT_300_SPAN, 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        assert response.full_text.count("到了十分价钱一分货") == 1
        assert "的这个阶段" in response.full_text


class TestTheRestOfTheTranscriptIsUnaffected:
    def test_ordinary_utterances_pass_through_untouched(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, _real_300_boundary())
        assert "先说一下这家店的位置" in _texts(response)
        assert "最后总结一下" in _texts(response)

    def test_segments_stay_ordered_after_a_stitch(self, real_wav: Path) -> None:
        """A merged segment starts earlier than its right-hand half did."""
        response = _transcribe(real_wav, _real_300_boundary())
        starts = [segment.start_ms for segment in response.segments]
        assert starts == sorted(starts)

    def test_no_timestamp_resets_to_zero_mid_transcript(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, _real_600_boundary())
        assert all(segment.start_ms > 0 for segment in response.segments[1:])

    def test_no_timestamp_exceeds_the_media_duration(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, _real_600_boundary())
        assert all(segment.end_ms <= REAL_DURATION_MS for segment in response.segments)

    def test_full_text_matches_the_stitched_segments(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, _real_300_boundary())
        assert response.full_text == "".join(_texts(response))


class TestMergedMetadataIsConservative:
    """A stitched segment must not claim more than both halves agreed on."""

    def test_an_agreed_speaker_is_preserved(self, real_wav: Path) -> None:
        replies = _three_sessions(
            [
                _filler("先说一下这家店的位置", 120_000, 0),
                _local(LEFT_300, LEFT_300_SPAN, 0, speaker="spk_1"),
            ],
            [_local(RIGHT_300, RIGHT_300_SPAN, 1, speaker="spk_1")],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        merged = next(s for s in response.segments if REPEAT_300 in s.text)
        assert merged.speaker_id == "spk_1"

    def test_a_disputed_speaker_becomes_none(self, real_wav: Path) -> None:
        """Two sessions disagreeing about who spoke is not a basis for picking one."""
        replies = _three_sessions(
            [
                _filler("先说一下这家店的位置", 120_000, 0),
                _local(LEFT_300, LEFT_300_SPAN, 0, speaker="spk_1"),
            ],
            [_local(RIGHT_300, RIGHT_300_SPAN, 1, speaker="spk_2")],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        merged = next(s for s in response.segments if REPEAT_300 in s.text)
        assert merged.speaker_id is None

    def test_confidence_is_the_lower_of_the_two(self, real_wav: Path) -> None:
        """The merged text spans both halves, so it is only as trustworthy as the weaker."""
        replies = _three_sessions(
            [
                _filler("先说一下这家店的位置", 120_000, 0),
                _local(LEFT_300, LEFT_300_SPAN, 0, confidence=0.91),
            ],
            [_local(RIGHT_300, RIGHT_300_SPAN, 1, confidence=0.72)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        merged = next(s for s in response.segments if REPEAT_300 in s.text)
        assert merged.confidence == 0.72

    def test_a_missing_confidence_does_not_become_a_number(self, real_wav: Path) -> None:
        replies = _three_sessions(
            [
                _filler("先说一下这家店的位置", 120_000, 0),
                _local(LEFT_300, LEFT_300_SPAN, 0, confidence=0.91),
            ],
            [_local(RIGHT_300, RIGHT_300_SPAN, 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        merged = next(s for s in response.segments if REPEAT_300 in s.text)
        assert merged.confidence is None


class TestTheRightSideMayStartEarlier:
    """A stitch must not depend on which half happens to sort first.

    Nothing guarantees the left-session utterance starts earlier globally. Ownership is
    decided by midpoint, not by start, so a right-hand utterance that opens *before* the
    boundary and runs past it can start earlier than the left-hand one while both are still
    owned correctly:

        left  / origin 0   299.500-300.400   midpoint 299.950  -> segment 0's core
        right / origin 1   299.000-301.100   midpoint 300.050  -> segment 1's core

    Global sorting then puts `right` first, and a pass that emits as it walks has already
    published `right` by the time it reaches `left` and discovers the pair.
    """

    #: Shares 到了十分价钱一分货 with RIGHT_300, so the existing exact rule matches.
    LEFT = "而且我再说一遍到了十分价钱一分货"
    LEFT_SPAN = (299_500, 300_400)
    RIGHT = "到了十分价钱一分货的这个阶段你是否喜欢"
    RIGHT_SPAN = (299_000, 301_100)
    UNIQUE_LEFT = "而且我再说一遍"
    UNIQUE_RIGHT = "的这个阶段你是否喜欢"

    def _replies(self) -> list[list[dict[str, object]]]:
        return _three_sessions(
            [_filler("先说一下这家店的位置", 120_000, 0), _local(self.LEFT, self.LEFT_SPAN, 0)],
            [_local(self.RIGHT, self.RIGHT_SPAN, 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )

    def test_the_right_half_really_does_sort_first(self, real_wav: Path) -> None:
        """Guards the premise: if this stops holding the test below proves nothing."""
        assert self.RIGHT_SPAN[0] < self.LEFT_SPAN[0]
        left_mid = sum(self.LEFT_SPAN) // 2
        right_mid = sum(self.RIGHT_SPAN) // 2
        assert left_mid < 300_000 <= right_mid

    def test_the_unstitched_right_half_is_not_also_emitted(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, self._replies())
        assert self.RIGHT not in _texts(response)

    def test_only_one_stitched_segment_results(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, self._replies())
        touching = [text for text in _texts(response) if REPEAT_300 in text]
        assert len(touching) == 1

    def test_the_repeated_phrase_occurs_once(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, self._replies())
        assert response.full_text.count(REPEAT_300) == 1

    def test_both_unique_sides_survive(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, self._replies())
        assert self.UNIQUE_LEFT in response.full_text
        assert self.UNIQUE_RIGHT in response.full_text

    def test_the_span_is_the_union(self, real_wav: Path) -> None:
        """Here the right half supplies *both* ends: it opens earlier and closes later.

        299.000-301.100, not 299.500-300.400. The left half is entirely inside it, which is
        exactly the shape that breaks any rule keyed on which utterance came first.
        """
        response = _transcribe(real_wav, self._replies())
        expected = (
            min(self.LEFT_SPAN[0], self.RIGHT_SPAN[0]),
            max(self.LEFT_SPAN[1], self.RIGHT_SPAN[1]),
        )
        assert expected == (self.RIGHT_SPAN[0], self.RIGHT_SPAN[1])
        assert expected in _spans(response)

    def test_the_segment_count_drops_by_exactly_one(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, self._replies())
        assert len(response.segments) == 3


class TestPairMembershipIsExclusive:
    """An index may take part in at most one accepted pair, in any role."""

    def test_a_shared_right_half_leaves_everything_unresolved(self, real_wav: Path) -> None:
        """Two left utterances both matching one right utterance.

        Already covered from the left side; asserted here from the right to pin that
        exclusivity is about pair *membership*, not about a dict key happening to be unique.
        """
        first = "我再说一遍到了十分价钱一分货"
        second = "换个说法到了十分价钱一分货"
        replies = _three_sessions(
            [
                _local(first, (296_000, 300_200), 0),
                _local(second, (297_000, 300_400), 0),
            ],
            [_local(RIGHT_300, RIGHT_300_SPAN, 1)],
            [_filler("最后总结一下", 700_000, 2)],
        )
        response = _transcribe(real_wav, replies)
        assert first in _texts(response)
        assert second in _texts(response)
        assert RIGHT_300 in _texts(response)

    def test_an_index_that_is_both_a_left_and_a_right_blocks_both_pairs(self) -> None:
        """A chain A-B-C, where B is B(right of A) and B(left of C).

        Tested against `_stitch_candidates` rather than through `transcribe()` because this
        shape is not reachable from a plausible transcript: to be a right at one seam and a
        left at the next, B would have to intersect both, i.e. span ~300 s as a single
        utterance. It is guarded anyway because counting the two roles separately accepts it
        -- B appears once as a left and once as a right -- and then which pair wins is
        iteration order, with B's text merged into a segment it only half belongs to.
        """
        a = _owned("我再说一遍到了十分价钱一分货", 296_000, 300_200, 0)
        b = _owned("到了十分价钱一分货家网红的猫头鹰", 299_000, 601_000, 1)
        c = _owned("家网红的猫头鹰泡芙非常的近", 599_080, 602_600, 2)
        assert _overlap(a, b) >= MIN_STITCH_OVERLAP_CHARS
        assert _overlap(b, c) >= MIN_STITCH_OVERLAP_CHARS
        assert doubao_adapter._stitch_candidates([a, b, c]) == {}


class TestAtRealTranscriptScale:
    """The real run returned 151 segments. Nothing should stitch except at the two seams."""

    def _dense(self) -> list[list[dict[str, object]]]:
        """Filler across all three sessions, with one phrase deliberately recurring.

        Natural speech repeats itself constantly, which is the risk a suffix/prefix rule
        runs: at 151 segments there are thousands of pairs, and a rule that is only mostly
        boundary-local will eventually merge two of them.
        """
        recurring = "这家店各方面都很均衡"
        sessions: list[list[dict[str, object]]] = [[], [], []]
        for origin, (low, high) in enumerate(((5_000, 295_000), (305_000, 595_000), (605_000, 835_000))):
            start = low
            while start < high:
                text = recurring if (start // 5_000) % 4 == 0 else f"第{start // 1_000}秒的内容"
                sessions[origin].append(_local(text, (start, start + 3_000), origin))
                start += 5_000
        return [[_response(*session)] for session in sessions]

    def _dense_with_real_seams(self) -> list[list[dict[str, object]]]:
        """The dense filler plus both real boundary pairs, at their real timestamps."""
        replies = self._dense()
        seams = (
            (0, LEFT_300, LEFT_300_SPAN),
            (1, RIGHT_300, RIGHT_300_SPAN),
            (1, LEFT_600, LEFT_600_SPAN),
            (2, RIGHT_600, RIGHT_600_SPAN),
        )
        for origin, text, span in seams:
            _response_utterances(replies[origin]).append(_local(text, span, origin))
        return replies

    def test_exactly_the_two_real_seams_merge_and_nothing_else(
        self, real_wav: Path
    ) -> None:
        """Two merges out of 166 utterances, with a phrase recurring dozens of times.

        Measured while writing this: of all pairs in this transcript only 2 clear adjacency,
        intersection and drift -- the two real seams. The temporal gate, not the text rule,
        is what makes this safe at scale, which is the intended design: text matching is the
        last check on an already tiny candidate set, never a search over the transcript. The
        text rule's own rejections are covered by the threshold and ambiguity tests above.
        """
        replies = self._dense_with_real_seams()
        sent = sum(len(_response_utterances(reply)) for reply in replies)
        response = _transcribe(real_wav, replies)
        assert len(response.segments) == sent - 2

    def test_both_seams_still_resolve_at_scale(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, self._dense_with_real_seams())
        assert response.full_text.count(REPEAT_300) == 1
        assert response.full_text.count(REPEAT_600) == 1

    def test_the_recurring_filler_phrase_is_never_collapsed(self, real_wav: Path) -> None:
        """It recurs dozens of times legitimately. All of them must survive."""
        replies = self._dense_with_real_seams()
        recurring = "这家店各方面都很均衡"
        sent = sum(
            1
            for reply in replies
            for utterance in _response_utterances(reply)
            if isinstance(utterance, dict) and utterance["text"] == recurring
        )
        response = _transcribe(real_wav, replies)
        assert sent > 20
        # +1 because LEFT_600 opens with the same phrase and survives inside its stitch.
        assert response.full_text.count(recurring) == sent + 1

    def test_timestamps_stay_monotonic_at_scale(self, real_wav: Path) -> None:
        response = _transcribe(real_wav, self._dense_with_real_seams())
        starts = [segment.start_ms for segment in response.segments]
        assert starts == sorted(starts)
        assert all(0 <= s.start_ms <= s.end_ms <= REAL_DURATION_MS for s in response.segments)


def _response_utterances(reply: list[dict[str, object]]) -> list[object]:
    payload = reply[0]["payload_msg"]
    assert isinstance(payload, dict)
    result = payload["result"]
    assert isinstance(result, dict)
    utterances = result["utterances"]
    assert isinstance(utterances, list)
    return utterances
