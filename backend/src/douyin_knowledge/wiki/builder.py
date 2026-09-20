"""Wiki page building: compose, diff, and commit revisions.

This module owns every write to ``wiki_pages`` / ``wiki_revisions`` /
``wiki_supports`` / ``wiki_links``. The invariants it enforces:

* **The Wiki is derived state (WIKI-002).** Nothing here is a source of truth;
  ``rebuild_all`` can reconstruct every page from the spine. That is why pages
  are keyed on ``canonical_key`` (a stable function of the subject) rather than
  on the title — a renamed entity must revise its page, not orphan it.
* **Revisions are immutable (WIKI-009).** A change appends a revision and moves
  the ``is_current`` flag. Content that did not change writes nothing at all,
  because a no-op revision would make the audit trail unreadable.
* **Support is statement-level (WIKI-004).** Supports are rewritten per revision
  from the composer's statement keys, so a citation always belongs to the exact
  revision that made the assertion.

Claim selection goes through the DB-004 currency filter. A reprocessed source
must not leave its superseded claims baked into a wiki page: the page would keep
asserting something the system has already stopped believing, and the citation
would still resolve, which is worse than a dangling link.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.text import content_hash, normalize_identity
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim, Entity, EntityAlias
from douyin_knowledge.db.models.knowledge import Topic
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.wiki import (
    WikiLink,
    WikiPage,
    WikiRevision,
    WikiSupport,
)
from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.policy.reconciler import HIDDEN_ACTIONS
from douyin_knowledge.wiki.composer import (
    PAGE_TYPE_ENTITY,
    PAGE_TYPE_TOPIC,
    ComposedPage,
    compose_page,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

logger = get_logger(__name__)

MIN_CLAIMS_FOR_PAGE = 1
"""A single citable claim is enough. The alternative — waiting for corroboration
— means a one-off save produces nothing, which is precisely the case this
product exists to solve."""


@dataclass
class BuildOutcome:
    """What building one page actually did."""

    page_id: str
    canonical_key: str
    title: str
    created: bool = False
    revised: bool = False
    revision_no: int = 0
    statement_count: int = 0
    support_count: int = 0
    skipped_reason: str | None = None


@dataclass
class BuildStats:
    pages_created: int = 0
    pages_revised: int = 0
    pages_unchanged: int = 0
    pages_skipped: int = 0
    outcomes: list[BuildOutcome] = field(default_factory=list)

    def record(self, outcome: BuildOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.skipped_reason:
            self.pages_skipped += 1
        elif outcome.created:
            self.pages_created += 1
        elif outcome.revised:
            self.pages_revised += 1
        else:
            self.pages_unchanged += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "pages_created": self.pages_created,
            "pages_revised": self.pages_revised,
            "pages_unchanged": self.pages_unchanged,
            "pages_skipped": self.pages_skipped,
        }


def entity_canonical_key(entity: Entity) -> str:
    """Stable page identity for an entity.

    Uses the entity id, not the name: renaming 好运茶餐厅 must revise the same
    page. Names are display data; identity belongs to the spine.
    """
    return f"entity:{entity.id}"


def topic_canonical_key(topic: Topic) -> str:
    return f"topic:{topic.id}"


def slugify(text: str) -> str:
    """URL-safe slug that survives Chinese titles.

    CJK has no word boundaries to hyphenate, so a pure-ASCII slugifier returns
    an empty string and every Chinese page collides. Non-ASCII characters are
    kept verbatim — SQLite and modern URL encoding both handle them.
    """
    normalized = normalize_identity(text)
    kept = [c if (c.isalnum() or c in "-_") else "-" for c in normalized]
    slug = "".join(kept).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "page"


class WikiBuilder:
    """Compose and commit wiki pages from the current knowledge spine."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------ currency

    def _current_runs(self) -> dict[str, str]:
        """Current run per source, restricted to sources policy allows.

        The wiki is a derived view, so an excluded source must stop contributing statements
        the next time a page is composed. Because the wiki is rebuildable rather than a
        source of truth, reversing the exclusion and recomposing restores the statement
        without any of the underlying claims having been touched (DEC-015).
        """
        rows = self.session.execute(
            select(
                SourceProcessingState.source_id,
                SourceProcessingState.current_processing_run_id,
            ).where(
                SourceProcessingState.current_processing_run_id.is_not(None),
                SourceProcessingState.current_policy_action.not_in(sorted(HIDDEN_ACTIONS)),
            )
        )
        return {row[0]: row[1] for row in rows if row[1]}

    def _live_claims(self, stmt) -> list[Claim]:
        """Run a claim query and keep only rows from each source's current run."""
        current = self._current_runs()
        if not current:
            return []
        stmt = stmt.join(Source, Source.id == Claim.source_id).where(
            Source.locally_deleted_at_ms.is_(None)
        )
        return [
            claim
            for claim in self.session.scalars(stmt)
            if current.get(claim.source_id) == claim.processing_run_id
        ]

    def claims_for_entity(self, entity_id: str) -> list[Claim]:
        """Claims where the entity is the subject *or* the object.

        Object-position claims matter: "好运茶餐厅 near 港大" is real knowledge
        about 港大 too, and a wiki that only reads subject position produces
        place pages that know nothing about what is near them.
        """
        return self._live_claims(
            select(Claim).where(
                (Claim.subject_entity_id == entity_id) | (Claim.object_entity_id == entity_id)
            )
        )

    def claims_for_topic(self, topic_id: str) -> list[Claim]:
        return self._live_claims(
            select(Claim).where(
                (Claim.subject_topic_id == topic_id) | (Claim.object_topic_id == topic_id)
            )
        )

    # --------------------------------------------------------------- pages

    def _aliases(self, entity_id: str) -> list[str]:
        return list(
            self.session.scalars(
                select(EntityAlias.alias).where(EntityAlias.entity_id == entity_id)
            )
        )

    def _get_or_create_page(
        self,
        *,
        canonical_key: str,
        page_type: str,
        title: str,
        entity_id: str | None = None,
        topic_id: str | None = None,
        source_id: str | None = None,
    ) -> tuple[WikiPage, bool]:
        page = self.session.scalars(
            select(WikiPage).where(WikiPage.canonical_key == canonical_key)
        ).one_or_none()
        if page is not None:
            # Title drift is a revision-worthy change but not a new page.
            page.title = title
            page.slug = slugify(title)
            page.updated_at_ms = now_ms()
            if page.status == "archived":
                page.status = "active"
            return page, False

        page = WikiPage(
            page_type=page_type,
            canonical_key=canonical_key,
            title=title,
            slug=slugify(title),
            entity_id=entity_id,
            topic_id=topic_id,
            source_id=source_id,
            status="active",
        )
        self.session.add(page)
        self.session.flush()
        return page, True

    def _current_revision(self, page_id: str) -> WikiRevision | None:
        return self.session.scalars(
            select(WikiRevision)
            .where(WikiRevision.page_id == page_id)
            .where(WikiRevision.is_current.is_(True))
        ).one_or_none()

    def _next_revision_no(self, page_id: str) -> int:
        latest = self.session.scalars(
            select(WikiRevision.revision_no)
            .where(WikiRevision.page_id == page_id)
            .order_by(WikiRevision.revision_no.desc())
            .limit(1)
        ).first()
        return (latest or 0) + 1

    def commit_revision(
        self,
        page: WikiPage,
        composed: ComposedPage,
        *,
        integration_run_id: str | None = None,
        mutation_type: str = "update_page",
    ) -> BuildOutcome:
        """Append a revision if the composed content differs from the current one."""
        markdown = composed.content_markdown
        digest = content_hash(page.canonical_key, markdown)

        current = self._current_revision(page.id)
        if current is not None and current.content_hash == digest:
            return BuildOutcome(
                page_id=page.id,
                canonical_key=page.canonical_key,
                title=page.title,
                revision_no=current.revision_no,
                statement_count=len(composed.statements),
            )

        if current is not None:
            # Clear the old flag *before* inserting: the partial unique index
            # ux_wiki_revision_current allows exactly one current row per page.
            current.is_current = 0
            self.session.flush()

        revision = WikiRevision(
            page_id=page.id,
            integration_run_id=integration_run_id,
            revision_no=self._next_revision_no(page.id),
            mutation_type="create_page" if current is None else mutation_type,
            content_markdown=markdown,
            summary=composed.summary,
            frontmatter_json=composed.frontmatter,
            content_hash=digest,
            is_current=1,
        )
        self.session.add(revision)
        self.session.flush()

        support_count = self._write_supports(revision, composed)
        page.updated_at_ms = now_ms()

        return BuildOutcome(
            page_id=page.id,
            canonical_key=page.canonical_key,
            title=page.title,
            created=current is None,
            revised=current is not None,
            revision_no=revision.revision_no,
            statement_count=len(composed.statements),
            support_count=support_count,
        )

    def _write_supports(self, revision: WikiRevision, composed: ComposedPage) -> int:
        """Attach statement-level provenance to a freshly inserted revision.

        Supports belong to the revision, not the page, so no deletion is needed:
        the previous revision keeps its own citations and stays auditable.
        """
        count = 0
        for statement in composed.statements:
            for claim_id in statement.claim_ids:
                self.session.add(
                    WikiSupport(
                        wiki_revision_id=revision.id,
                        statement_key=statement.key,
                        claim_id=claim_id,
                        support_role="supports",
                    )
                )
                count += 1
        # Page-level provenance for the synthesized summary, which by design is
        # not reducible to a single claim (WIKI.md 7).
        for source_id in composed.frontmatter.get("source_ids", []):
            self.session.add(
                WikiSupport(
                    wiki_revision_id=revision.id,
                    statement_key="summary",
                    source_id=source_id,
                    support_role="context",
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------ entities

    def build_entity_page(
        self,
        entity: Entity,
        *,
        integration_run_id: str | None = None,
    ) -> BuildOutcome:
        if entity.merged_into_entity_id:
            return BuildOutcome(
                page_id="",
                canonical_key=entity_canonical_key(entity),
                title=entity.canonical_name,
                skipped_reason="entity_merged_away",
            )

        claims = self.claims_for_entity(entity.id)
        if len(claims) < MIN_CLAIMS_FOR_PAGE:
            return BuildOutcome(
                page_id="",
                canonical_key=entity_canonical_key(entity),
                title=entity.canonical_name,
                skipped_reason="no_current_claims",
            )

        composed = compose_page(
            title=entity.canonical_name,
            page_type=PAGE_TYPE_ENTITY,
            claims=claims,
            aliases=self._aliases(entity.id),
            extra_frontmatter={"entity_id": entity.id, "entity_type": entity.entity_type},
        )
        page, _created = self._get_or_create_page(
            canonical_key=entity_canonical_key(entity),
            page_type=PAGE_TYPE_ENTITY,
            title=entity.canonical_name,
            entity_id=entity.id,
        )
        outcome = self.commit_revision(
            page, composed, integration_run_id=integration_run_id
        )
        self._rebuild_links(page, claims)
        return outcome

    def build_topic_page(
        self,
        topic: Topic,
        *,
        integration_run_id: str | None = None,
    ) -> BuildOutcome:
        claims = self.claims_for_topic(topic.id)
        if len(claims) < MIN_CLAIMS_FOR_PAGE:
            return BuildOutcome(
                page_id="",
                canonical_key=topic_canonical_key(topic),
                title=topic.name,
                skipped_reason="no_current_claims",
            )
        composed = compose_page(
            title=topic.name,
            page_type=PAGE_TYPE_TOPIC,
            claims=claims,
            extra_frontmatter={"topic_id": topic.id},
        )
        page, _created = self._get_or_create_page(
            canonical_key=topic_canonical_key(topic),
            page_type=PAGE_TYPE_TOPIC,
            title=topic.name,
            topic_id=topic.id,
        )
        outcome = self.commit_revision(
            page, composed, integration_run_id=integration_run_id
        )
        self._rebuild_links(page, claims)
        return outcome

    # --------------------------------------------------------------- links

    def _rebuild_links(self, page: WikiPage, claims: Sequence[Claim]) -> None:
        """Links are a projection (WIKI-007): delete and re-derive, never patch.

        Relation claims are the only link source. Deriving links from prose
        would make the graph depend on wording, and the graph must stay
        reproducible from structured data alone.
        """
        revision = self._current_revision(page.id)
        if revision is None:
            return

        self.session.query(WikiLink).filter(WikiLink.from_page_id == page.id).delete(
            synchronize_session=False
        )

        related_entity_ids: set[str] = set()
        for claim in claims:
            for candidate in (claim.subject_entity_id, claim.object_entity_id):
                if candidate and candidate != page.entity_id:
                    related_entity_ids.add(candidate)
        if not related_entity_ids:
            return

        target_pages = self.session.scalars(
            select(WikiPage).where(WikiPage.entity_id.in_(related_entity_ids))
        ).all()
        for target in target_pages:
            if target.id == page.id:
                continue
            self.session.add(
                WikiLink(
                    from_page_id=page.id,
                    to_page_id=target.id,
                    link_type="related",
                    source_revision_id=revision.id,
                )
            )

    # ------------------------------------------------------------- batches

    def build_for_entities(
        self,
        entity_ids: Sequence[str],
        *,
        integration_run_id: str | None = None,
    ) -> BuildStats:
        stats = BuildStats()
        if not entity_ids:
            return stats
        entities = self.session.scalars(
            select(Entity).where(Entity.id.in_(list(entity_ids)))
        ).all()
        for entity in entities:
            stats.record(self.build_entity_page(entity, integration_run_id=integration_run_id))
        return stats

    def rebuild_all(self, *, integration_run_id: str | None = None) -> BuildStats:
        """Recompile every page from the spine.

        This is the operational proof of WIKI-002: if the wiki is truly derived,
        dropping it and running this must produce an equivalent wiki. Pages whose
        subject no longer has current claims are archived rather than deleted so
        their revision history stays auditable.
        """
        stats = BuildStats()
        for entity in self.session.scalars(
            select(Entity).where(Entity.merged_into_entity_id.is_(None))
        ).all():
            stats.record(self.build_entity_page(entity, integration_run_id=integration_run_id))
        for topic in self.session.scalars(select(Topic)).all():
            stats.record(self.build_topic_page(topic, integration_run_id=integration_run_id))

        built_keys = {o.canonical_key for o in stats.outcomes if not o.skipped_reason}
        for page in self.session.scalars(
            select(WikiPage).where(WikiPage.status == "active")
        ).all():
            if page.canonical_key not in built_keys:
                page.status = "archived"
                page.updated_at_ms = now_ms()
                logger.info("wiki_page_archived", extra={"page_id": page.id})
        return stats
