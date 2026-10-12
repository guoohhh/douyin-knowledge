"""Doubao Seed-ASR 2.0 adapter.

The adapter keeps the local-first boundary intact: it streams bytes from a local
file directly to Volcengine's WebSocket API.  The API doesn't accept MP4, so video
inputs are converted to a temporary 16 kHz mono WAV with ffmpeg.  Nothing is
uploaded to object storage and the API key is never included in an error message.

Long media is transcribed as several bounded WebSocket sessions rather than one long
one, because one long one does not reliably finish.  Measured against a real 841.8 s
source (``src_a0e0631c136e141dff7008``, healthy H.264/AAC MP4 that ffmpeg decodes
through EOF), controlled probes at 180/300/480/600 s all succeeded and the full
duration failed twice, differently each time:

* attempt 1 -- audio sent to ~216.8 s, results received to ~167.45 s, WebSocket 1006,
  keepalive ping timeout;
* attempt 2 -- all audio sent, last result at ~744.44 s, WebSocket 1000 with upstream
  ``45000081`` "Timeout waiting next packet; session has ended".

So the failure is a provider-side session failure with a transport failure secondary to
it, and it is *not* deterministic.  Nothing here should be read as "600 seconds is the
Doubao limit": no such documented limit was found, and the successful 600 s probe is
evidence about one run, not a boundary.  `DEFAULT_SEGMENT_S` is an operational
reliability choice with margin under the shortest duration that has actually been
observed to fail, and it is configurable precisely because it is a guess about a
provider's behaviour rather than a fact about its contract.

Segmentation is a provider-adapter concern and stays here.  `transcribe()` returns one
ordinary `ASRResponse` whose timestamps are relative to the original media, so no
caller can tell that it happened -- see `_reconcile`.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import wave
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from douyin_knowledge.ai.providers import ASRResponse, TranscriptSegment
from douyin_knowledge.core.errors import ASRFailed, ConfigurationError
from douyin_knowledge.observability.logging import get_logger

logger = get_logger(__name__)

DOUBAO_SEED_ASR_2_MODEL = "doubao-seed-asr-2.0"
DEFAULT_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"

DEFAULT_SEGMENT_S = 300.0
"""Core audio duration per provider session.

Operational, not contractual. A 300 s probe against the real failing source succeeded
with substantial margin, and the shortest duration observed to fail is 841.8 s. Lower it
if sessions start failing; raising it trades reliability for fewer sessions.
"""

DEFAULT_OVERLAP_S = 1.0
"""Extra audio each session hears on either side of the audio it owns.

Speech does not stop at arithmetic boundaries. Without this, a segment cut mid-word gives
the recogniser half a syllable of context and it guesses -- and the word that gets
mangled is the one at the boundary, which is as likely to be the answer to a question as
anything else. One second is the narrowest value the boundary tests hold at.

An earlier version of this note claimed the overlap is context only and never contributes
a returned utterance. The real 841.7 s run disproved that: the provider regroups speech
into different utterances depending on how much context follows, so at each boundary both
neighbours return an utterance covering the seam, each containing unique speech. See
`MIN_STITCH_OVERLAP_CHARS` and `_stitch_boundaries`.
"""

MIN_STITCH_OVERLAP_CHARS = 7
"""Normalized characters two boundary utterances must share before they are stitched.

A conservative operational threshold, not a linguistic guarantee. It is set by the real
evidence: the 600 s boundary of the validated run repeated exactly 7 characters
(家网红的猫头鹰) and the 300 s boundary repeated 9 (到了十分价钱一分货), so 7 is the
narrowest value that resolves both real cases.

Shorter matches are not reliable evidence of duplication -- a few common characters recur
constantly in speech -- and the cost of being wrong is asymmetric. Failing to stitch leaves
visible duplication that a reader can see and a later pass can revisit; stitching wrongly
deletes speech that was really said, silently. So this biases toward false negatives, and
should only be lowered against real corpus data showing missed duplicates.
"""

_MAX_BOUNDARY_DRIFT_MS = 5_000
"""How far from a production boundary a stitch candidate may sit.

Audio overlap is 1 s, but utterance boundaries drift further than the audio does: the real
pairs intersected by 1.560 s and 1.750 s while individual utterances extended up to 8 s
past the cut. This bounds the search to the seam without assuming the drift equals the
overlap. Intersection and ownership already do most of the narrowing; this is the backstop
that keeps "near the boundary" from quietly meaning "anywhere".
"""

DEFAULT_MAX_ATTEMPTS = 3
"""Attempts per segment, first try included.

Both observed failures were transient, so a retry is the right response; three bounds it.
Unbounded retry against a provider that has started failing is how one stuck video
consumes a worker forever.
"""

DEFAULT_RETRY_BACKOFF_S = 2.0
"""Base for the deterministic backoff between attempts: 2 s, then 4 s.

