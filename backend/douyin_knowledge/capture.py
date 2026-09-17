import json
import os
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, Field


class CapturedSource(BaseModel):
    external_id: str
    platform: str = "douyin"
    url: str = ""
    title: str = ""
    caption: str = ""
    creator_id: str = ""
    creator_name: str = ""
    collection: str = ""
    semantic_type: str = ""
    transcript: str = ""
    # Explicit fixture evidence/claims; validated against source text before persistence.
    claims: list[dict] = Field(default_factory=list)


class CaptureProvider(Protocol):
    def list_saves(self) -> list[CapturedSource]: ...


class FileCaptureProvider:
    def __init__(self, path: str):
        self.path = Path(path)

    def list_saves(self) -> list[CapturedSource]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [CapturedSource.model_validate(item) for item in raw]


class SidecarCaptureProvider:
    """Explicit collection-export endpoint contract; no Douyin protocol code here.

    The upstream v5 API does not currently document a saved-collection listing
    endpoint. A bridge can expose DK_SIDECAR_SAVES_PATH returning our normalized
    list. An absent path fails clearly instead of claiming automatic sync works.
    """

    def __init__(self):
        self.url = os.getenv("DK_SIDECAR_URL", "http://127.0.0.1:8000").rstrip("/")
        self.path = os.getenv("DK_SIDECAR_SAVES_PATH", "")
        self.key = os.getenv("DK_SIDECAR_API_KEY", "")

    def list_saves(self) -> list[CapturedSource]:
        if not self.path:
            raise RuntimeError("Set DK_SIDECAR_SAVES_PATH to a collection-export bridge endpoint")
        headers = {"X-API-Key": self.key} if self.key else {}
        response = httpx.get(self.url + self.path, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Collection-export endpoint must return a JSON list")
        return [CapturedSource.model_validate(item) for item in data]
