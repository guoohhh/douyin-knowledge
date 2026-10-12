"""Durable daily capture scheduling on the existing SQLite job pipeline.

Only the continuous worker ticks the scheduler.  The one settings row is both the
user's configuration and the last-due checkpoint; SQLite's BEGIN IMMEDIATE makes
concurrent ticks serialize with job enqueueing.  A due slot is consumed once, even
when its job fails.  The user can retry deliberately with manual Sync now.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.scope import initialize_capture_scope
from douyin_knowledge.config.settings import Settings
from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.ops import AppSetting, Job, JobEvent
from douyin_knowledge.jobs.queue import JobQueue
from douyin_knowledge.jobs.types import JobStatus, JobType, Priority

DAILY_SYNC_KEY = "capture.daily_sync"
ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)
SYNC_TYPES = (
    JobType.SYNC_CAPTURE_SCOPE,
    JobType.SYNC_DEFAULT_FAVORITES,
    JobType.SYNC_NAMED_COLLECTION,
    JobType.SYNC_COLLECTIONS,
    JobType.SYNC_COLLECTION_SOURCES,
)


def local_timezone_name() -> str:
    """Choose an IANA zone when available; UTC is an explicit portable fallback."""
    candidates = [os.environ.get("TZ", "")]
    localtime = str(Path("/etc/localtime").resolve())
    if "/zoneinfo/" in localtime:
        candidates.append(localtime.split("/zoneinfo/", 1)[1])
    timezone_file = Path("/etc/timezone")
    if timezone_file.is_file():
        candidates.append(timezone_file.read_text(encoding="utf-8").strip())
    for candidate in candidates:
        try:
            ZoneInfo(candidate)
            return candidate
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return "UTC"


class DailySyncUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    local_time: time = time(3, 0)
    timezone: str = Field(default_factory=local_timezone_name)

    @field_validator("local_time")
    @classmethod
    def minute_precision(cls, value: time) -> time:
        if value.second or value.microsecond or value.tzinfo is not None:
            raise ValueError("daily sync time must be HH:MM without an offset")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA timezone name") from exc
        return value


class DailySyncState(DailySyncUpdate):
    effective_after_ms: int | None = None
    last_due_at_ms: int | None = None
    last_attempt_at_ms: int | None = None
    last_job_id: str | None = None
    last_target_job_ids: list[str] = Field(default_factory=list)
    root_fanout_completed: bool = False
    last_outcome: str | None = None


class DailySyncView(DailySyncState):
    next_run_at_ms: int | None = None


def read_state(session: Session) -> DailySyncState:
    row = session.get(AppSetting, DAILY_SYNC_KEY)
    return DailySyncState.model_validate(row.value_json) if row else DailySyncState()


def _persist(session: Session, state: DailySyncState) -> None:
    row = session.get(AppSetting, DAILY_SYNC_KEY)
    if row is None:
        session.add(AppSetting(key=DAILY_SYNC_KEY, value_json=state.model_dump(mode="json")))
    else:
        row.value_json = state.model_dump(mode="json")
        row.updated_at_ms = now_ms()
    session.flush()


def save_state(session: Session, update: DailySyncUpdate, *, at_ms: int | None = None) -> DailySyncState:
    state = read_state(session)
    changed = any(
        getattr(state, field) != getattr(update, field)
        for field in ("enabled", "local_time", "timezone")
    )
    if changed:
        state.enabled = update.enabled
        state.local_time = update.local_time
        state.timezone = update.timezone
        # A newly enabled or changed schedule starts with the next future slot.
        # Previous failures and outcomes remain inspectable.
        state.effective_after_ms = at_ms if at_ms is not None else now_ms()
    _persist(session, state)
    return state


def _slot(local_date: date, state: DailySyncState) -> int:
    zone = ZoneInfo(state.timezone)
    intended = datetime.combine(local_date, state.local_time, tzinfo=zone)
    # A spring-forward gap maps to the first corresponding valid time after the
    # jump.  An ambiguous fall-back hour uses fold=0, so it runs only once.
    instant = intended.astimezone(timezone.utc)
    if instant.astimezone(zone).replace(tzinfo=None) != intended.replace(tzinfo=None):
        instant = instant.astimezone(zone).astimezone(timezone.utc)
    return int(instant.timestamp() * 1000)


def due_and_next(state: DailySyncState, at_ms: int) -> tuple[int | None, int | None]:
    if not state.enabled:
        return None, None
    today = datetime.fromtimestamp(at_ms / 1000, tz=ZoneInfo(state.timezone)).date()
    today_slot = _slot(today, state)
    if today_slot <= at_ms:
        return today_slot, _slot(today + timedelta(days=1), state)
    return _slot(today - timedelta(days=1), state), today_slot


def view_state(session: Session, *, at_ms: int | None = None) -> DailySyncView:
    state = read_state(session)
    due, next_slot = due_and_next(state, at_ms if at_ms is not None else now_ms())
    if due is not None and due > (state.effective_after_ms or 0) and (
        state.last_due_at_ms is None or due > state.last_due_at_ms
    ):
        next_slot = due  # overdue catch-up is the next expected attempt
    return DailySyncView.model_validate({**state.model_dump(), "next_run_at_ms": next_slot})


def tick(session: Session, settings: Settings, *, at_ms: int | None = None) -> DailySyncState:
    """Submit at most one latest overdue slot, without remote/provider reads."""
    at_ms = at_ms if at_ms is not None else now_ms()
    state = read_state(session)
    due, _ = due_and_next(state, at_ms)
    if due is None or due <= (state.effective_after_ms or 0):
        return state
    if state.last_due_at_ms is not None and due <= state.last_due_at_ms:
        return state

    # Never inherit a queued manual job's possibly paid-AI payload.  Defer until
    # all active capture jobs finish; the next tick will catch up once.
    active = session.scalar(
        select(Job.id).where(Job.job_type.in_(SYNC_TYPES), Job.status.in_(ACTIVE)).limit(1)
    )
    if active is not None:
        return state

    scope = initialize_capture_scope(session, settings)
    state.last_due_at_ms = due
    state.last_attempt_at_ms = at_ms
    state.last_target_job_ids = []
    state.root_fanout_completed = False
    state.last_job_id = None
    if not scope.default_favorites and not scope.named_collection_ids:
        state.last_outcome = "skipped_empty_scope"
    else:
        result = JobQueue(session, lease_ttl_s=settings.worker_lease_ttl_s).enqueue(
            JobType.SYNC_CAPTURE_SCOPE,
            payload={"auto_process": False, "scheduled_due_at_ms": due},
            dedupe_key="sync_capture_scope:douyin",
            priority=Priority.NORMAL,
        )
        state.last_job_id = result.job.id
        state.last_outcome = "queued"
    _persist(session, state)
    return state


def record_targets(session: Session, due_at_ms: int, job_ids: list[str]) -> None:
    state = read_state(session)
    if state.last_due_at_ms != due_at_ms:
        return
    state.last_target_job_ids = job_ids
    _persist(session, state)


def refresh_outcome(session: Session) -> DailySyncState:
    """A root's success means fan-out completed; targets determine sync success."""
    state = read_state(session)
    if not state.last_job_id or state.last_outcome == "skipped_empty_scope":
        return state
    if state.last_outcome in ("succeeded", "failed", "completed_with_skips", "no_targets"):
        return state  # preserve the automatic run's result across later manual retries
    root = session.get(Job, state.last_job_id)
    if root is None:
        return state
    is_scheduled_root = (root.payload_json or {}).get("scheduled_due_at_ms") == state.last_due_at_ms
    if not is_scheduled_root and not state.root_fanout_completed:
        return state  # A later manual re-arm must not invent a scheduled completion.
    was_completed = state.root_fanout_completed
    if is_scheduled_root and root.status in (JobStatus.FAILED, JobStatus.CANCELLED):
        outcome = "failed"
    elif is_scheduled_root and root.status in ACTIVE:
        outcome = root.status
    else:
        state.root_fanout_completed = True
        targets = [session.get(Job, job_id) for job_id in state.last_target_job_ids]
        if any(job is None or job.status in ACTIVE for job in targets):
            outcome = "running"
        elif any(
            job is not None and job.status in (JobStatus.FAILED, JobStatus.CANCELLED)
            for job in targets
        ):
            outcome = "failed"
        else:
            if targets:
                skipped = session.scalar(
                    select(JobEvent.id)
                    .where(
                        JobEvent.job_id.in_(state.last_target_job_ids),
                        JobEvent.event_type == "skipped",
                        JobEvent.created_at_ms >= (state.last_attempt_at_ms or 0),
                    )
                    .limit(1)
                )
                outcome = "completed_with_skips" if skipped else "succeeded"
            else:
                outcome = "no_targets"
    if outcome != state.last_outcome or state.root_fanout_completed != was_completed:
        state.last_outcome = str(outcome)
        _persist(session, state)
    return state
