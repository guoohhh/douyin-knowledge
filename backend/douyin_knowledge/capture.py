"""Capture contracts and the v5.1 Douyin sidecar transport boundary."""

import json
import os
import time
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
    collections: list[str] = Field(default_factory=list)
    semantic_type: str = ""
    transcript: str = ""
    # Explicit fixture assertions; quotes are validated against evidence before persistence.
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
    """Read Douyin bookmark folders from Douyin_TikTok_Download_API >=5.1.

    The sidecar owns the imported Douyin identity and all platform protocol
    details. This adapter only consumes its normalized REST models.
    """

    def __init__(self, client: httpx.Client | None = None):
        self.url = os.getenv("DK_SIDECAR_URL", "http://127.0.0.1:8000").rstrip("/")
        self.key = os.getenv("DK_SIDECAR_API_KEY", "")
        self.identity = os.getenv("DK_SIDECAR_IDENTITY", "")
        self.client = client or httpx.Client(timeout=30)

    def _get(self, path: str, params: dict) -> dict:
        response = self.client.get(
            self.url + path,
            params=params,
            headers={"X-API-Key": self.key},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(
                f"Sidecar request failed: {payload.get('error', {}).get('code', 'unknown')}"
            )
        for _ in range(30):
            if response.status_code != 202:
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ValueError("Sidecar response has no data object")
                return data
            task_id = payload.get("data", {}).get("task_id")
            if not task_id:
                raise ValueError("Sidecar async response has no task ID")
            time.sleep(1)
            response = self.client.get(
                self.url + f"/api/v1/tasks/{task_id}",
                headers={"X-API-Key": self.key},
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success"):
                raise RuntimeError(
                    f"Sidecar task failed: {payload.get('error', {}).get('code', 'unknown')}"
                )
            task = payload.get("data", {})
            if task.get("state") == "failed":
                raise RuntimeError("Sidecar task failed")
            if task.get("state") == "done":
                data = task.get("data")
                if not isinstance(data, dict):
                    raise ValueError("Sidecar task result has no data object")
                return data
        raise TimeoutError("Sidecar task did not finish within 30 seconds")

    def _pages(self, path: str, params: dict):
        cursor = None
        seen = set()
        for _ in range(1000):
            query = {**params, "wait": 20}
            if cursor:
                query["cursor"] = cursor
            data = self._get(path, query)
            items = data.get("items")
            if not isinstance(items, list):
                raise ValueError("Sidecar page has no items list")
            yield items
            next_cursor = data.get("cursor")
            if not data.get("has_more"):
                return
            if not next_cursor or next_cursor in seen:
                raise ValueError("Sidecar pagination cursor missing or repeated")
            seen.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError("Sidecar pagination exceeded 1000 pages")

    def list_saves(self) -> list[CapturedSource]:
        if not self.key or not self.identity:
            raise RuntimeError(
                "Set DK_SIDECAR_API_KEY and DK_SIDECAR_IDENTITY for private Douyin saves"
            )
        folders = [
            folder
            for page in self._pages(
                "/api/v1/douyin/user/collections", {"identity": self.identity, "count": 50}
            )
            for folder in page
        ]
        records: dict[str, CapturedSource] = {}
        for folder in folders:
            folder_id, folder_name = str(folder["collection_id"]), str(folder["name"])
            for page in self._pages(
                "/api/v1/douyin/collection/posts",
                {"identity": self.identity, "collection_id": folder_id, "count": 50},
            ):
                for post in page:
                    external_id = str(post["content_id"])
                    author = post.get("author") or {}
                    record = records.get(external_id)
                    if record is None:
                        record = CapturedSource(
                            external_id=external_id,
                            url=post.get("web_url") or "",
                            title=post.get("title") or "",
                            caption=post.get("description") or "",
                            creator_id=author.get("uid") or "",
                            creator_name=author.get("nickname") or "",
                            collection=folder_name,
                            collections=[folder_name],
                        )
                        records[external_id] = record
                    elif folder_name not in record.collections:
                        record.collections.append(folder_name)
        return list(records.values())
