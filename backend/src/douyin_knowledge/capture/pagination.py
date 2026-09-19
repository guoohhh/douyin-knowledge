"""Walking a paginated collection, and deciding when the walk was *complete*.

This exists as its own module for one reason: membership pruning is the only
destructive operation in capture, and it is gated on a single boolean -- did we see
the whole collection? That boolean is worth isolating and testing directly rather
than inferring it from control flow buried in a sync method.

The failure it prevents is quiet and expensive. If a page walk stops early -- a
network blip on page 3 of 5, a sidecar task expiring, a cursor that stops advancing
-- and the caller treats the pages it *did* get as the complete listing, then every
item on the unseen pages is marked absent from the collection. Nothing errors. The
user's library appears to shrink, and the only trace is a membership flag flipped on
hundreds of rows. So:

    incomplete walk  ->  never prune
    complete walk    ->  prune

and "complete" means the provider explicitly said there was no more, not that we
stopped asking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from douyin_knowledge.core.errors import ValidationError
from douyin_knowledge.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from douyin_knowledge.capture.models import CapturedSource, SourcePage

logger = get_logger(__name__)

#: Default page ceiling. At the sidecar's maximum page size of 50 this is 25k items,
#: comfortably past any real saved collection, so hitting it means the cursor is not
#: advancing rather than that the user is unusually prolific.
DEFAULT_MAX_PAGES = 500


class _PagedProvider(Protocol):
    """Just the one method the walk needs, so tests can pass a stub."""

    def list_collection_sources(
        self, external_collection_id: str, *, cursor: str | None = ..., limit: int = ...
    ) -> SourcePage: ...


@dataclass
class CollectionWalk:
    """The result of walking one collection, including whether it finished.

    ``complete`` starts ``False`` and is only ever set by the walk reaching a
    provider-stated end. That default is the safe one: if an exception unwinds
    through the walk, or a future edit adds an early ``return``, the object a caller
    is holding still says "do not prune".
    """

    external_collection_id: str
    sources: list[CapturedSource] = field(default_factory=list)
    pages: int = 0
    complete: bool = False
    #: Why the walk stopped, for logs and for the job event. Not a status code.
    stop_reason: str = "not_started"

    @property
    def safe_to_prune(self) -> bool:
        """Read as: may we mark unseen memberships absent?

        Named for the decision rather than the state, because `walk.complete` at a
        call site invites "complete enough".
        """
        return self.complete


class CollectionWalker:
    """Walks one collection's pages, holding the verdict where callers can read it.

    Deliberately not a bare generator. The caller needs the verdict *after* iteration
    ends -- including when it ends by exception -- so it has to live somewhere the
    caller already holds a reference to:

        walker = CollectionWalker(provider, "col-1")
        try:
            for page in walker.pages():
                persist(page)
        finally:
            prune_if(walker.walk.safe_to_prune)

    The `finally` is the point: a mid-walk failure unwinds through `pages()`, and
    `walker.walk.complete` is still `False`, so the pruning branch is skipped without
    the caller needing to catch anything.
    """

    def __init__(
        self,
        provider: _PagedProvider,
        external_collection_id: str,
        *,
        page_limit: int = 50,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self.provider = provider
        self.page_limit = page_limit
        self.max_pages = max_pages
        self.walk = CollectionWalk(external_collection_id=external_collection_id)

    @property
    def sources(self) -> list[CapturedSource]:
        """Everything seen so far, complete walk or not."""
        return self.walk.sources

    def pages(self) -> Iterator[list[CapturedSource]]:
        """Yield each page's sources, accumulating into ``self.walk``.

        Raises `ValidationError` on a cursor that does not advance and on exceeding
        ``max_pages``. Both are contract violations rather than transient conditions:
        retrying a non-advancing cursor re-fetches the same page forever. Neither is
        retryable, so neither should look like a capture outage to the job queue.
        """
        walk = self.walk
        collection_id = walk.external_collection_id
        cursor: str | None = None
        # Every cursor already *followed*. A provider cycling A -> B -> A would defeat
        # a check that only compares against the immediately previous cursor.
        seen: set[str] = set()

        while True:
            if walk.pages >= self.max_pages:
                walk.stop_reason = "page_cap_exceeded"
                # Loud, not truncated-with-a-warning. The behavior this replaced
                # logged a warning and then pruned against the partial listing, which
                # is precisely the data-loss case this module exists to prevent.
                raise ValidationError(
                    f"collection {collection_id!r} exceeded {self.max_pages} pages; "
                    "the cursor is probably not advancing",
                    collection_id=collection_id,
                    pages=walk.pages,
                )

            page = self.provider.list_collection_sources(
                collection_id, cursor=cursor, limit=self.page_limit
            )
            walk.pages += 1
            walk.sources.extend(page.sources)
            yield list(page.sources)

            if not page.has_more:
                walk.complete = True
                walk.stop_reason = "provider_reported_end"
                return

            if page.next_cursor is None:
                # `has_more: true` with no cursor. There is no way to ask for the
                # rest, so this is not an ending -- it is an unfinishable walk, and
                # calling it complete would prune everything we never got to see.
                walk.stop_reason = "has_more_without_cursor"
                raise ValidationError(
                    f"collection {collection_id!r} reported more pages but gave no cursor",
                    collection_id=collection_id,
                    pages=walk.pages,
                )

            if page.next_cursor in seen:
                walk.stop_reason = "cursor_repeated"
                raise ValidationError(
                    f"collection {collection_id!r} repeated a pagination cursor; "
                    "the walk cannot advance",
                    collection_id=collection_id,
                    pages=walk.pages,
                )

            seen.add(page.next_cursor)
            cursor = page.next_cursor


def log_walk(walk: CollectionWalk, **extra: Any) -> None:
    """Emit the walk verdict at a level matching its consequence."""
    payload = {
        "collection": walk.external_collection_id,
        "pages": walk.pages,
        "sources": len(walk.sources),
        "complete": walk.complete,
        "stop_reason": walk.stop_reason,
        **extra,
    }
    if walk.complete:
        logger.info("collection_walk_complete", extra=payload)
    else:
        logger.warning("collection_walk_incomplete", extra=payload)


__all__ = [
    "DEFAULT_MAX_PAGES",
    "CollectionWalk",
    "CollectionWalker",
    "log_walk",
]
