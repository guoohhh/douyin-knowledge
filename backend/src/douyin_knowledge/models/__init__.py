"""Domain models package.

Models are organized by conceptual layer:
- source: Captured content from platforms
- evidence: First-class extracted information
- knowledge: Structured understanding (entities, claims)
- wiki: Compounding knowledge entries
- conversation: Queries and answers
- policy: Processing rules and configuration
"""

from __future__ import annotations

# Re-export all models for convenient imports
from douyin_knowledge.db.models.capture import Collection, Creator, Source
from douyin_knowledge.db.models.conversation import (
    Conversation,
    ConversationState,
    Message,
    MessageCitation,
)
from douyin_knowledge.db.models.entities import (
    Claim,
    ClaimEvidence,
    Entity,
    EntityMention,
)
from douyin_knowledge.db.models.knowledge import Topic
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun
from douyin_knowledge.db.models.userstate import UserAnnotation
from douyin_knowledge.db.models.wiki import WikiPage, WikiRevision

__all__ = [
    # source
    "Source",
    "Creator",
    "Collection",
    # evidence
    "EvidenceUnit",
    "ProcessingRun",
    # knowledge
    "EntityMention",
    "Entity",
    "Claim",
    "ClaimEvidence",
    "Topic",
    # wiki
    "WikiPage",
    "WikiRevision",
    "UserAnnotation",
    # conversation
    "Conversation",
    "ConversationState",
    "Message",
    "MessageCitation",
]
