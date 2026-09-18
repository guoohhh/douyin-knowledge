"""Wiki integration runs: route, integrate, lint, record.

``WikiUpdater`` is the transactional boundary around a wiki mutation. It owns the
``wiki_integration_runs`` lifecycle so every page revision can be traced to the
run — and therefore to the processing run and source — that caused it (WIKI-009).

Stage 1 (route/select) is deliberately code-owned, per WIKI-006/WIKI-007: the
candidate page set is derived from resolved entity mentions on the processed
source, not proposed by a model. A model that picks its own targets can edit a
page the user never connected to this video, and there is no cheap way to notice.

Stage 2 (integrate) currently composes deterministically. That is a real
capability, not a placeholder: the composer produces grounded, cited pages with
no API key. LLM prose refinement layers on top of this skeleton later and must
validate back against the same claim ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.entities import EntityMention
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.wiki import (
    WikiIntegrationRun,
    WikiLintFinding,
    WikiPage,
    WikiRevision,
    WikiSupport,
)
from douyin_knowledge.extraction.entity_resolver import STATUS_NEW, STATUS_RESOLVED
from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.wiki.builder import BuildStats, WikiBuilder

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

logger = get_logger(__name__)

TRIGGER_SOURCE_PROCESSED = "source_processed"
TRIGGER_MANUAL_REBUILD = "manual_rebuild"
TRIGGER_ENTITY_MERGE = "entity_merge"

PROMPT_VERSION = "wiki-deterministic-1"

TRUSTED_RESOLUTION_STATUSES = (STATUS_RESOLVED, STATUS_NEW)
"""Mention statuses whose entity link is safe to compile into the Wiki."""


@dataclass
class IntegrationResult:
    run_id: str
    status: str
    trigger_type: str
    stats: dict[str, int] = field(default_factory=dict)
    candidate_entity_ids: list[str] = field(default_factory=list)
    page_ids: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "trigger_type": self.trigger_type,
            "stats": self.stats,
            "candidate_entity_ids": self.candidate_entity_ids,
            "page_ids": self.page_ids,
            "findings": self.findings,
            "error": self.error,
        }


class WikiUpdater:
    """Drive wiki integration runs."""

    def __init__(self, session: Session, *, model_name: str | None = None) -> None:
        self.session = session
        self.builder = WikiBuilder(session)
        self.model_name = model_name

    # ------------------------------------------------------------ stage 1

    def select_candidate_entities(self, source_id: str) -> list[str]:
        """Entities this source actually resolved to.

        ``resolved`` (matched an existing identity) and ``new`` (created one from
        the normalized name) both qualify: in each case the link rests on exact
        normalized identity, which ENT-002 designates the only trustworthy merge
        signal.

        ``ambiguous`` is excluded. Those mentions carry fuzzy candidates and a
        deliberately NULL ``resolved_entity_id``; writing one into a wiki page
        would launder a guess into compiled knowledge that then looks
        authoritative and cites cleanly.
        """
        rows = self.session.execute(
            select(EntityMention.resolved_entity_id)
            .where(EntityMention.source_id == source_id)
            .where(EntityMention.resolved_entity_id.is_not(None))
            .where(EntityMention.resolution_status.in_(TRUSTED_RESOLUTION_STATUSES))
        )
        return list(dict.fromkeys(row[0] for row in rows if row[0]))

    # ------------------------------------------------------------ stage 2

    def integrate_source(
        self,
        source_id: str,
        *,
        processing_run_id: str | None = None,
    ) -> IntegrationResult:
        """Run wiki integration for one freshly processed source."""
        if processing_run_id is None:
            state = self.session.get(SourceProcessingState, source_id)
            processing_run_id = state.current_processing_run_id if state else None

        run = WikiIntegrationRun(
            trigger_type=TRIGGER_SOURCE_PROCESSED,
            trigger_source_id=source_id,
            processing_run_id=processing_run_id,
            status="running",
            model_name=self.model_name,
            prompt_version=PROMPT_VERSION,
        )
        self.session.add(run)
        self.session.flush()

        try:
            entity_ids = self.select_candidate_entities(source_id)
            run.selected_pages_json = {"entity_ids": entity_ids}
            if not entity_ids:
                # Not an error: a source can legitimately resolve to nothing
                # nameable. Recording "skipped" keeps the audit trail honest
                # instead of implying work happened.
                return self._finish(
                    run,
                    status="skipped",
                    stats={},
                    entity_ids=[],
                    context={"reason": "no_resolved_entities"},
                )

            stats = self.builder.build_for_entities(entity_ids, integration_run_id=run.id)
            page_ids = [o.page_id for o in stats.outcomes if o.page_id]
            findings = self.lint_pages(page_ids, run_id=run.id)
            return self._finish(
                run,
                status="succeeded",
                stats=stats.as_dict(),
                entity_ids=entity_ids,
                page_ids=page_ids,
                context={"findings": len(findings)},
                findings=findings,
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.session.rollback()
            run = self.session.merge(run)
            run.status = "failed"
            run.finished_at_ms = now_ms()
            run.error_json = {"type": type(exc).__name__, "message": str(exc)}
            logger.exception("wiki_integration_failed", extra={"source_id": source_id})
            return IntegrationResult(
                run_id=run.id,
                status="failed",
                trigger_type=TRIGGER_SOURCE_PROCESSED,
                error=str(exc),
            )

    def rebuild_all(self) -> IntegrationResult:
        """Full recompile — the recovery path when composition logic changes."""
        run = WikiIntegrationRun(
            trigger_type=TRIGGER_MANUAL_REBUILD,
            status="running",
            model_name=self.model_name,
            prompt_version=PROMPT_VERSION,
        )
        self.session.add(run)
        self.session.flush()

        stats: BuildStats = self.builder.rebuild_all(integration_run_id=run.id)
        page_ids = [o.page_id for o in stats.outcomes if o.page_id]
        findings = self.lint_pages(page_ids, run_id=run.id)
        return self._finish(
            run,
            status="succeeded",
            stats=stats.as_dict(),
            entity_ids=[],
            page_ids=page_ids,
            context={"findings": len(findings)},
            findings=findings,
        )

    def _finish(
        self,
        run: WikiIntegrationRun,
        *,
        status: str,
        stats: dict[str, int],
        entity_ids: Sequence[str],
        page_ids: Sequence[str] = (),
        context: dict[str, Any] | None = None,
        findings: Sequence[str] = (),
    ) -> IntegrationResult:
        run.status = status
        run.finished_at_ms = now_ms()
        run.context_stats_json = {**stats, **(context or {})}
        logger.info(
            "wiki_integration_finished",
            extra={"run_id": run.id, "status": status, **stats},
        )
        return IntegrationResult(
            run_id=run.id,
            status=status,
            trigger_type=run.trigger_type,
            stats=stats,
            candidate_entity_ids=list(entity_ids),
            page_ids=list(page_ids),
            findings=list(findings),
        )

    # --------------------------------------------------------------- lint

    def lint_pages(self, page_ids: Sequence[str], *, run_id: str | None = None) -> list[str]:
        """Wiki Lint V1: structural and provenance checks only.

        Findings are stored, not raised. A page with a lint finding is still more
        useful than no page, and the quality loop (WIKI-008) is designed around
        surfacing defects for repair rather than blocking writes.
        """
        findings: list[str] = []
        for page_id in dict.fromkeys(page_ids):
            page = self.session.get(WikiPage, page_id)
            if page is None:
                continue
            revision = self.session.scalars(
                select(WikiRevision)
                .where(WikiRevision.page_id == page_id)
                .where(WikiRevision.is_current.is_(True))
            ).one_or_none()

            if revision is None:
                findings.append(self._record_finding(page, None, "missing_current_revision", "error"))
                continue

            support_count = len(
                self.session.scalars(
                    select(WikiSupport.id).where(WikiSupport.wiki_revision_id == revision.id)
                ).all()
            )
            if support_count == 0:
                # An uncited page is the failure mode this whole architecture
                # exists to prevent, so it is an error, not a warning.
                findings.append(
                    self._record_finding(page, revision, "revision_without_support", "error")
                )

            body = revision.content_markdown or ""
            if "## 已知信息" not in body:
                findings.append(
                    self._record_finding(page, revision, "empty_body_sections", "warning")
                )
            if not page.title.strip():
                findings.append(self._record_finding(page, revision, "missing_title", "error"))

        if findings and run_id:
            logger.info("wiki_lint_findings", extra={"run_id": run_id, "count": len(findings)})
        return findings

    def _record_finding(
        self,
        page: WikiPage,
        revision: WikiRevision | None,
        finding_type: str,
        severity: str,
    ) -> str:
        """Insert a finding, reopening rather than duplicating a known one."""
        existing = self.session.scalars(
            select(WikiLintFinding)
            .where(WikiLintFinding.page_id == page.id)
            .where(WikiLintFinding.finding_type == finding_type)
            .where(WikiLintFinding.status == "open")
        ).first()
        if existing is not None:
            existing.revision_id = revision.id if revision else None
            existing.detected_at_ms = now_ms()
            return existing.id

        finding = WikiLintFinding(
            page_id=page.id,
            revision_id=revision.id if revision else None,
            finding_type=finding_type,
            severity=severity,
            status="open",
            details_json={"page_title": page.title, "canonical_key": page.canonical_key},
        )
        self.session.add(finding)
        self.session.flush()
        return finding.id

    def resolve_findings(self, page_id: str) -> int:
        """Close open findings for a page — called after a successful revision."""
        findings = self.session.scalars(
            select(WikiLintFinding)
            .where(WikiLintFinding.page_id == page_id)
            .where(WikiLintFinding.status == "open")
        ).all()
        for finding in findings:
            finding.status = "resolved"
            finding.resolved_at_ms = now_ms()
        return len(findings)
