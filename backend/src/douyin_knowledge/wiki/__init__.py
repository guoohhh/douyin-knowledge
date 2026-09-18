"""Wiki integration: compile claims into persistent, cited pages.

    Source -> EvidenceUnit -> Claim -> WikiRevision -> Answer

The Wiki is a *compiled view*, never a source of truth (WIKI-002). Everything in
this package can be thrown away and rebuilt from the spine with
``WikiUpdater.rebuild_all()``, which is what makes it safe to change composition
logic without migrating data.

Layering:

* ``composer`` — pure functions: claims in, markdown + statement keys out.
* ``builder`` — the only writer of pages/revisions/supports/links.
* ``updater`` — the run lifecycle, candidate routing, and lint.
"""

from douyin_knowledge.wiki.builder import BuildOutcome, BuildStats, WikiBuilder
from douyin_knowledge.wiki.composer import ComposedPage, Statement, compose_page
from douyin_knowledge.wiki.updater import IntegrationResult, WikiUpdater

__all__ = [
    "BuildOutcome",
    "BuildStats",
    "ComposedPage",
    "IntegrationResult",
    "Statement",
    "WikiBuilder",
    "WikiUpdater",
    "compose_page",
]
