"""Part J/K - Job queue and settings (sections 48-50)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class Job(Base):
    """SQLite-backed durable queue row.

    There is no ``lease_expires_at`` column (ARCHITECTURE 13 prose mentions one, the
    DDL does not). Lease expiry is derived as ``locked_at_ms + lease_ttl`` so a single
    setting can change the timeout without a migration. See DECISIONS DEC-C1.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_jobs_dedupe_key"),
        # The claim query orders by (status, priority DESC, available_at_ms).
        Index("ix_jobs_status_priority_available", "status", "priority", "available_at_ms"),
        Index("ix_jobs_source_id", "source_id"),
        Index("ix_jobs_type_status", "job_type", "status"),
        Index("ix_jobs_locked_at_ms", "locked_at_ms"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("job"))
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    locked_at_ms: Mapped[int | None] = mapped_column(Integer)
    locked_by: Mapped[str | None] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    started_at_ms: Mapped[int | None] = mapped_column(Integer)
    finished_at_ms: Mapped[int | None] = mapped_column(Integer)
    last_error_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job_created", "job_id", "created_at_ms"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("jev"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    data_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class AppSetting(Base):
    """Non-secret user settings. Secrets never land here (SEC-002)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_json: Mapped[Any] = mapped_column(JsonText, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
