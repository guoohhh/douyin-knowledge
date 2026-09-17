"""Bundled demo dataset.

Synthetic content written for this repository. It contains no real personal
collection data and no real creator identifiers (SEC-003), but it is shaped like the
real thing: mixed Chinese/English, mixed content quality, some items that policy is
supposed to skip, and one multi-entity list video that exercises the whole
Source -> Evidence -> Claim -> Entity -> Wiki chain end to end.

Each item carries an inline ``native_subtitle`` so the demo reaches processing
level 2 without ffmpeg or a real ASR provider.
"""

from __future__ import annotations

from typing import Any

PLATFORM = "douyin"

COLLECTIONS: list[dict[str, Any]] = [
    {
        "external_collection_id": "col_food_hk",
        "name": "香港吃喝",
        "description": "港岛九龙的餐厅收藏",
    },
    {
        "external_collection_id": "col_tech",
        "name": "技术学习",
        "description": "AI / 工程 / 工具",
    },
    {
        "external_collection_id": "col_misc",
        "name": "随手存",
        "description": "娱乐和杂项",
    },
]

CREATORS: list[dict[str, Any]] = [
    {
        "external_creator_id": "cr_hkfoodie",
        "display_name": "港岛食堂",
        "handle": "hkfoodie_demo",
    },
    {
        "external_creator_id": "cr_devnotes",
        "display_name": "开发者手记",
        "handle": "devnotes_demo",
    },
    {
        "external_creator_id": "cr_clipfarm",
        "display_name": "影视剪辑站",
        "handle": "clipfarm_demo",
    },
    {
        "external_creator_id": "cr_travelmin",
        "display_name": "极简旅行",
        "handle": "travelmin_demo",
    },
]

_CREATOR_BY_ID = {c["external_creator_id"]: c for c in CREATORS}


def _source(
    external_id: str,
    creator_id: str,
    title: str,
    caption: str,
    subtitle: str,
    *,
    collections: list[str],
    duration_ms: int = 90_000,
    hashtags: list[str] | None = None,
    saved_at_ms: int,
    published_at_ms: int,
    source_type: str = "video",
) -> dict[str, Any]:
    return {
        "platform": PLATFORM,
        "external_id": external_id,
        "source_type": source_type,
        "title": title,
        "caption_raw": caption,
        "source_url": f"https://example.invalid/{PLATFORM}/{external_id}",
        "cover_url": f"https://example.invalid/{PLATFORM}/{external_id}/cover.jpg",
        "published_at_ms": published_at_ms,
        "saved_at_ms": saved_at_ms,
        "duration_ms": duration_ms,
        "availability": "available",
        "creator": _CREATOR_BY_ID[creator_id],
        "hashtags": hashtags or [],
        "statistics": {"like_count": 1200, "comment_count": 88, "collect_count": 430},
        "media": [
            {"kind": "video", "url": f"https://example.invalid/{external_id}.mp4",
             "mime_type": "video/mp4", "duration_ms": duration_ms, "width": 1080, "height": 1920},
            {"kind": "subtitle", "language": "zh", "mime_type": "text/vtt", "text": subtitle},
        ],
        "raw": {"demo": True},
        "_collections": collections,
    }


# 2026-04-xx timestamps, deliberately spread so "recently saved" queries are meaningful.
_D = 86_400_000
_BASE = 1_774_000_000_000

