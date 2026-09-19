"""Pagination walk and the pruning gate it feeds.

The cases here are the ones where a wrong answer is silent. A walk that stops early
and is then treated as complete does not raise; it marks the unseen items absent and
the user's library quietly shrinks. So each test asserts on the *verdict*
(`safe_to_prune`) rather than merely that the code ran.
"""

from __future__ import annotations

import pytest

from douyin_knowledge.capture.models import CapturedCreator, CapturedSource, SourcePage
from douyin_knowledge.capture.pagination import CollectionWalk, CollectionWalker
from douyin_knowledge.core.errors import ValidationError


def make_source(external_id: str) -> CapturedSource:
    return CapturedSource(
        platform="douyin",
        external_id=external_id,
        source_type="video",
        title=f"video {external_id}",
        caption_raw="",
        source_url=f"https://example.invalid/{external_id}",
        creator=CapturedCreator(external_creator_id="creator-1", display_name="Creator One"),
    )


class ScriptedProvider:
    """Replays a fixed list of pages, or raises in their place.

    Each element is either a `SourcePage` or an exception instance to raise when that
    page is requested. Records the cursors it was asked for, so a test can assert the
    walk passed the opaque cursor back *unchanged* rather than reconstructing one.
    """

    def __init__(self, pages: list[SourcePage | Exception]) -> None:
        self.pages = pages
        self.requested_cursors: list[str | None] = []
        self.requested_limits: list[int] = []

    def list_collection_sources(
        self, external_collection_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> SourcePage:
        self.requested_cursors.append(cursor)
        self.requested_limits.append(limit)
        index = len(self.requested_cursors) - 1
        if index >= len(self.pages):
            raise AssertionError(
                f"walk asked for page {index + 1}; only {len(self.pages)} scripted"
            )
        item = self.pages[index]
        if isinstance(item, Exception):
            raise item
        return item


def drain(
    provider: ScriptedProvider, *, max_pages: int = 500, page_limit: int = 50
) -> CollectionWalker:
    """Run a walk to its end, returning the walker however it ended."""
    walker = CollectionWalker(provider, "col-1", max_pages=max_pages, page_limit=page_limit)
    for _page in walker.pages():
        pass
    return walker


# --------------------------------------------------------------- multi-page walks


def test_walk_follows_multiple_pages_and_is_complete() -> None:
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="c1", has_more=True),
            SourcePage(sources=[make_source("b")], next_cursor="c2", has_more=True),
            SourcePage(sources=[make_source("c")], next_cursor=None, has_more=False),
        ]
    )
    walker = drain(provider)

    assert [s.external_id for s in walker.sources] == ["a", "b", "c"]
    assert walker.walk.pages == 3
    assert walker.walk.complete is True
    assert walker.walk.safe_to_prune is True
    assert walker.walk.stop_reason == "provider_reported_end"


def test_walk_passes_the_opaque_cursor_back_verbatim() -> None:
    """The cursor is not ours to interpret. Douyin's is a stringified millisecond
    timestamp; if anything here parsed or incremented it, the walk would skip items."""
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="1757462400000", has_more=True),
            SourcePage(sources=[make_source("b")], next_cursor=None, has_more=False),
        ]
    )
    drain(provider)

    assert provider.requested_cursors == [None, "1757462400000"]


def test_walk_keeps_page_size_constant_across_pages() -> None:
    """A cursor is only valid for the query that produced it, so a changing `count`
    mid-walk is undefined behavior upstream."""
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="c1", has_more=True),
            SourcePage(sources=[make_source("b")], next_cursor="c2", has_more=True),
            SourcePage(sources=[make_source("c")], next_cursor=None, has_more=False),
        ]
    )
    drain(provider, page_limit=25)

    assert provider.requested_limits == [25, 25, 25]


def test_pages_are_yielded_one_at_a_time_for_incremental_persistence() -> None:
    """The caller persists per page, so each yield must carry only that page -- not
    the accumulated total, which would re-write everything on every page."""
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="c1", has_more=True),
            SourcePage(sources=[make_source("b")], next_cursor=None, has_more=False),
        ]
    )
    walker = CollectionWalker(provider, "col-1")
    yielded = [[s.external_id for s in page] for page in walker.pages()]

    assert yielded == [["a"], ["b"]]


# ------------------------------------------------------------------ last page only


def test_single_page_collection_is_complete() -> None:
    provider = ScriptedProvider(
        [SourcePage(sources=[make_source("a")], next_cursor=None, has_more=False)]
    )
    walker = drain(provider)

    assert walker.walk.pages == 1
    assert walker.walk.safe_to_prune is True


