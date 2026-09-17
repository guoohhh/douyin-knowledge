"""SQLAlchemy declarative base and shared column types."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, MetaData, Text
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names keep Alembic autogenerate stable on SQLite,
# where unnamed constraints cannot be dropped.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} {pk}>"


# All `*_json` columns in PHYSICAL_SCHEMA are TEXT holding JSON. Using SQLAlchemy's
# JSON type over a TEXT affinity gives us transparent (de)serialization while the
# on-disk representation stays exactly what the schema documents.
JsonText: Any = JSON().with_variant(JSON(none_as_null=True), "sqlite")
LongText: Any = Text
