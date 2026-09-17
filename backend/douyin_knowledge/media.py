"""Optional bounded video-to-speech evidence adapter."""

import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

MAX_MEDIA_BYTES = 100 * 1024 * 1024


def should_transcribe(caption: str, transcript: str) -> bool:
    return not transcript.strip() and len(caption.strip()) < int(
        os.getenv("DK_ASR_CAPTION_THRESHOLD", "80")
    )


class OpenAITranscriber:
    def transcribe(self, url: str) -> str:
        if not os.getenv("DK_OPENAI_API_KEY"):
            raise RuntimeError("DK_OPENAI_API_KEY is required for media transcription")
        import imageio_ffmpeg

        with tempfile.TemporaryDirectory(prefix="dk-media-") as directory:
            video = Path(directory) / "video"
            audio = Path(directory) / "audio.mp3"
            size = 0
            with httpx.Client(timeout=60, follow_redirects=False) as client:
                for _ in range(6):
                    if urlparse(url).scheme != "https":
                        raise ValueError("Media URL must use HTTPS")
                    with client.stream("GET", url) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("Media redirect has no location")
                            url = urljoin(url, location)
                            continue
                        response.raise_for_status()
                        with video.open("wb") as stream:
                            for chunk in response.iter_bytes():
                                size += len(chunk)
                                if size > MAX_MEDIA_BYTES:
                                    raise ValueError("Media exceeds 100 MiB limit")
                                stream.write(chunk)
                        break
                else:
                    raise ValueError("Media redirected too many times")
            subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(video),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-b:a",
                    "32k",
                    str(audio),
                ],
                check=True,
                timeout=120,
                capture_output=True,
            )
            if audio.stat().st_size > 25 * 1024 * 1024:
                raise ValueError("Extracted audio exceeds 25 MiB transcription limit")
            base = os.getenv("DK_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            with audio.open("rb") as stream:
                response = httpx.post(
                    base + "/audio/transcriptions",
                    headers={"Authorization": "Bearer " + os.environ["DK_OPENAI_API_KEY"]},
                    data={"model": os.getenv("DK_OPENAI_ASR_MODEL", "whisper-1")},
                    files={"file": ("audio.mp3", stream, "audio/mpeg")},
                    timeout=180,
                )
            response.raise_for_status()
            result = response.json().get("text", "").strip()
            if not result:
                raise ValueError("Transcription returned no text")
            return result
