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

_context: ContextVar[dict[str, Any]] = ContextVar("dk_log_context", default={})


def current_context() -> dict[str, Any]:
    return dict(_context.get())


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    merged = {**_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)
