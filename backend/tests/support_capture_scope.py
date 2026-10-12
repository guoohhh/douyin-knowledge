"""Explicit demo target selection for tests that expect a populated corpus."""

from __future__ import annotations

from typing import Any

from douyin_knowledge.capture.fixture_provider import FixtureCaptureProvider


def select_fixture_collections(client: Any) -> None:
    ids = [item.external_collection_id for item in FixtureCaptureProvider().list_collections()]
    response = client.put(
        "/api/sources/capture-scope",
        json={"schema_version": 1, "platform": "douyin", "default_favorites": False,
              "named_collection_ids": ids},
    )
    assert response.status_code == 200, response.text
