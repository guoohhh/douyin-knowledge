"""Tests for FixtureCaptureProvider."""

from __future__ import annotations

import pytest

from douyin_knowledge.capture.fixture_provider import FixtureCaptureProvider


@pytest.fixture
def provider() -> FixtureCaptureProvider:
    return FixtureCaptureProvider()


def test_health(provider: FixtureCaptureProvider) -> None:
    health = provider.health()
    assert health.ok is True
    assert health.name == "fixture"


def test_list_collections(provider: FixtureCaptureProvider) -> None:
    collections = provider.list_collections()
    assert len(collections) == 3
    assert collections[0].name == "香港吃喝"
    assert collections[1].name == "技术学习"
    assert collections[2].name == "随手存"


def test_list_collection_sources(provider: FixtureCaptureProvider) -> None:
    page = provider.list_collection_sources("col_food_hk", cursor=None, limit=10)
    assert len(page.sources) == 2
    assert page.sources[0].title == "港大附近5家值得吃的店"
    assert page.sources[1].title == "又去了一次好运茶餐厅"
    assert page.has_more is False
    assert page.next_cursor is None


def test_fetch_source(provider: FixtureCaptureProvider) -> None:
    source = provider.fetch_source("v_hku_food_list")
    assert source.external_id == "v_hku_food_list"
    assert source.title == "港大附近5家值得吃的店"
    assert source.creator.display_name == "港岛食堂"
    assert len(source.media) == 2  # video + inline subtitle


def test_pagination(provider: FixtureCaptureProvider) -> None:
    page1 = provider.list_collection_sources("col_tech", cursor=None, limit=2)
    assert len(page1.sources) == 2
    assert page1.has_more is True
    assert page1.next_cursor == "2"

    page2 = provider.list_collection_sources("col_tech", cursor=page1.next_cursor, limit=2)
    assert len(page2.sources) == 2
    assert page2.has_more is False


def test_mark_unavailable(provider: FixtureCaptureProvider) -> None:
    from douyin_knowledge.core.errors import SourceUnavailable

    provider.mark_unavailable("v_hku_food_list")
    with pytest.raises(SourceUnavailable):
        provider.fetch_source("v_hku_food_list")


def test_remove_from_collection(provider: FixtureCaptureProvider) -> None:
    page_before = provider.list_collection_sources("col_food_hk", cursor=None, limit=10)
    assert len(page_before.sources) == 2

    provider.remove_from_collection("col_food_hk", "v_hku_food_list")

    page_after = provider.list_collection_sources("col_food_hk", cursor=None, limit=10)
    assert len(page_after.sources) == 1
    assert page_after.sources[0].external_id != "v_hku_food_list"