No jitter. One local worker retrying one segment is not a thundering herd, and a
deterministic schedule is one a test can assert on.
"""

_FRAMES_PER_MS = 16  # 16 kHz mono: ms -> frames is exact, so slices land on frames.

_FULL_CLIENT_REQUEST = 0x1
_AUDIO_ONLY_REQUEST = 0x2
_FULL_SERVER_RESPONSE = 0x9
_SERVER_ERROR_RESPONSE = 0xF
_POSITIVE_SEQUENCE = 0x1
_NEGATIVE_WITH_SEQUENCE = 0x3
_JSON_SERIALIZATION = 0x1
_GZIP_COMPRESSION = 0x1

Response = dict[str, Any]
Transport = Callable[[Path, str | None], list[Response]]


class DoubaoProtocolError(ASRFailed):
    """A response that did not match the protocol.

    Still an `ASRFailed`, so every existing caller and test that catches or asserts on that
    keeps working. The subclass exists only to mark "retrying this will produce the identical
    failure": a truncated frame or a missing timestamp is a deterministic property of the
    bytes received, unlike a 1006 close, which is a property of the moment.
    """


@dataclass(frozen=True)
class AudioSegment:
    """One provider session's worth of audio: what it hears, and what it owns.

    Two intervals, deliberately distinct:

    ``[coverage_start_ms, coverage_end_ms)`` is the audio actually sent, overlap included.
    Provider timestamps come back relative to ``coverage_start_ms``, because that is where
    the audio it received began.

    ``[core_start_ms, core_end_ms)`` is the audio this segment is *responsible for*. Core
    intervals tile the media exactly -- no gaps, no overlap -- which is what makes overlap
    reconciliation a matter of arithmetic instead of text comparison: an utterance belongs
    to whichever core interval contains its midpoint, and exactly one does.
    """

    index: int
    core_start_ms: int
    core_end_ms: int
    coverage_start_ms: int
    coverage_end_ms: int

    def owns(self, start_ms: int, end_ms: int, *, is_last: bool) -> bool:
        """Whether this segment owns an utterance, by midpoint.

        Midpoint, not start: an utterance that begins 200 ms before a boundary and ends two
        seconds after it is mostly in the next segment, and both segments heard it. Start
        would award it to the segment that caught its first syllable.

        Half-open on the right, so a midpoint landing exactly on a boundary goes to the
        later segment and never to both. The final segment closes its interval, because a
        midpoint can legitimately equal the media duration.
        """
        midpoint = (start_ms + end_ms) // 2
        if midpoint < self.core_start_ms:
            return False
        return midpoint <= self.core_end_ms if is_last else midpoint < self.core_end_ms


def plan_segments(duration_ms: int, *, segment_ms: int, overlap_ms: int) -> list[AudioSegment]:
    """Tile `duration_ms` into segments of `segment_ms` core audio with `overlap_ms` context.

    Deterministic in its arguments alone -- no clock, no randomness, no provider state -- so
    the same media always produces the same sessions and the same timestamps.

    A trailing remainder shorter than the overlap is folded into the previous segment rather
    than sent as its own session: a 0.4 s session is a session that exists to be a rounding
    artefact, and its core interval would be too short to own an utterance midpoint anyway.
    """
    if duration_ms <= 0:
        raise ASRFailed("Doubao ASR cannot segment empty audio", provider="doubao")
    if segment_ms <= 0:
        raise ConfigurationError("doubao ASR segment duration must be positive")
    if overlap_ms < 0 or overlap_ms >= segment_ms:
        raise ConfigurationError("doubao ASR overlap must be shorter than one segment")

    boundaries: list[int] = list(range(0, duration_ms, segment_ms))
    if len(boundaries) > 1 and duration_ms - boundaries[-1] <= overlap_ms:
        boundaries.pop()

    segments: list[AudioSegment] = []
    for index, core_start in enumerate(boundaries):
        is_last = index == len(boundaries) - 1
        core_end = duration_ms if is_last else boundaries[index + 1]
        segments.append(
            AudioSegment(
                index=index,
                core_start_ms=core_start,
                core_end_ms=core_end,
                coverage_start_ms=max(0, core_start - overlap_ms),
                coverage_end_ms=min(duration_ms, core_end + overlap_ms),
            )
        )
    return segments


class DoubaoASRProvider:
    """Transcribe local media with Volcengine Doubao Seed-ASR 2.0."""

    def __init__(
        self,
        *,
        api_key: str,
        resource_id: str = DEFAULT_RESOURCE_ID,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_s: float = 300.0,
        ffmpeg_path: str = "ffmpeg",
        transport: Transport | None = None,
        segment_s: float = DEFAULT_SEGMENT_S,
        overlap_s: float = DEFAULT_OVERLAP_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ConfigurationError("doubao ASR requires DK_DOUBAO_ASR_API_KEY")
        if max_attempts < 1:
            raise ConfigurationError("doubao ASR requires at least one attempt per segment")
        self.model = DOUBAO_SEED_ASR_2_MODEL
        self._api_key = api_key
        self.resource_id = resource_id
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.ffmpeg_path = ffmpeg_path
        self._transport = transport or self._transcribe_over_websocket
        self.segment_ms = int(segment_s * 1000)
        self.overlap_ms = int(overlap_s * 1000)
        self.max_attempts = max_attempts
        self.retry_backoff_s = retry_backoff_s
        # Injected so retry tests do not spend the backoff. Bounded retry that a test cannot
        # run through is bounded retry nobody checks.
        self._sleep = sleep

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        timestamp_granularity: str = "segment",
    ) -> ASRResponse:
        """Transcribe a local audio/video file and normalize utterance timestamps."""
        del timestamp_granularity  # Seed-ASR returns utterance timestamps when enabled.
        source = Path(audio_path)
        if not source.is_file():
            raise ConfigurationError(f"Audio file not found: {audio_path}")

        with self._prepared_wav(source) as wav_path:
            duration_ms = _wav_duration_ms(wav_path)
            if duration_ms is None or duration_ms <= self.segment_ms:
                # Short media keeps the original single-session path byte for byte. Most
                # sources are seconds long, one session is what the provider handles
                # reliably at that length, and routing a 20 s clip through slicing and
                # reconciliation would add failure modes to the case that never failed.
                # `None` means the duration could not be read, which is not a reason to
                # refuse to transcribe -- it is a reason not to slice blind.
                return self._transcribe_whole(wav_path, language)
            return self._transcribe_segmented(wav_path, language, duration_ms)

    def _transcribe_whole(self, wav_path: Path, language: str | None) -> ASRResponse:
        """One provider session over the whole file: the pre-segmentation behaviour."""
        result = self._session(wav_path, language, segment=None)
        segments = self._utterances(result, offset_ms=0)

        full_text = result.get("text", "")
        if not isinstance(full_text, str):
            raise DoubaoProtocolError(
                "Doubao ASR returned malformed transcript text", provider="doubao"
            )
        if not full_text and segments:
            full_text = "".join(segment.text for segment in segments)
        return ASRResponse(
            segments=segments,
            full_text=full_text,
            language=language,
            model=self.model,
        )

    def _transcribe_segmented(
        self, wav_path: Path, language: str | None, duration_ms: int
    ) -> ASRResponse:
        """Several bounded sessions, reconciled into one whole-media response.

        Every segment must succeed. A segment that exhausts its retries fails the whole
        call, because the alternative is returning 11 of 14 minutes of a video as though it
        were the transcript: downstream there is no field that says "incomplete", so the
        gap becomes evidence that the video does not discuss something it discusses, and a
        confident wrong answer is worse than a visible ASR failure.
        """
        plan = plan_segments(
            duration_ms, segment_ms=self.segment_ms, overlap_ms=self.overlap_ms
        )
        logger.info(
            "doubao_asr_segmented",
            extra={
                "segments": len(plan),
                "duration_ms": duration_ms,
                "segment_ms": self.segment_ms,
                "overlap_ms": self.overlap_ms,
            },
        )

        per_segment: list[list[TranscriptSegment]] = []
        for segment in plan:
            with _wav_slice(wav_path, segment) as slice_path:
                result = self._session(slice_path, language, segment=segment)
            # Offset by coverage, not core: the provider timestamps what it received, and
            # what it received starts at coverage_start_ms. Using core_start_ms here would
            # shift every utterance of every segment after the first by the overlap.
            per_segment.append(self._utterances(result, offset_ms=segment.coverage_start_ms))

        segments = _reconcile(plan, per_segment)
        return ASRResponse(
            segments=segments,
            # Reconciled text, so overlap context never appears twice. The provider's own
            # per-segment `text` fields cannot be concatenated for the same reason.
            full_text="".join(segment.text for segment in segments),
            language=language,
            model=self.model,
        )

    def _session(
        self, wav_path: Path, language: str | None, *, segment: AudioSegment | None
    ) -> dict[str, Any]:
        """One provider session's final result, retrying transient failures.

        Transport *and* response interpretation are inside the retry, because the observed
        failures arrive both ways. Attempt 2 against the real source returned a well-formed
        frame carrying upstream ``45000081``, and attempt 1 returned partial results and
        simply stopped: neither raises out of a WebSocket read, so retrying only the socket
        call would leave the more common of the two failures unretried.

        Both observed real failures -- WebSocket 1006 after a keepalive ping timeout, and a
        1000 close carrying upstream ``45000081`` -- are transient by nature: the same audio
        succeeded at shorter lengths and the two attempts failed differently. Retrying them
        is the point of this method.

        Two things are never retried. `ConfigurationError` (missing ffmpeg, rejected
        credentials, a bad segment policy) describes the environment, and the environment
        will not have changed in two seconds. `DoubaoProtocolError` means a response did not
        have the shape the protocol defines; that is deterministic, so three attempts only
        turn one fast failure into a slow one.
        """
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                responses = self._transport(wav_path, language)
                return self._final_result(responses, segment=segment)
            except ConfigurationError:
                raise
            except DoubaoProtocolError as exc:
                raise _with_context(exc, segment, attempt, self.max_attempts) from None
            except Exception as exc:
                last = exc
                if attempt == self.max_attempts:
                    break
                logger.warning(
                    "doubao_asr_segment_retry",
                    extra={
                        "attempt": attempt,
                        "max_attempts": self.max_attempts,
                        "segment_index": segment.index if segment else None,
                        **_safe_transport_context(exc),
                    },
                )
                self._sleep(self.retry_backoff_s * (2 ** (attempt - 1)))

        assert last is not None
        if isinstance(last, ASRFailed):
            raise _with_context(last, segment, self.max_attempts, self.max_attempts) from None
        raise ASRFailed(
            "Doubao ASR transport failed",
            provider="doubao",
            **_segment_context(segment),
            **_attempt_context(self.max_attempts, self.max_attempts),
            **_safe_transport_context(last),
        ) from last

    def _utterances(self, result: dict[str, Any], *, offset_ms: int) -> list[TranscriptSegment]:
        """Provider utterances as transcript segments, shifted onto the media timeline."""
        utterances = result.get("utterances", [])
        if utterances is None:
            utterances = []
        if not isinstance(utterances, list):
            raise DoubaoProtocolError(
                "Doubao ASR returned malformed utterances", provider="doubao"
            )

        segments: list[TranscriptSegment] = []
        for utterance in utterances:
            if not isinstance(utterance, dict):
                raise DoubaoProtocolError(
                    "Doubao ASR returned a malformed utterance", provider="doubao"
                )
            text = utterance.get("text", "")
            start_ms = utterance.get("start_time")
            end_ms = utterance.get("end_time")
            if (
                not isinstance(text, str)
                or not isinstance(start_ms, int)
                or not isinstance(end_ms, int)
            ):
                raise DoubaoProtocolError(
                    "Doubao ASR utterance is missing text or timestamps",
                    provider="doubao",
                )
            if start_ms < 0 or end_ms < start_ms:
                raise DoubaoProtocolError(
                    "Doubao ASR returned invalid timestamps", provider="doubao"
                )
            if not text.strip():
                continue
            additions = utterance.get("additions")
            speaker_id = additions.get("speaker_id") if isinstance(additions, dict) else None
            confidence = utterance.get("confidence")
            segments.append(
                TranscriptSegment(
                    text=text,
                    # The one line that makes segmentation invisible downstream. An
                    # EvidenceUnit timestamp has to mean "this moment of the original
                    # video"; a segment-relative one silently points at the wrong minute.
                    start_ms=offset_ms + start_ms,
                    end_ms=offset_ms + end_ms,
                    confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
                    speaker_id=str(speaker_id) if speaker_id is not None else None,
                )
            )
        return segments

    @contextmanager
    def _prepared_wav(self, source: Path) -> Iterator[Path]:
        """Yield a Seed-ASR-compatible WAV, transcoding only when necessary."""
        if source.suffix.lower() == ".wav" and _is_16k_mono_pcm_wav(source):
            yield source
            return

        executable = shutil.which(self.ffmpeg_path)
        if executable is None:
            raise ConfigurationError(
                "Doubao ASR needs ffmpeg to convert this media to 16 kHz mono WAV"
            )
        with tempfile.TemporaryDirectory(prefix="dk-doubao-asr-") as directory:
            output = Path(directory) / "audio.wav"
            completed = subprocess.run(
                [
                    executable,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0 or not output.is_file():
                raise ASRFailed(
                    "ffmpeg could not extract audio for Doubao ASR",
                    provider="doubao",
                    returncode=completed.returncode,
                )
            yield output

    def _transcribe_over_websocket(self, wav_path: Path, language: str | None) -> list[Response]:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover - installation problem
            raise ConfigurationError(
                "Doubao ASR requires the 'websockets' package"
            ) from exc

        request_id = str(uuid.uuid4())
        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Connect-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        responses: list[Response] = []

        async def exchange() -> None:
            async with connect(
                self.endpoint,
                additional_headers=headers,
                open_timeout=self.timeout_s,
                close_timeout=10,
                max_size=16 * 1024 * 1024,
            ) as websocket:
                sequence = 1
                await websocket.send(_full_client_request(sequence, language))
                first = await asyncio.wait_for(websocket.recv(), timeout=self.timeout_s)
                responses.append(_parse_server_message(first))

                audio = wav_path.read_bytes()

                async def send_audio() -> None:
                    nonlocal sequence
                    chunk_size = 16000 * 2 * 200 // 1000  # 200 ms, mono 16-bit PCM.
                    chunks = list(_chunks(audio, chunk_size)) or [b""]
                    for index, chunk in enumerate(chunks):
                        sequence += 1
                        is_last = index == len(chunks) - 1
                        await websocket.send(_audio_request(sequence, chunk, is_last=is_last))
                        if not is_last:
                            await asyncio.sleep(0.2)

                async def receive_results() -> None:
                    while not responses[-1].get("is_last_package", False):
                        message = await asyncio.wait_for(
                            websocket.recv(), timeout=self.timeout_s
                        )
                        responses.append(_parse_server_message(message))

                await asyncio.gather(send_audio(), receive_results())

        try:
            asyncio.run(exchange())
        except ASRFailed:
            raise
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {401, 403}:
                # Configuration, not a transient ASR failure. Retrying a rejected key three
                # times produces three rejections and hides the actual problem behind a
                # retry budget.
                raise ConfigurationError("Doubao ASR authentication was rejected") from exc
            raise ASRFailed(
                "Doubao ASR WebSocket request failed",
                provider="doubao",
                **_safe_transport_context(exc),
            ) from exc
        return responses

    @staticmethod
    def _final_result(
        responses: list[Response], *, segment: AudioSegment | None = None
    ) -> dict[str, Any]:
        context = _segment_context(segment)
        if not responses:
            raise ASRFailed("Doubao ASR returned no response", provider="doubao", **context)

        final_result: dict[str, Any] | None = None
        saw_last = False
        for response in responses:
            code = response.get("code", 0)
            if not isinstance(code, int):
                raise DoubaoProtocolError(
                    "Doubao ASR returned a malformed status code", provider="doubao", **context
                )
            if code != 0:
                # An upstream business code is safe to surface and is the single most useful
                # field when this fails in production: `45000081` is what identified the real
                # failure as a provider session timeout rather than a local bug. The message
                # is the provider's own and carries no account material.
                payload = response.get("payload_msg")
                message = payload.get("message") if isinstance(payload, dict) else None
                raise ASRFailed(
                    "Doubao ASR API returned an error",
                    provider="doubao",
                    upstream_code=code,
                    **({"upstream_message": message} if isinstance(message, str) else {}),
                    **context,
                )
            saw_last = saw_last or response.get("is_last_package") is True
            payload = response.get("payload_msg")
            if payload is None:
                continue
            if not isinstance(payload, dict):
                raise DoubaoProtocolError(
                    "Doubao ASR returned a malformed payload", provider="doubao", **context
                )
            result: Any = payload.get("result")
            if isinstance(result, list):
                result = next((item for item in reversed(result) if isinstance(item, dict)), None)
            if result is not None:
                if not isinstance(result, dict):
                    raise DoubaoProtocolError(
                        "Doubao ASR returned a malformed result", provider="doubao", **context
                    )
                final_result = result

        if not saw_last:
            # Truncation, which is exactly what the real attempt 1 looked like: results to
            # ~167 s of a 841.8 s file and then a 1006 close. Transient, so retryable, and
            # deliberately not a protocol error.
            raise ASRFailed(
                "Doubao ASR response ended before the final package",
                provider="doubao",
                **context,
            )
        return final_result or {"text": "", "utterances": []}

def _reconcile(
    plan: list[AudioSegment], per_segment: list[list[TranscriptSegment]]
) -> list[TranscriptSegment]:
    """Keep each utterance once, by temporal ownership, and sort globally.

    Overlap exists so the recogniser has context across a cut, not so the same speech is
    transcribed twice -- but it *is* transcribed twice, once by each neighbour, and the two
    transcriptions differ: punctuation moves, a word is heard differently with less context
    after it. So "did I already output this?" cannot be answered by comparing text without
    inventing a similarity threshold, and a threshold that is wrong either duplicates a
    sentence or deletes one.

    Ownership sidesteps the question. Core intervals tile the media, each utterance's
    midpoint falls in exactly one of them, and the segment owning that interval is the one
    that keeps it. No text comparison, no threshold, stable under any punctuation the
    provider chooses.

    Ownership is necessary but not sufficient. It assumes each piece of speech belongs to
    one utterance whose position is stable across sessions, and the real 841.7 s run showed
    it is not: the provider regroups speech into different utterances depending on how much
    context follows it, so at each boundary the two neighbours return *different* utterances
    that share a repeated phrase, each legitimately owned, each containing unique speech.
    Ownership keeps both, correctly, and `_stitch_boundaries` then removes the repetition.
    """
    kept: list[_OwnedUtterance] = []
    for segment, utterances in zip(plan, per_segment, strict=True):
        is_last = segment.index == len(plan) - 1
        kept.extend(
            _OwnedUtterance(utterance, segment.index)
            for utterance in utterances
            if segment.owns(utterance.start_ms, utterance.end_ms, is_last=is_last)
        )
    # Sorted across the whole media, not per segment. Segments arrive in order and their
    # cores do not overlap, so this is nearly a no-op -- but "nearly" is not a guarantee a
    # downstream consumer of `segments[0]` should have to rely on, and an utterance that
    # starts inside the overlap can precede the previous segment's last owned utterance.
    kept.sort(key=lambda item: (item.start_ms, item.end_ms, item.text))
    return _stitch_boundaries(kept)


@dataclass(frozen=True)
class _OwnedUtterance:
    """An utterance plus which provider session produced it.

    Origin is what makes the stitching pass boundary-local rather than a transcript-wide
    deduplicator. Without it, two ordinary consecutive utterances from the *same* session
    that happen to share a phrase look identical to a genuine cross-session duplicate, and
    merging those would delete speech that really was said twice.
    """

    utterance: TranscriptSegment
    origin: int

    @property
    def start_ms(self) -> int:
        return self.utterance.start_ms

    @property
    def end_ms(self) -> int:
        return self.utterance.end_ms

    @property
    def text(self) -> str:
        return self.utterance.text


def _normalize(text: str) -> tuple[str, list[int]]:
    """Comparable characters, plus where each one came from in the raw string.

    Returns the normalized text and a parallel list mapping each normalized index back to
    its index in `text`. The mapping is the point: the two sides of a boundary are punctuated
    differently, so a match found in normalized space has to be translated back to a raw
    offset. Slicing the raw string by a normalized character count instead is the obvious
    shortcut and it eats real characters whenever the counts differ.
    """
    kept: list[str] = []
    origins: list[int] = []
    for index, char in enumerate(text):
        if not char.isalnum():  # Drops whitespace and punctuation, keeps CJK and digits.
            continue
        kept.append(char)
        origins.append(index)
    return "".join(kept), origins


def _overlap_chars(left_norm: str, right_norm: str) -> int:
    """Longest normalized suffix of `left_norm` that is a prefix of `right_norm`.

    Longest rather than shortest: the repeated phrase is the whole shared region, and
    removing less than all of it leaves part of the duplicate behind.
    """
    limit = min(len(left_norm), len(right_norm))
    for length in range(limit, MIN_STITCH_OVERLAP_CHARS - 1, -1):
        if left_norm[-length:] == right_norm[:length]:
            return length
    return 0


def _intersects(left: _OwnedUtterance, right: _OwnedUtterance) -> bool:
    return left.end_ms > right.start_ms and right.end_ms > left.start_ms


def _stitch_boundaries(owned: list[_OwnedUtterance]) -> list[TranscriptSegment]:
    """Merge duplicated speech across session boundaries, conservatively.

    The invariant that forces this pass: audio overlap != ASR utterance-boundary overlap.
    Adjacent sessions hear overlapping audio, but the provider groups it into utterances
    whose start and end move by seconds, so a boundary produces two utterances that share a
    phrase in the middle while each holds unique speech at its far end.

    Whole-utterance winner rules are therefore all wrong -- later-wins, earlier-wins,
    midpoint, edge distance. Both real examples have unique speech on *both* sides, so every
    one of those rules deletes something that was said. The fix is to keep both texts and
    remove only the repetition, producing one slightly broader evidence atom spanning the
    union of the two intervals. Slightly broader is safe; a fabricated timestamp for an
    artificially trimmed fragment is not.

    A pair is only considered when all of these hold, and any doubt leaves it alone:
    adjacent sessions, near their shared boundary, intersecting in time, and a normalized
    left-suffix/right-prefix match of at least `MIN_STITCH_OVERLAP_CHARS`. No fuzzy
    similarity, no transcript-wide comparison.
    """
    candidates = _stitch_candidates(owned)
    if not candidates:
        return [item.utterance for item in owned]

    # Decide everything before emitting anything. An earlier version walked `owned` in
    # global order and suppressed the right half as it reached it, which quietly assumed the
    # left half sorts first. Ownership is decided by midpoint, not by start, so a right-hand
    # utterance that opens before the boundary and runs past it can start earlier than its
    # left-hand partner while both are owned correctly -- and then the right half had already
    # been published by the time the pair was discovered, so it appeared twice: once alone
    # and once inside the stitch. Deciding first makes the result independent of order.
    paired = {index for pair in candidates.items() for index in (pair[0], pair[1][0])}
    merged = [
        _stitch(owned[left_index], owned[right_index], overlap)
        for left_index, (right_index, overlap) in candidates.items()
    ]
    merged.extend(
        item.utterance for index, item in enumerate(owned) if index not in paired
    )
    merged.sort(key=lambda item: (item.start_ms, item.end_ms, item.text))
    return merged


def _stitch_candidates(owned: list[_OwnedUtterance]) -> dict[int, tuple[int, int]]:
    """Unambiguous left index -> (right index, normalized overlap length).

    Ambiguity is dropped rather than resolved. If one utterance could stitch to two
    different partners, choosing between them is a guess, and a wrong guess deletes real
    speech -- so both candidates are discarded and the duplication stays visible.
    """
    pairs: list[tuple[int, int, int]] = []
    for left_index, left in enumerate(owned):
        left_norm, _ = _normalize(left.text)
        if len(left_norm) < MIN_STITCH_OVERLAP_CHARS:
            continue
        for right_index, right in enumerate(owned):
            if right.origin != left.origin + 1:
                continue  # Same session, or sessions that never shared audio.
            if not _intersects(left, right):
                continue  # No shared audio means the phrase was said twice, not heard twice.
            if abs(right.start_ms - left.end_ms) > _MAX_BOUNDARY_DRIFT_MS:
                continue  # Too far from the seam to be a boundary artefact.
            right_norm, _ = _normalize(right.text)
            overlap = _overlap_chars(left_norm, right_norm)
            if overlap:
                pairs.append((left_index, right_index, overlap))

    # Membership is counted across both roles at once, not per role. An index that is the
    # left of one candidate and the right of another is just as ambiguous as one with two
    # partners on the same side: whichever pair got accepted would be decided by iteration
    # order, and the loser's text would be merged into a segment it does not belong to.
    # Counting roles separately misses that case entirely.
    membership = Counter(
        index for left_index, right_index, _ in pairs for index in (left_index, right_index)
    )
    return {
        left_index: (right_index, overlap)
        for left_index, right_index, overlap in pairs
        if membership[left_index] == 1 and membership[right_index] == 1
    }


def _stitch(
    left: _OwnedUtterance, right: _OwnedUtterance, overlap: int
) -> TranscriptSegment:
    """One segment holding both texts once, spanning the union of both intervals."""
    right_norm, origins = _normalize(right.text)
    # Translate the normalized match length into a raw offset. `origins[overlap]` is where
    # the first non-duplicated normalized character sits in the raw string; anything before
    # it is duplicate text or the punctuation between duplicate characters.
    cut = origins[overlap] if overlap < len(right_norm) else len(right.text)
    remainder = right.text[cut:]
    return TranscriptSegment(
        text=left.text + remainder,
        start_ms=min(left.start_ms, right.start_ms),
        end_ms=max(left.end_ms, right.end_ms),
        # The merged text spans both halves, so it is only as reliable as the weaker half.
        # A missing confidence stays missing: inventing one for half the text would make the
        # merged segment look better attested than it is.
        confidence=(
            min(left.utterance.confidence, right.utterance.confidence)
            if left.utterance.confidence is not None and right.utterance.confidence is not None
            else None
        ),
        # Agreement only. Two sessions disagreeing about who spoke is not grounds for
        # picking one, and a wrong speaker id is worse than no speaker id.
        speaker_id=(
            left.utterance.speaker_id
            if left.utterance.speaker_id == right.utterance.speaker_id
            else None
        ),
    )


def _wav_duration_ms(path: Path) -> int | None:
    """Duration of a PCM WAV in whole milliseconds, or None if it cannot be read."""
    try:
        with wave.open(str(path), "rb") as audio:
            rate = audio.getframerate()
            if rate <= 0:
                return None
            return int(audio.getnframes() * 1000 / rate)
    except (OSError, wave.Error):
        return None


@contextmanager
def _wav_slice(source: Path, segment: AudioSegment) -> Iterator[Path]:
    """Yield `segment`'s coverage interval as its own WAV file.

    Frame-accurate: at 16 kHz a millisecond is exactly 16 frames, so a slice boundary is a
    frame boundary and the offset arithmetic in `_utterances` is exact rather than
    approximately right. Sliced from the already-prepared WAV, never re-transcoded from the
    original MP4 -- re-running ffmpeg per segment would cost minutes per video and, worse,
    would make each segment's zero point depend on a decoder's seek accuracy.
    """
    with wave.open(str(source), "rb") as audio:
        params = audio.getparams()
        start_frame = segment.coverage_start_ms * _FRAMES_PER_MS
        frames = (segment.coverage_end_ms - segment.coverage_start_ms) * _FRAMES_PER_MS
        audio.setpos(min(start_frame, params.nframes))
        payload = audio.readframes(frames)

    with tempfile.TemporaryDirectory(prefix="dk-doubao-seg-") as directory:
        target = Path(directory) / f"segment-{segment.index:03d}.wav"
        with wave.open(str(target), "wb") as out:
            out.setnchannels(params.nchannels)
            out.setsampwidth(params.sampwidth)
            out.setframerate(params.framerate)
            out.writeframes(payload)
        yield target


def _segment_context(segment: AudioSegment | None) -> dict[str, Any]:
    """Safe, structured segment identity for `DKError.context`.

    Structured rather than interpolated into the message: the whole reason the real
    diagnosis was slow is that transport detail collapsed into one string, and recovering
    "which segment, how far in" then meant parsing log lines.
    """
    if segment is None:
        return {}
    return {
        "segment_index": segment.index,
        "segment_start_ms": segment.core_start_ms,
        "segment_end_ms": segment.core_end_ms,
        "segment_coverage_start_ms": segment.coverage_start_ms,
        "segment_coverage_end_ms": segment.coverage_end_ms,
    }


def _attempt_context(attempt: int, max_attempts: int) -> dict[str, Any]:
    return {"attempt": attempt, "max_attempts": max_attempts}


def _safe_transport_context(exc: BaseException) -> dict[str, Any]:
    """Non-secret transport detail from a WebSocket exception.

    Close code and reason are what distinguished the two real failures from each other --
    1006 with a ping timeout versus 1000 with upstream 45000081 -- and neither is derivable
    from the exception type. Read by attribute so `websockets` version differences degrade
    to "absent" rather than raising.

    Read positively, one field at a time. Never `vars(exc)` or `repr(exc)`: the request that
    caused the exception holds the `X-Api-Key` header, so anything that copies the
    exception's contents wholesale is one library change away from logging the key.
    """
    context: dict[str, Any] = {"error_type": type(exc).__name__}
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        context["websocket_close_code"] = code
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        context["websocket_close_reason"] = reason
    return context


def _with_context(
    exc: ASRFailed, segment: AudioSegment | None, attempt: int, max_attempts: int
) -> ASRFailed:
    """Add segment/attempt identity to an `ASRFailed` raised deeper down.

    Mutates rather than re-wraps so the original type -- including `DoubaoProtocolError` --
    and its already-collected upstream fields survive. Existing keys win: an inner frame
    knows more about itself than this one does.
    """
    for key, value in {
        **_segment_context(segment),
        **_attempt_context(attempt, max_attempts),
    }.items():
        exc.context.setdefault(key, value)
    return exc


def _is_16k_mono_pcm_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as audio:
            return (
                audio.getnchannels() == 1
                and audio.getsampwidth() == 2
                and audio.getframerate() == 16000
                and audio.getcomptype() == "NONE"
            )
    except (OSError, wave.Error):
        return False


def _header(message_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes((0x11, (message_type << 4) | flags, (serialization << 4) | compression, 0))


def _full_client_request(sequence: int, language: str | None) -> bytes:
    audio: dict[str, Any] = {
        "format": "wav",
        "codec": "raw",
        "rate": 16000,
        "bits": 16,
        "channel": 1,
    }
    if language:
        audio["language"] = language
    body = {
        "user": {"uid": "douyin-knowledge"},
        "audio": audio,
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
            "enable_nonstream": False,
        },
    }
    payload = gzip.compress(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    return b"".join(
        (
            _header(_FULL_CLIENT_REQUEST, _POSITIVE_SEQUENCE, _JSON_SERIALIZATION, _GZIP_COMPRESSION),
            struct.pack(">iI", sequence, len(payload)),
            payload,
        )
    )


def _audio_request(sequence: int, audio: bytes, *, is_last: bool) -> bytes:
    payload = gzip.compress(audio)
    flags = _NEGATIVE_WITH_SEQUENCE if is_last else _POSITIVE_SEQUENCE
    signed_sequence = -sequence if is_last else sequence
    return b"".join(
        (
            _header(_AUDIO_ONLY_REQUEST, flags, 0, _GZIP_COMPRESSION),
            struct.pack(">iI", signed_sequence, len(payload)),
            payload,
        )
    )


def _parse_server_message(message: str | bytes) -> Response:
    if not isinstance(message, bytes) or len(message) < 4:
        raise ASRFailed("Doubao ASR returned a malformed WebSocket frame", provider="doubao")
    header_words = message[0] & 0x0F
    header_size = header_words * 4
    if header_words == 0 or len(message) < header_size:
        raise ASRFailed("Doubao ASR returned an invalid frame header", provider="doubao")
    message_type = message[1] >> 4
    flags = message[1] & 0x0F
    compression = message[2] & 0x0F
    payload = memoryview(message)[header_size:]
    response: Response = {
        "code": 0,
        "event": 0,
        "is_last_package": bool(flags & 0x02),
        "payload_sequence": 0,
        "payload_msg": None,
    }

    if flags & 0x01:
        response["payload_sequence"], payload = _take_int(payload, signed=True)
    if flags & 0x04:
        response["event"], payload = _take_int(payload, signed=True)

    if message_type == _FULL_SERVER_RESPONSE:
        payload_size, payload = _take_int(payload, signed=False)
    elif message_type == _SERVER_ERROR_RESPONSE:
        response["code"], payload = _take_int(payload, signed=True)
        payload_size, payload = _take_int(payload, signed=False)
    else:
        raise ASRFailed(
            "Doubao ASR returned an unsupported WebSocket message",
            provider="doubao",
            message_type=message_type,
        )

    if payload_size != len(payload):
        raise ASRFailed("Doubao ASR returned a truncated payload", provider="doubao")
    raw = bytes(payload)
    if compression == _GZIP_COMPRESSION and raw:
        try:
            raw = gzip.decompress(raw)
        except gzip.BadGzipFile as exc:
            raise ASRFailed("Doubao ASR returned invalid compressed data", provider="doubao") from exc
    if raw:
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ASRFailed("Doubao ASR returned invalid JSON", provider="doubao") from exc
        response["payload_msg"] = decoded
    return response


def _take_int(payload: memoryview, *, signed: bool) -> tuple[int, memoryview]:
    if len(payload) < 4:
        raise ASRFailed("Doubao ASR returned a truncated frame", provider="doubao")
    return int.from_bytes(payload[:4], "big", signed=signed), payload[4:]


def _chunks(data: bytes, size: int) -> Iterator[bytes]:
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]
