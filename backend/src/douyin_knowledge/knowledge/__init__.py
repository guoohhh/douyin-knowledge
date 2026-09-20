"""Shared definition of what counts as *current, normal* knowledge.

Every surface that presents knowledge as knowledge -- retrieval, the wiki composer, the
search index, the Entity/Knowledge API, Resurface -- has to answer the same question:
may this claim be shown right now? Before this module each surface answered it with its
own hand-rolled query, and they had already drifted: the Knowledge API filtered on the
processing-run pointer alone, so an excluded source vanished from Ask and search while
its claims stayed on entity pages and in claim counts.

The eligibility rule lives here once so the surfaces cannot disagree again.
"""

from douyin_knowledge.knowledge.eligibility import (
    current_eligible_runs,
    eligible_claim_ids,
    eligible_claims,
    is_source_eligible,
)

__all__ = [
    "current_eligible_runs",
    "eligible_claims",
    "eligible_claim_ids",
    "is_source_eligible",
]
