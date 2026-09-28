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
anything else. One second is the narrowest value the boundary tests hold at; the overlap
is context only and never contributes a returned utterance, so widening it costs upload
bytes and model time rather than correctness.
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
    """
    kept: list[TranscriptSegment] = []
    for segment, utterances in zip(plan, per_segment, strict=True):
        is_last = segment.index == len(plan) - 1
        kept.extend(
            utterance
            for utterance in utterances
            if segment.owns(utterance.start_ms, utterance.end_ms, is_last=is_last)
        )
    # Sorted across the whole media, not per segment. Segments arrive in order and their
    # cores do not overlap, so this is nearly a no-op -- but "nearly" is not a guarantee a
    # downstream consumer of `segments[0]` should have to rely on, and an utterance that
    # starts inside the overlap can precede the previous segment's last owned utterance.
    kept.sort(key=lambda item: (item.start_ms, item.end_ms, item.text))
    return kept


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
