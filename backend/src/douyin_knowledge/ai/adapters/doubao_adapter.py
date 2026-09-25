"""Doubao Seed-ASR 2.0 adapter.

The adapter keeps the local-first boundary intact: it streams bytes from a local
file directly to Volcengine's WebSocket API.  The API doesn't accept MP4, so video
inputs are converted to a temporary 16 kHz mono WAV with ffmpeg.  Nothing is
uploaded to object storage and the API key is never included in an error message.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import struct
import subprocess
import tempfile
import uuid
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from douyin_knowledge.ai.providers import ASRResponse, TranscriptSegment
from douyin_knowledge.core.errors import ASRFailed, ConfigurationError

DOUBAO_SEED_ASR_2_MODEL = "doubao-seed-asr-2.0"
DEFAULT_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"

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
    ) -> None:
        if not api_key:
            raise ConfigurationError("doubao ASR requires DK_DOUBAO_ASR_API_KEY")
        self.model = DOUBAO_SEED_ASR_2_MODEL
        self._api_key = api_key
        self.resource_id = resource_id
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.ffmpeg_path = ffmpeg_path
        self._transport = transport or self._transcribe_over_websocket

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

        try:
            with self._prepared_wav(source) as wav_path:
                responses = self._transport(wav_path, language)
        except ConfigurationError:
            raise
        except ASRFailed:
            raise
        except Exception as exc:
            raise ASRFailed(
                "Doubao ASR transport failed",
                provider="doubao",
                error_type=type(exc).__name__,
            ) from exc

        result = self._final_result(responses)
        utterances = result.get("utterances", [])
        if utterances is None:
            utterances = []
        if not isinstance(utterances, list):
            raise ASRFailed("Doubao ASR returned malformed utterances", provider="doubao")

        segments: list[TranscriptSegment] = []
        for utterance in utterances:
            if not isinstance(utterance, dict):
                raise ASRFailed("Doubao ASR returned a malformed utterance", provider="doubao")
            text = utterance.get("text", "")
            start_ms = utterance.get("start_time")
            end_ms = utterance.get("end_time")
            if not isinstance(text, str) or not isinstance(start_ms, int) or not isinstance(end_ms, int):
                raise ASRFailed(
                    "Doubao ASR utterance is missing text or timestamps",
                    provider="doubao",
                )
            if start_ms < 0 or end_ms < start_ms:
                raise ASRFailed("Doubao ASR returned invalid timestamps", provider="doubao")
            if not text.strip():
                continue
            additions = utterance.get("additions")
            speaker_id = additions.get("speaker_id") if isinstance(additions, dict) else None
            segments.append(
                TranscriptSegment(
                    text=text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker_id=str(speaker_id) if speaker_id is not None else None,
                )
            )

        full_text = result.get("text", "")
        if not isinstance(full_text, str):
            raise ASRFailed("Doubao ASR returned malformed transcript text", provider="doubao")
        if not full_text and segments:
            full_text = "".join(segment.text for segment in segments)
        return ASRResponse(
            segments=segments,
            full_text=full_text,
            language=language,
            model=self.model,
        )

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
                raise ConfigurationError("Doubao ASR authentication was rejected") from exc
            raise ASRFailed(
                "Doubao ASR WebSocket request failed",
                provider="doubao",
                error_type=type(exc).__name__,
            ) from exc
        return responses

    @staticmethod
    def _final_result(responses: list[Response]) -> dict[str, Any]:
        if not responses:
            raise ASRFailed("Doubao ASR returned no response", provider="doubao")

        final_result: dict[str, Any] | None = None
        saw_last = False
        for response in responses:
            code = response.get("code", 0)
            if not isinstance(code, int):
                raise ASRFailed("Doubao ASR returned a malformed status code", provider="doubao")
            if code != 0:
                raise ASRFailed(
                    "Doubao ASR API returned an error",
                    provider="doubao",
                    upstream_code=code,
                )
            saw_last = saw_last or response.get("is_last_package") is True
            payload = response.get("payload_msg")
            if payload is None:
                continue
            if not isinstance(payload, dict):
                raise ASRFailed("Doubao ASR returned a malformed payload", provider="doubao")
            result: Any = payload.get("result")
            if isinstance(result, list):
                result = next((item for item in reversed(result) if isinstance(item, dict)), None)
            if result is not None:
                if not isinstance(result, dict):
                    raise ASRFailed("Doubao ASR returned a malformed result", provider="doubao")
                final_result = result

        if not saw_last:
            raise ASRFailed("Doubao ASR response ended before the final package", provider="doubao")
        return final_result or {"text": "", "utterances": []}

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
