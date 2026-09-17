"""Time helpers. The database stores epoch milliseconds everywhere (PHYSICAL_SCHEMA 4)."""

from __future__ import annotations

import datetime as _dt
import time


def now_ms() -> int:
    return int(time.time() * 1000)


def to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.UTC).isoformat()


def from_iso(value: str | None) -> int | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    dt = _dt.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return int(dt.timestamp() * 1000)
