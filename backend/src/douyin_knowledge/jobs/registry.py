"""Job handler registry.

Handlers are plain functions taking a :class:`JobContext`. Registration is explicit
(``register_default_handlers``) rather than import-time magic so the worker's
behaviour is greppable and tests can install a subset.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from douyin_knowledge.config import Settings
from douyin_knowledge.core.errors import ConfigurationError
from douyin_knowledge.db.models.ops import Job
from douyin_knowledge.jobs.queue import JobQueue


@dataclass(slots=True)
class JobContext:
    job: Job
    session: Session
    queue: JobQueue
    settings: Settings

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload_json or {}

    def emit(self, event_type: str, message: str | None = None, **data: Any) -> None:
        self.queue.log(self.job, event_type, message, data or None)


JobHandler = Callable[[JobContext], None]


@dataclass
class HandlerRegistry:
    handlers: dict[str, JobHandler] = field(default_factory=dict)

    def register(self, job_type: str, handler: JobHandler) -> None:
        self.handlers[str(job_type)] = handler

    def get(self, job_type: str) -> JobHandler:
        handler = self.handlers.get(job_type)
        if handler is None:
            raise ConfigurationError(f"no handler registered for job type {job_type!r}")
        return handler

    def known_types(self) -> list[str]:
        return sorted(self.handlers)
