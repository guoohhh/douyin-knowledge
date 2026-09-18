"""Ambient diagnostic context.

Every log line emitted inside a ``log_context`` block carries the ids the operator
actually needs when something breaks: which source, which job, which processing run,
which provider (OBS-001).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

# No default: a mutable default on a ContextVar is shared by every context that never
# set one, so a single in-place mutation would leak one job's ids into another's logs.
# Both accessors below already copy rather than mutate, but `None` makes that structural
# instead of a convention the next caller has to know about.
_context: ContextVar[dict[str, Any] | None] = ContextVar("dk_log_context", default=None)


def current_context() -> dict[str, Any]:
    return dict(_context.get() or {})


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    merged = {
        **(_context.get() or {}),
        **{k: v for k, v in fields.items() if v is not None},
    }
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)
