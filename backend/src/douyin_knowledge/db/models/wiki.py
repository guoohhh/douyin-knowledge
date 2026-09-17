"""Part G - Compounding Wiki (sections 32-40).

The Wiki is derived state: rebuildable from Evidence/Claim/Entity plus the
integration prompt, and versioned so a bad integration can be rolled back.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class WikiIntegrationRun(Base):
    __tablename__ = "wiki_integration_runs"
    __table_args__ = (Index("ix_wiki_integration_runs_started", "started_at_ms"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("wrun"))
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_source_id: Mapped[str | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL")
    )
    processing_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    selected_pages_json: Mapped[Any | None] = mapped_column(JsonText)
    model_name: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    context_stats_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    started_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    finished_at_ms: Mapped[int | None] = mapped_column(Integer)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (
        UniqueConstraint("canonical_key", name="uq_wiki_pages_canonical_key"),
        Index("ix_wiki_pages_page_type_status", "page_type", "status"),
        Index("ix_wiki_pages_entity_id", "entity_id"),
        Index("ix_wiki_pages_topic_id", "topic_id"),
        Index("ix_wiki_pages_slug", "slug"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("wp"))
    page_type: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id", ondelete="SET NULL"))
    topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class WikiRevision(Base):
    """Immutable revision.

    Deviation from PHYSICAL_SCHEMA 34: ``mutation_type`` is added because WIKI.md 18
    requires recording which mutation produced a revision, and without a column the
    audit trail is unrecoverable. See docs/DECISIONS.md (DEC-C7).
    """

    __tablename__ = "wiki_revisions"
    __table_args__ = (
        UniqueConstraint("page_id", "revision_no", name="uq_wiki_revisions_page_revision_no"),
        Index("ix_wiki_revisions_page_current", "page_id", "is_current"),
        # Only one current revision per page (PHYSICAL_SCHEMA 34).
        Index(
            "ux_wiki_revision_current",
            "page_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("wrev"))
    page_id: Mapped[str] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=False
    )
    integration_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("wiki_integration_runs.id", ondelete="SET NULL")
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    mutation_type: Mapped[str] = mapped_column(Text, nullable=False, default="create_page")
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    frontmatter_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class WikiSupport(Base):
    """Statement-level provenance for a Wiki revision (WIKI-004)."""

    __tablename__ = "wiki_supports"
    __table_args__ = (
        Index("ix_wiki_supports_revision_statement", "wiki_revision_id", "statement_key"),
        Index("ix_wiki_supports_claim_id", "claim_id"),
        Index("ix_wiki_supports_source_id", "source_id"),
        CheckConstraint(
            "claim_id IS NOT NULL OR evidence_id IS NOT NULL OR source_id IS NOT NULL",
            name="wiki_supports_target_present",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("wsup"))
    wiki_revision_id: Mapped[str] = mapped_column(
        ForeignKey("wiki_revisions.id", ondelete="CASCADE"), nullable=False
    )
    statement_key: Mapped[str] = mapped_column(Text, nullable=False)
    claim_id: Mapped[str | None] = mapped_column(ForeignKey("claims.id", ondelete="SET NULL"))
    evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_units.id", ondelete="SET NULL")
    )
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    support_role: Mapped[str] = mapped_column(Text, nullable=False, default="supports")
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class WikiLink(Base):
    """Projection - fully rebuildable from current revisions."""

    __tablename__ = "wiki_links"
    __table_args__ = (Index("ix_wiki_links_to_page_id", "to_page_id"),)

    from_page_id: Mapped[str] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), primary_key=True
    )
    to_page_id: Mapped[str] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), primary_key=True
    )
    link_type: Mapped[str] = mapped_column(Text, primary_key=True, default="related")
    source_revision_id: Mapped[str] = mapped_column(
        ForeignKey("wiki_revisions.id", ondelete="CASCADE"), primary_key=True
    )


class WikiLintFinding(Base):
    __tablename__ = "wiki_lint_findings"
    __table_args__ = (
        Index("ix_wiki_lint_findings_status_type", "status", "finding_type"),
        Index("ix_wiki_lint_findings_page_id", "page_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("lint"))
    page_id: Mapped[str | None] = mapped_column(ForeignKey("wiki_pages.id", ondelete="CASCADE"))
    revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("wiki_revisions.id", ondelete="CASCADE")
    )
    finding_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="warning")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    details_json: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False)
    detected_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    resolved_at_ms: Mapped[int | None] = mapped_column(Integer)


class QualityRule(Base):
    """The persistent wrong-answer notebook. Open/monitoring rules enter prompts."""

    __tablename__ = "quality_rules"
    __table_args__ = (
        UniqueConstraint("rule_key", name="uq_quality_rules_rule_key"),
        Index("ix_quality_rules_status_finding_type", "status", "finding_type"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("qr"))
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    finding_type: Mapped[str] = mapped_column(Text, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_triggered_at_ms: Mapped[int | None] = mapped_column(Integer)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class QualityRuleSample(Base):
    __tablename__ = "quality_rule_samples"
    __table_args__ = (Index("ix_quality_rule_samples_rule_fixed", "quality_rule_id", "is_fixed"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("qrs"))
    quality_rule_id: Mapped[str] = mapped_column(
        ForeignKey("quality_rules.id", ondelete="CASCADE"), nullable=False
    )
    finding_id: Mapped[str | None] = mapped_column(
        ForeignKey("wiki_lint_findings.id", ondelete="SET NULL")
    )
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    wiki_page_id: Mapped[str | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="SET NULL")
    )
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    is_fixed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    fixed_at_ms: Mapped[int | None] = mapped_column(Integer)


class QualityLedgerEntry(Base):
    """Append-only repair history."""

    __tablename__ = "quality_ledger"
    __table_args__ = (Index("ix_quality_ledger_created_action", "created_at_ms", "action_type"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("qled"))
    finding_id: Mapped[str | None] = mapped_column(
        ForeignKey("wiki_lint_findings.id", ondelete="SET NULL")
    )
    quality_rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("quality_rules.id", ondelete="SET NULL")
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[Any | None] = mapped_column(JsonText)
    after_json: Mapped[Any | None] = mapped_column(JsonText)
    model_name: Mapped[str | None] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
