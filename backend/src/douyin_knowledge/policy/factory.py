"""One place that decides how a `PolicyEvaluator` is wired.

The evaluator now has an optional triage service which in turn has an optional model, and
there are five call sites (job handler, reconciler, two API routes, CLI). Letting each one
assemble the parts itself is how the job handler ends up willing to pay for a model call
while the reconciler silently is not, which would make a rule's effect depend on *which
surface* triggered the evaluation rather than on the rule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from douyin_knowledge.policy.evaluator import PolicyEvaluator
from douyin_knowledge.policy.repository import PolicyRepository
from douyin_knowledge.policy.triage import CheapTriage
from douyin_knowledge.policy.triage_service import TriageService

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from douyin_knowledge.config.settings import Settings


def build_triage(session: Session, settings: Settings | None = None) -> TriageService:
    """A triage service, with a model attached only if one is both configured and enabled.

    Under `DK_AI_PROVIDER=mock` no model is attached at all, even with the flag on. The
    mock structured model returns a canned payload, so attaching it would produce
    confident-looking labels that mean nothing -- the same defect `get_answer_chat_model`
    already avoids for answers (DEC-006).
    """
    model = None
    model_name = None
    if (
        settings is not None
        and settings.enable_triage_model_fallback
        and settings.ai_provider != "mock"
    ):
        from douyin_knowledge.ai.registry import get_structured_model

        model = get_structured_model(settings)
        model_name = settings.model_for_role("triage")

    return TriageService(session, CheapTriage(model=model, model_name=model_name))


def build_evaluator(session: Session, settings: Settings | None = None) -> PolicyEvaluator:
    """The evaluator every surface should use."""
    triage = build_triage(session, settings)
    return PolicyEvaluator(
        PolicyRepository(session),
        session,
        triage=triage,
        allow_triage_model=triage.triage.model is not None,
    )


__all__ = ["build_evaluator", "build_triage"]