def test_empty_collection_is_complete_not_failed() -> None:
    """An empty collection is a legitimate complete answer, and must stay prunable --
    it is exactly how "I emptied this folder" reaches the database."""
    provider = ScriptedProvider([SourcePage(sources=[], next_cursor=None, has_more=False)])
    walker = drain(provider)

    assert walker.sources == []
    assert walker.walk.safe_to_prune is True


def test_trailing_cursor_on_a_last_page_does_not_continue() -> None:
    """`has_more: false` ends the walk even when a cursor is still present. Following
    it would re-read page 1 and, with a cursor set, never terminate."""
    provider = ScriptedProvider(
        [SourcePage(sources=[make_source("a")], next_cursor="c1", has_more=False)]
    )
    walker = drain(provider)

    assert walker.walk.pages == 1
    assert walker.walk.safe_to_prune is True


# ---------------------------------------------------------------- repeated cursor


def test_repeated_cursor_fails_and_is_not_prunable() -> None:
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="same", has_more=True),
            SourcePage(sources=[make_source("b")], next_cursor="same", has_more=True),
        ]
    )
    walker = CollectionWalker(provider, "col-1")
    with pytest.raises(ValidationError, match="repeated a pagination cursor"):
        for _page in walker.pages():
            pass

    assert walker.walk.complete is False
    assert walker.walk.safe_to_prune is False
    assert walker.walk.stop_reason == "cursor_repeated"
    # The pages we did read are still on the walk: they are real observations.
    assert [s.external_id for s in walker.sources] == ["a", "b"]


def test_cycling_cursors_are_caught_not_just_immediate_repeats() -> None:
    """A -> B -> A defeats a check that only compares with the previous cursor."""
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="c1", has_more=True),
            SourcePage(sources=[make_source("b")], next_cursor="c2", has_more=True),
            SourcePage(sources=[make_source("c")], next_cursor="c1", has_more=True),
        ]
    )
    with pytest.raises(ValidationError, match="repeated a pagination cursor"):
        drain(provider)


def test_has_more_without_a_cursor_is_unfinishable_not_complete() -> None:
    provider = ScriptedProvider(
        [SourcePage(sources=[make_source("a")], next_cursor=None, has_more=True)]
    )
    walker = CollectionWalker(provider, "col-1")
    with pytest.raises(ValidationError, match="gave no cursor"):
        for _page in walker.pages():
            pass

    assert walker.walk.safe_to_prune is False
    assert walker.walk.stop_reason == "has_more_without_cursor"


def test_page_cap_raises_rather_than_truncating() -> None:
    """Truncate-and-prune is the data-loss case. Failing loudly is the fix."""
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source(str(i))], next_cursor=f"c{i}", has_more=True)
            for i in range(10)
        ]
    )
    walker = CollectionWalker(provider, "col-1", max_pages=3)
    with pytest.raises(ValidationError, match="exceeded 3 pages"):
        for _page in walker.pages():
            pass

    assert walker.walk.pages == 3
    assert walker.walk.safe_to_prune is False
    assert walker.walk.stop_reason == "page_cap_exceeded"


# ------------------------------------------------- failure on an intermediate page


def test_failure_mid_walk_leaves_the_walk_not_prunable() -> None:
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="c1", has_more=True),
            RuntimeError("sidecar died on page 2"),
        ]
    )
    walker = CollectionWalker(provider, "col-1")
    with pytest.raises(RuntimeError, match="page 2"):
        for _page in walker.pages():
            pass

    assert walker.walk.complete is False
    assert walker.walk.safe_to_prune is False
    assert [s.external_id for s in walker.sources] == ["a"]


def test_failure_on_the_very_first_page_leaves_an_empty_unprunable_walk() -> None:
    provider = ScriptedProvider([RuntimeError("sidecar unreachable")])
    walker = CollectionWalker(provider, "col-1")
    with pytest.raises(RuntimeError):
        for _page in walker.pages():
            pass

    # Nothing was observed at all, which is emphatically not "the collection is empty".
    assert walker.walk.pages == 0
    assert walker.sources == []
    assert walker.walk.safe_to_prune is False


def test_a_fresh_walk_defaults_to_not_prunable() -> None:
    """The default matters: if a future edit adds an early return that forgets to set
    `complete`, the object still says "do not prune"."""
    assert CollectionWalk(external_collection_id="col-1").safe_to_prune is False
