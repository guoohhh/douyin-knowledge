"""Identifier helpers.

All primary keys in this system are opaque strings. We use a short, sortable,
URL-safe id: 8 hex chars of millisecond timestamp + 12 random hex chars, prefixed
with a type tag so that a stray id in a log line is self-describing.
"""

from __future__ import annotations

import secrets
import time


def new_id(prefix: str) -> str:
    ts = int(time.time() * 1000) & 0xFFFFFFFFFF
    return f"{prefix}_{ts:010x}{secrets.token_hex(6)}"


def new_run_id() -> str:
    return new_id("run")


def new_job_id() -> str:
    return new_id("job")
