"""Policy domain models."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    class StrEnum(str, Enum):
        """Backport of StrEnum for Python 3.10.

        ``__str__`` must be the plain string one. Without it, ``Enum.__str__`` wins and
        ``str(JobType.PROCESS_SOURCE)`` returns "JobType.PROCESS_SOURCE" -- which is
        exactly what the queue would then write into ``jobs.job_type``, so no handler
        would ever match on 3.10 while 3.12 worked fine.
        """

        def __new__(cls, value: str) -> StrEnum:
            obj = str.__new__(cls, value)
            obj._value_ = value
            return obj

        __str__ = str.__str__
        __format__ = str.__format__


class RuleType(StrEnum):
    """Processing rule type."""
    SOURCE = "source"
    CREATOR = "creator"
    COLLECTION = "collection"
    METADATA = "metadata"
    SEMANTIC = "semantic"


class PolicyAction(StrEnum):
    """Policy decision action."""
    PROCESS = "process"
    METADATA_ONLY = "metadata_only"
    ALWAYS_PROCESS = "always_process"
    EXCLUDE = "exclude"


class PolicyPhase(StrEnum):
    """Policy evaluation phase."""
    METADATA = "metadata"
    SEMANTIC = "semantic"
    MANUAL_OVERRIDE = "manual_override"


#: Actions under which a source's knowledge must not reach normal retrieval, the wiki, or
#: current answers. ``metadata_only`` stops before knowledge processing, so its content was
#: never actually understood (PROCESSING_POLICY §12). ``exclude`` is a hard block. Both
#: preserve historical runs/evidence/claims if they exist, so rule reversal restores
#: eligibility without reprocessing, but while the rule is active neither may be cited.
#:
#: Defined here, in the dependency-free module, rather than next to the reconciler that
#: first needed it: `knowledge.eligibility` reads it and the reconciler now drives a wiki
#: recompile, so keeping it in `policy.reconciler` made
#: reconciler -> wiki.builder -> knowledge.eligibility -> reconciler an import cycle.
HIDDEN_ACTIONS = frozenset({PolicyAction.EXCLUDE.value, PolicyAction.METADATA_ONLY.value})


@dataclass(frozen=True)
class ProcessingRule:
    """User-configured or system-suggested processing rule.

    Maps to processing_rules table in PHYSICAL_SCHEMA.md.
    """
    id: str
    name: str | None
    is_enabled: bool
    rule_type: RuleType
    action: PolicyAction
    priority: int
    target_source_id: str | None = None
    target_creator_id: str | None = None
    target_collection_id: str | None = None
    matcher_json: dict[str, Any] | None = None
    origin: str = "user"
    created_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass(frozen=True)
class PolicyDecision:
    """Result of policy evaluation for a source.

    Maps to policy_decisions table in PHYSICAL_SCHEMA.md.
    """
    id: str
    source_id: str
    rule_id: str | None
    phase: PolicyPhase
    action: PolicyAction
    reason_code: str | None = None
    explanation: dict[str, Any] | None = None
    model_name: str | None = None
    created_at_ms: int = 0
