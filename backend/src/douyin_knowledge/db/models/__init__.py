"""All ORM models. Importing this module registers every table on ``Base.metadata``."""

from douyin_knowledge.db.base import Base
from douyin_knowledge.db.models.capture import (
    Collection,
    Creator,
    Source,
    SourceAsset,
    SourceCollectionMembership,
    SourceSnapshot,
)
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
    EntityAlias,
    EntityExternalId,
    EntityMention,
    EntityMentionEvidence,
)
from douyin_knowledge.db.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemLabel,
    KnowledgeItemTopic,
    Topic,
)
from douyin_knowledge.db.models.ops import AppSetting, Job, JobEvent
from douyin_knowledge.db.models.policy import (
    PolicyDecision,
    ProcessingRule,
    SourceProcessingState,
)
from douyin_knowledge.db.models.processing import (
    EvidenceUnit,
    ProcessingRun,
    ProcessingRunEvidence,
    RetrievalChunk,
    RetrievalChunkEvidence,
)
from douyin_knowledge.db.models.search import SearchDocument, VectorDocument
from douyin_knowledge.db.models.userstate import (
    EntityUserState,
    KnowledgeItemUserState,
    UserAnnotation,
)
from douyin_knowledge.db.models.wiki import (
    QualityLedgerEntry,
    QualityRule,
    QualityRuleSample,
    WikiIntegrationRun,
    WikiLintFinding,
    WikiLink,
    WikiPage,
    WikiRevision,
    WikiSupport,
)

__all__ = [
    "AppSetting",
    "Base",
    "Claim",
    "ClaimEvidence",
    "Collection",
    "Conversation",
    "ConversationState",
    "Creator",
    "Entity",
    "EntityAlias",
    "EntityExternalId",
    "EntityMention",
    "EntityMentionEvidence",
    "EntityUserState",
    "EvidenceUnit",
    "Job",
    "JobEvent",
    "KnowledgeItem",
    "KnowledgeItemLabel",
    "KnowledgeItemTopic",
    "KnowledgeItemUserState",
    "Message",
    "MessageCitation",
    "PolicyDecision",
    "ProcessingRule",
    "ProcessingRun",
    "ProcessingRunEvidence",
    "QualityLedgerEntry",
    "QualityRule",
    "QualityRuleSample",
    "RetrievalChunk",
    "RetrievalChunkEvidence",
    "SearchDocument",
    "Source",
    "SourceAsset",
    "SourceCollectionMembership",
    "SourceProcessingState",
    "SourceSnapshot",
    "Topic",
    "UserAnnotation",
    "VectorDocument",
    "WikiIntegrationRun",
    "WikiLintFinding",
    "WikiLink",
    "WikiPage",
    "WikiRevision",
    "WikiSupport",
]
