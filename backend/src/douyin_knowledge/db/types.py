"""SQLAlchemy column type annotations and helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from sqlalchemy import JSON
from sqlalchemy.orm import mapped_column

# Type aliases for common column patterns
IntPk = Annotated[int, mapped_column(primary_key=True, autoincrement=True)]
"""Integer primary key with autoincrement"""

JsonDict = Annotated[dict[str, Any], mapped_column(JSON().with_variant(JSON(none_as_null=True), "sqlite"))]
"""JSON column that stores dict, transparently serialized"""


def utcnow() -> datetime:
    """Return current UTC datetime (naive, for SQLite storage)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