SOURCES: list[dict[str, Any]] = [
    _source(
        "v_hku_food_list",
        "cr_hkfoodie",
        "港大附近5家值得吃的店",
        "西环到石塘咀，人均都不贵 #香港美食 #港大 #西环",
        # A list video: five entities, prices, opinions, one on-screen-only detail.
        "今天带大家看港大附近五家我自己会回头吃的店。"
        "第一家是好运茶餐厅，就在西边街，叉烧饭人均八十块钱，出餐特别快，中午排队大概十分钟。"
        "第二家是山城米线，人均五十左右，汤底是他们自己熬的，辣度可以调，我一般点小辣。"
        "第三家叫和风居，日式定食，人均一百二，性价比一般，但环境很安静适合一个人吃饭。"
        "第四家是老李牛腩，人均六十五，牛腩炖得很软，不过位置很小只有六个座位，最好避开饭点。"
        "第五家是石塘咀那边的甜香园，糖水人均三十，杨枝甘露做得比连锁店好喝。"
        "五家里面我最推荐好运茶餐厅，性价比最高。",
        collections=["col_food_hk"],
        hashtags=["香港美食", "港大", "西环"],
        duration_ms=185_000,
        saved_at_ms=_BASE - 3 * _D,
        published_at_ms=_BASE - 30 * _D,
    ),
    _source(
        "v_hoyun_revisit",
        "cr_hkfoodie",
        "又去了一次好运茶餐厅",
        "涨价了 #香港美食",
        "上次说好运茶餐厅人均八十，这次去发现叉烧饭涨到九十五了。"
        "味道还是一样好，但性价比我觉得下降了。另外他们现在下午两点到五点休息，别白跑。",
        collections=["col_food_hk"],
        hashtags=["香港美食"],
        duration_ms=62_000,
        saved_at_ms=_BASE - 1 * _D,
        published_at_ms=_BASE - 4 * _D,
    ),
    _source(
        "v_mcp_explained",
        "cr_devnotes",
        "MCP 到底解决了什么问题",
        "三分钟讲清楚 Model Context Protocol #AI #MCP",
        "很多人把 MCP 理解成一个插件市场，我觉得这是最大的误解。"
        "MCP 本质上是一个协议，它统一的是模型怎么发现和调用外部工具这件事。"
        "在 MCP 之前，每接一个数据源你都要写一套自己的适配层，工具描述、权限、错误处理全是自定义的。"
        "MCP 把这层标准化了，所以同一个 server 可以被不同的客户端复用。"
        "我的看法是 MCP 的价值不在能力上限，而在集成成本，它降低的是边际接入成本。"
        "但它现在最大的问题是权限模型太粗，一个 server 拿到的授权范围往往超过它实际需要的。",
        collections=["col_tech"],
        hashtags=["AI", "MCP"],
        duration_ms=210_000,
        saved_at_ms=_BASE - 2 * _D,
        published_at_ms=_BASE - 10 * _D,
    ),
    _source(
        "v_mcp_second_take",
        "cr_devnotes",
        "再聊 MCP：我改主意的地方",
        "补充上次的看法 #MCP #Agent",
        "上次我说 MCP 的价值主要在降低集成成本，这个我还是坚持。"
        "但有一点我改主意了：我原来觉得权限模型粗是小问题，现在觉得这是阻碍它进企业的主要原因。"
        "另外很多人问 MCP 和 function calling 的关系，我的理解是不冲突，"
        "function calling 是模型侧的调用能力，MCP 是服务侧的暴露方式。",
        collections=["col_tech"],
        hashtags=["MCP", "Agent"],
        duration_ms=150_000,
        saved_at_ms=_BASE - 12 * 3_600_000,
        published_at_ms=_BASE - 2 * _D,
    ),
    _source(
        "v_ruff_review",
        "cr_devnotes",
        "Ruff 用了半年的真实感受",
        "从 flake8 迁移的坑 #Python #工具",
        "Ruff 我们生产项目用了半年，先说结论：值得迁，但不要期待零成本。"
        "速度确实快，我们的 CI lint 阶段从四十秒降到不到两秒。"
        "规则覆盖上，flake8 那套插件绝大部分都有对应实现，但少数自定义插件没有替代品。"
        "最容易踩的坑是 isort 的行为差异，第一次跑会产生很大的 diff，建议单独一个 commit。",
        collections=["col_tech"],
        hashtags=["Python", "工具"],
        duration_ms=175_000,
        saved_at_ms=_BASE - 5 * _D,
        published_at_ms=_BASE - 20 * _D,
    ),
    _source(
        "v_movie_clip_1",
        "cr_clipfarm",
        "【电影片段】这段台词太经典了",
        "#影视剪辑 #经典台词",
        "这段是电影里最经典的对话之一，配上音乐真的绝了。",
        collections=["col_misc"],
        hashtags=["影视剪辑", "经典台词"],
        duration_ms=45_000,
        saved_at_ms=_BASE - 6 * _D,
        published_at_ms=_BASE - 40 * _D,
    ),
    _source(
        "v_variety_clip_1",
        "cr_clipfarm",
        "综艺名场面合集",
        "笑到停不下来 #综艺 #搞笑",
        "这期节目里嘉宾的反应真的太真实了。",
        collections=["col_misc"],
        hashtags=["综艺", "搞笑"],
        duration_ms=95_000,
        saved_at_ms=_BASE - 7 * _D,
        published_at_ms=_BASE - 41 * _D,
    ),
    _source(
        "v_kyoto_3day",
        "cr_travelmin",
        "京都三天怎么安排最省力",
        "不赶行程版本 #日本 #京都 #旅行",
        "京都三天我的建议是按区域走，不要按景点排。"
        "第一天东山，清水寺早上七点到，人最少，然后走二年坂三年坂到八坂神社。"
        "第二天岚山，建议坐嵯峨野小火车，单程票大概一千日元，提前一天买就行。"
        "第三天市区，锦市场加二条城，下午留时间买东西。"
        "交通我不建议买巴士一日券，除非你一天坐超过四次，否则按次刷卡更划算。",
        collections=["col_misc", "col_tech"],
        hashtags=["日本", "京都", "旅行"],
        duration_ms=240_000,
        saved_at_ms=_BASE - 9 * _D,
        published_at_ms=_BASE - 60 * _D,
    ),
]

MEMBERSHIPS: dict[str, list[str]] = {}
for _s in SOURCES:
    for _c in _s.pop("_collections"):
        MEMBERSHIPS.setdefault(_c, []).append(_s["external_id"])
