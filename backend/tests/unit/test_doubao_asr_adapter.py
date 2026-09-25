"""Deterministic contract tests for the Doubao Seed-ASR adapter."""

from __future__ import annotations

import gzip
import json
import struct
import wave
from pathlib import Path

import pytest

from douyin_knowledge.ai.adapters.doubao_adapter import (
    DOUBAO_SEED_ASR_2_MODEL,
    DoubaoASRProvider,
    _parse_server_message,
)
from douyin_knowledge.core.errors import ASRFailed, ConfigurationError


def _wav(tmp_path: Path) -> Path:
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 160)
    return path


def _response(result: dict[str, object] | None = None, **overrides: object) -> dict[str, object]:
    response: dict[str, object] = {
        "code": 0,
        "is_last_package": True,
        "payload_msg": {"result": result or {"text": "", "utterances": []}},
    }
    response.update(overrides)
    return response


def test_segmented_chinese_transcription_maps_timestamps_and_speakers(tmp_path: Path) -> None:
    seen: list[tuple[Path, str | None]] = []

    def transport(path: Path, language: str | None) -> list[dict[str, object]]:
        seen.append((path, language))
        return [
            _response(
                {
                    "text": "你好，世界。第二句话。",
                    "utterances": [
                        {
                            "text": "你好，世界。",
                            "start_time": 280,
                            "end_time": 2000,
                            "definite": True,
                            "additions": {"speaker_id": "0"},
                        },
                        {
                            "text": "第二句话。",
                            "start_time": 2040,
                            "end_time": 3900,
                            "definite": True,
                            "additions": {"speaker_id": "1"},
                        },
                    ],
                }
            )
        ]

    provider = DoubaoASRProvider(api_key="not-a-real-key", transport=transport)
    result = provider.transcribe(str(_wav(tmp_path)), language="zh-CN")

    assert result.full_text == "你好，世界。第二句话。"
    assert result.language == "zh-CN"
    assert result.model == DOUBAO_SEED_ASR_2_MODEL
    assert [(item.start_ms, item.end_ms) for item in result.segments] == [
        (280, 2000),
        (2040, 3900),
    ]
    assert [item.speaker_id for item in result.segments] == ["0", "1"]
    assert seen == [(tmp_path / "audio.wav", "zh-CN")]


def test_empty_transcript_is_a_valid_response(tmp_path: Path) -> None:
    provider = DoubaoASRProvider(
        api_key="not-a-real-key",
        transport=lambda _path, _language: [_response()],
    )
    result = provider.transcribe(str(_wav(tmp_path)))
    assert result.full_text == ""
    assert result.segments == []


@pytest.mark.parametrize(
    "response",
    [
        {"code": 0, "is_last_package": True, "payload_msg": []},
        _response({"text": "bad", "utterances": "not-a-list"}),
        _response(
            {
                "text": "bad",
                "utterances": [{"text": "bad", "start_time": 10}],
            }
        ),
        {"code": 0, "is_last_package": False, "payload_msg": None},
    ],
)
def test_malformed_responses_fail_loudly(tmp_path: Path, response: dict[str, object]) -> None:
    provider = DoubaoASRProvider(
        api_key="not-a-real-key",
        transport=lambda _path, _language: [response],
    )
    with pytest.raises(ASRFailed):
        provider.transcribe(str(_wav(tmp_path)))


def test_api_error_preserves_non_secret_upstream_code(tmp_path: Path) -> None:
    provider = DoubaoASRProvider(
        api_key="not-a-real-key",
        transport=lambda _path, _language: [
            _response(code=45000001, payload_msg={"message": "authentication failed"})
        ],
    )
    with pytest.raises(ASRFailed) as info:
        provider.transcribe(str(_wav(tmp_path)))
    assert info.value.context["upstream_code"] == 45000001
    assert "not-a-real-key" not in str(info.value.to_dict())


def test_network_failure_becomes_retryable_asr_failure(tmp_path: Path) -> None:
    def failed_transport(_path: Path, _language: str | None) -> list[dict[str, object]]:
        raise OSError("connection reset")

    provider = DoubaoASRProvider(api_key="not-a-real-key", transport=failed_transport)
    with pytest.raises(ASRFailed) as info:
        provider.transcribe(str(_wav(tmp_path)))
    assert info.value.retryable is True
    assert info.value.context["error_type"] == "OSError"


def test_mp4_requires_ffmpeg_when_not_installed(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not-media")
    provider = DoubaoASRProvider(
        api_key="not-a-real-key",
        ffmpeg_path="definitely-not-a-real-ffmpeg",
        transport=lambda _path, _language: [],
    )
    with pytest.raises(ConfigurationError, match="needs ffmpeg"):
        provider.transcribe(str(video))


def test_binary_server_response_parser_accepts_official_shape() -> None:
    body = gzip.compress(
        json.dumps({"result": {"text": "你好", "utterances": []}}).encode()
    )
    frame = b"".join(
        (
            bytes((0x11, 0x93, 0x11, 0x00)),
            struct.pack(">iI", -2, len(body)),
            body,
        )
    )
    response = _parse_server_message(frame)
    assert response["is_last_package"] is True
    assert response["payload_sequence"] == -2
    assert response["payload_msg"] == {"result": {"text": "你好", "utterances": []}}


def test_binary_server_response_parser_rejects_truncated_payload() -> None:
    frame = bytes((0x11, 0x92, 0x10, 0x00)) + struct.pack(">I", 10) + b"{}"
    with pytest.raises(ASRFailed, match="truncated payload"):
        _parse_server_message(frame)
