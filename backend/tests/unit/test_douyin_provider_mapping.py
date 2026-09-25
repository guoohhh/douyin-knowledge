"""Test the douyin provider's mapping layer without calling the real sidecar.

The transport layer (envelope unwrap, 202 polling, error mapping) is deliberately not
tested here: it needs a live sidecar or a heavyweight mock server, and the value is
low (envelope parsing is trivial, error mapping is a static dict). The fixture
provider already exercises the CaptureProvider protocol end-to-end, so the sync
service's use of the interface is covered. What *does* need verification is that the
dtk shape -> CapturedSource/CapturedCreator/CapturedMedia mapping doesn't silently
drop fields when the sidecar evolves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from douyin_knowledge.capture.douyin_provider import DouyinCaptureProvider


@pytest.fixture
def provider() -> DouyinCaptureProvider:
    """A provider with a fake client so mapping can be tested in isolation."""
    mock_client = MagicMock()
    return DouyinCaptureProvider("http://fake", client=mock_client)


def test_map_author(provider: DouyinCaptureProvider) -> None:
    raw = {
        "platform": "douyin",
        "uid": "MS4wLjABAAAA...",
        "sec_uid": "MS4wLjABAAAA...",
        "unique_id": "testuser",
        "nickname": "测试用户",
        "signature": "个性签名",
        "avatar": {"url": "https://example.com/avatar.jpg"},
        "web_url": "https://www.douyin.com/user/MS4wLjABAAAA...",
        "verified": True,
        "stats": {"follower_count": 1000},
    }
    creator = provider._map_author(raw)
    assert creator.external_creator_id == "MS4wLjABAAAA..."
    assert creator.display_name == "测试用户"
    assert creator.handle == "testuser"
    assert creator.avatar_url == "https://example.com/avatar.jpg"
    assert creator.profile_url == "https://www.douyin.com/user/MS4wLjABAAAA..."
    assert creator.raw == raw


def test_map_media_video(provider: DouyinCaptureProvider) -> None:
    raw = {
        "content_id": "7123456789",
        "kind": "video",
        "duration_ms": 15000,
        "media": {
            "covers": [{"url": "https://example.com/cover.jpg", "width": 720, "height": 1280}],
            "video": {
                "url": "https://example.com/video.mp4",
                "width": 720,
                "height": 1280,
                "size_bytes": 2048000,
            },
        },
    }
    media_list = provider._map_media(raw)
    assert len(media_list) == 2
    cover = media_list[0]
    assert cover.kind == "image"
    assert cover.url == "https://example.com/cover.jpg"
    assert cover.mime_type == "image/jpeg"
    video = media_list[1]
    assert video.kind == "video"
    assert video.url == "https://example.com/video.mp4"
    assert video.mime_type == "video/mp4"
    assert video.width == 720
    assert video.height == 1280
    assert video.duration_ms == 15000
    assert video.byte_size == 2048000


def test_map_media_image_album(provider: DouyinCaptureProvider) -> None:
    raw = {
        "content_id": "7123456789",
        "kind": "image_album",
        "media": {
            "covers": [{"url": "https://example.com/cover.jpg"}],
            "images": [
                {"url": "https://example.com/img1.jpg", "width": 1080, "height": 1080},
                {"url": "https://example.com/img2.jpg", "width": 1080, "height": 1080},
            ],
        },
    }
    media_list = provider._map_media(raw)
    assert len(media_list) == 3  # 1 cover + 2 images
    assert media_list[0].kind == "image"
    assert media_list[0].url == "https://example.com/cover.jpg"
    assert media_list[1].url == "https://example.com/img1.jpg"
    assert media_list[2].url == "https://example.com/img2.jpg"


def test_map_source(provider: DouyinCaptureProvider) -> None:
    raw = {
        "platform": "douyin",
        "content_id": "7123456789012345678",
        "kind": "video",
        "web_url": "https://www.douyin.com/video/7123456789012345678",
        "title": "测试视频",
        "description": "这是一个测试视频 #测试 #抖音",
        "created_at": "2024-01-15T10:30:00+08:00",
        "duration_ms": 15000,
        "is_deleted": False,
        "is_private": False,
        "author": {
            "platform": "douyin",
            "uid": "MS4wLjABAAAA...",
            "nickname": "测试用户",
            "verified": False,
        },
        "stats": {
            "play_count": 10000,
            "digg_count": 500,
            "comment_count": 50,
            "share_count": 20,
            "collect_count": 30,
        },
        "media": {
            "covers": [{"url": "https://example.com/cover.jpg"}],
            "video": {"url": "https://example.com/video.mp4", "width": 720, "height": 1280},
        },
        "tags": ["测试", "抖音"],
        "fetched_at": "2024-01-15T11:00:00+08:00",
    }
    source = provider._map_source(raw)
    assert source.platform == "douyin"
    assert source.external_id == "7123456789012345678"
    assert source.source_type == "video"
    assert source.title == "测试视频"
    assert source.caption_raw == "这是一个测试视频 #测试 #抖音"
    assert source.source_url == "https://www.douyin.com/video/7123456789012345678"
    assert source.cover_url == "https://example.com/cover.jpg"
    expected_ts = int(
        datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone(timedelta(seconds=28800))).timestamp() * 1000
    )
    assert source.published_at_ms == expected_ts
    assert source.saved_at_ms is None
    assert source.duration_ms == 15000
    assert source.availability == "available"
    assert source.creator.display_name == "测试用户"
    assert source.hashtags == ["测试", "抖音"]
    assert source.statistics["play_count"] == 10000
    assert source.statistics["digg_count"] == 500
    assert len(source.media) == 2
    assert source.raw == raw


def test_map_collection(provider: DouyinCaptureProvider) -> None:
    raw = {
        "platform": "douyin",
        "collection_id": "col_123",
        "name": "我的收藏夹",
        "cover": {"url": "https://example.com/collection_cover.jpg"},
        "item_count": 42,
        "is_public": False,
        "owner_id": "MS4wLjABAAAA...",
        "owner_name": "测试用户",
    }
    collection = provider._map_collection(raw)
    assert collection.external_collection_id == "col_123"
    assert collection.name == "我的收藏夹"
    assert collection.item_count == 42
    assert collection.description is None
    assert collection.raw == raw


def test_to_timestamp_ms(provider: DouyinCaptureProvider) -> None:
    # ISO with offset
    ts = provider._to_timestamp_ms("2024-01-15T10:30:00+08:00")
    expected = int(datetime(2024, 1, 15, 2, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert ts == expected
    # Z suffix
    ts_z = provider._to_timestamp_ms("2024-01-15T10:30:00Z")
    expected_z = int(datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert ts_z == expected_z
    # None
    assert provider._to_timestamp_ms(None) is None
    # Invalid
    assert provider._to_timestamp_ms("not-a-date") is None


def test_first_image_url() -> None:
    # Single dict
    assert (
        DouyinCaptureProvider._first_image_url({"url": "https://example.com/img.jpg"})
        == "https://example.com/img.jpg"
    )
    # List
    assert (
        DouyinCaptureProvider._first_image_url(
            [{"url": "https://example.com/1.jpg"}, {"url": "https://example.com/2.jpg"}]
        )
        == "https://example.com/1.jpg"
    )
    # Empty
    assert DouyinCaptureProvider._first_image_url([]) is None
    assert DouyinCaptureProvider._first_image_url(None) is None
    # Missing url key
    assert DouyinCaptureProvider._first_image_url([{"width": 100}]) is None
