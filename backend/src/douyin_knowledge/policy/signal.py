"""The text and metadata a policy rule is allowed to look at.

Policy previously matched against `Source.title` and `Source.caption_raw` directly, and
that is why the documented hashtag rule (PROCESSING_POLICY.md 5.5, "hashtag contains
'综艺' → metadata_only") could never fire. Hashtags are captured -- `CapturedSource`
carries them and `CapturedSource.text_signal()` was written to join them into one
haystack -- but nothing persists them as a column and nothing called `text_signal()`.
They survive only inside `SourceSnapshot.raw_json`, so a matcher reading the ORM row saw
a caption that usually does not repeat its own tags.

Assembling the signal once, in one place, fixes that without a migration and gives cheap
triage and the metadata matcher the *same* input. Two components that classify the same
video from different text will eventually disagree, and the disagreement would show up as
"why was this skipped?" answering differently from "what type is this?".

The signal is a plain frozen dataclass rather than an ORM row on purpose: triage is pure
text work, and being able to construct one by hand is what makes the classifier testable
without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.core.text import content_hash
from douyin_knowledge.db.models.capture import Creator, Source, SourceSnapshot

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class SourceSignal:
    """Everything known about a source before any expensive processing."""

    source_id: str
    title: str | None = None
    caption: str | None = None
    hashtags: tuple[str, ...] = ()
    duration_ms: int | None = None
    source_type: str = "video"
    creator_name: str | None = None
    statistics: dict[str, int] = field(default_factory=dict)

    @property
    def haystack(self) -> str:
        """Lowercased text for substring matching, hashtags included.

        Tags are emitted both bare and `#`-prefixed so a rule can be written either way.
        A user typing `综艺` and a user typing `#综艺` mean the same thing, and making them
        behave differently would be a trap rather than a feature.

        The creator name is *not* in here, even though the signal carries it. It is not
        part of `fingerprint`, so including it would let a rename silently change a
        classification while the cached label stayed valid -- the two must agree on what
        the inputs are. Rules that want the name use `creator_name_contains`, which reads
        the field directly.
        """
        parts = [self.title or "", self.caption or ""]
        for tag in self.hashtags:
            bare = tag.lstrip("#")
            parts.append(bare)
            parts.append(f"#{bare}")
        return "\n".join(p for p in parts if p).casefold()

    @property
    def fingerprint(self) -> str:
        """Identity of the *inputs* a classification was derived from.

        Triage results are cached against this, so re-titled or re-tagged sources get
        reclassified while a plain re-sync of identical metadata does not pay for it
        again. Creator name is deliberately excluded: it is not a property of the item,
        and a creator renaming their account should not invalidate every classification.
        """
        return content_hash(
            "signal",
            self.title or "",
            self.caption or "",
            "\n".join(sorted(t.lstrip("#") for t in self.hashtags)),
            str(self.duration_ms or ""),
            self.source_type,
        )


def build_signal(session: Session, source: Source) -> SourceSignal:
    """Assemble the policy signal for one persisted source.

    Hashtags come from the newest snapshot rather than the source row, because that is
    the only place they are stored. A source with no snapshot yet -- possible for rows
    written by older code paths -- simply has no tags, which degrades to the old
    title/caption behaviour instead of failing.
    """
    raw = _latest_raw(session, source)
    hashtags = tuple(str(t) for t in (raw.get("hashtags") or []) if str(t).strip())
    statistics = {
        str(k): int(v)
        for k, v in (raw.get("statistics") or {}).items()
        if isinstance(v, int | float)
    }

    return SourceSignal(
        source_id=source.id,
        title=source.title,
        caption=source.caption_raw,
        hashtags=hashtags,
        duration_ms=source.duration_ms,
        source_type=source.source_type,
        creator_name=_creator_name(session, source),
        statistics=statistics,
    )


def _latest_raw(session: Session, source: Source) -> dict[str, Any]:
    snapshot: SourceSnapshot | None = None
    if source.latest_snapshot_id:
        snapshot = session.get(SourceSnapshot, source.latest_snapshot_id)
    if snapshot is None:
        # `latest_snapshot_id` is a convenience pointer, not a constraint, so fall back to
        # the newest row by time rather than treating a stale pointer as "no snapshot".
        snapshot = session.scalars(
            select(SourceSnapshot)
            .where(SourceSnapshot.source_id == source.id)
            .order_by(SourceSnapshot.fetched_at_ms.desc())
            .limit(1)
        ).first()
    if snapshot is None or not isinstance(snapshot.raw_json, dict):
        return {}
    return snapshot.raw_json


def _creator_name(session: Session, source: Source) -> str | None:
    if source.creator is not None:
        return source.creator.display_name
    if not source.creator_id:
        return None
    creator = session.get(Creator, source.creator_id)
    return creator.display_name if creator else None


__all__ = ["SourceSignal", "build_signal"]
