from pathlib import Path

import httpx
import pytest

from douyin_knowledge.media import OpenAITranscriber, should_transcribe


def test_adaptive_transcription_decision(monkeypatch):
    monkeypatch.setenv("DK_ASR_CAPTION_THRESHOLD", "8")
    assert should_transcribe("短标题", "")
    assert not should_transcribe("已有足够长度的文字描述", "")
    assert not should_transcribe("短标题", "已有转写")


def test_transcription_adapter_download_extract_upload(monkeypatch):
    monkeypatch.setenv("DK_OPENAI_API_KEY", "test-key")

    def transport(request):
        assert request.url.host == "example.com"
        return httpx.Response(200, content=b"video bytes")

    client = httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr("douyin_knowledge.media.httpx.Client", lambda **kwargs: client)

    def extract(command, **kwargs):
        assert Path(command[command.index("-i") + 1]).read_bytes() == b"video bytes"
        Path(command[-1]).write_bytes(b"audio bytes")

    monkeypatch.setattr("douyin_knowledge.media.subprocess.run", extract)

    def upload(url, headers, data, files, timeout):
        assert url.endswith("/audio/transcriptions")
        assert headers["Authorization"] == "Bearer test-key"
        assert files["file"][1].read() == b"audio bytes"
        return httpx.Response(200, json={"text": "转写结果"}, request=httpx.Request("POST", url))

    monkeypatch.setattr("douyin_knowledge.media.httpx.post", upload)
    assert OpenAITranscriber().transcribe("https://example.com/video.mp4") == "转写结果"


def test_transcription_rejects_non_https(monkeypatch):
    monkeypatch.setenv("DK_OPENAI_API_KEY", "test-key")
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAITranscriber().transcribe("http://localhost/private")


def test_transcription_rejects_downgrade_redirect(monkeypatch):
    monkeypatch.setenv("DK_OPENAI_API_KEY", "test-key")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "http://localhost/private"})
        )
    )
    monkeypatch.setattr("douyin_knowledge.media.httpx.Client", lambda **kwargs: client)
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAITranscriber().transcribe("https://example.com/video.mp4")
