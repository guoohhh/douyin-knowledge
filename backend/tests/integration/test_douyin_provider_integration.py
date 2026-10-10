"""Integration tests for DouyinCaptureProvider against a live sidecar.

These tests require:
- A running sidecar at DK_DOUYIN_SIDECAR_URL (default http://localhost:8000)
- Valid DK_DOUYIN_SIDECAR_API_KEY if the sidecar requires auth
- A configured identity pool in the sidecar
- Real Douyin collections in the authenticated account

Run with:
    DK_DOUYIN_SIDECAR_URL=http://localhost:8000 pytest tests/integration/ -v

Skip if no sidecar:
    pytest tests/unit/ -v
"""

from __future__ import annotations

import os

import pytest

from douyin_knowledge.capture.douyin_provider import DouyinCaptureProvider

SIDECAR_URL = os.getenv("DK_DOUYIN_SIDECAR_URL")
SIDECAR_API_KEY = os.getenv("DK_DOUYIN_SIDECAR_API_KEY")
SIDECAR_IDENTITY = os.getenv("DK_DOUYIN_SIDECAR_IDENTITY")


@pytest.fixture
def sidecar_url() -> str:
    if not SIDECAR_URL:
        pytest.skip("DK_DOUYIN_SIDECAR_URL not set")
    return SIDECAR_URL


@pytest.fixture
def api_key() -> str | None:
    return SIDECAR_API_KEY


@pytest.fixture
def identity() -> str | None:
    return SIDECAR_IDENTITY


@pytest.fixture
def provider(
    sidecar_url: str, api_key: str | None, identity: str | None
) -> DouyinCaptureProvider:
    return DouyinCaptureProvider(sidecar_url, api_key=api_key, identity=identity)


def test_health(provider: DouyinCaptureProvider) -> None:
    health = provider.health()
    assert health.name == "douyin"
    assert health.ok is True


def test_list_collections(provider: DouyinCaptureProvider) -> None:
    collections = provider.list_collections()
    assert isinstance(collections, list)
    if collections:
        col = collections[0]
        assert col.external_collection_id
        assert col.name


def test_list_collection_sources(provider: DouyinCaptureProvider) -> None:
    collections = provider.list_collections()
    if not collections:
        pytest.skip("no collections in account")
    col_id = collections[0].external_collection_id
    page = provider.list_collection_sources(col_id, limit=5)
    assert isinstance(page.sources, list)
    if page.sources:
        source = page.sources[0]
        assert source.external_id
        assert source.title
        assert source.creator


def test_fetch_source(provider: DouyinCaptureProvider) -> None:
    collections = provider.list_collections()
    if not collections:
        pytest.skip("no collections in account")
    col_id = collections[0].external_collection_id
    page = provider.list_collection_sources(col_id, limit=1)
    if not page.sources:
        pytest.skip("collection is empty")
    external_id = page.sources[0].external_id
    source = provider.fetch_source(external_id)
    assert source.external_id == external_id
    assert source.platform == "douyin"
    assert source.creator
    assert source.media
