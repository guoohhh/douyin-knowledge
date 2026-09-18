"""Knowledge extraction pipeline: Evidence → EntityMention/Claim.

The extraction layer implements the core NLU/NLP pipeline that transforms
evidence units into structured knowledge:

1. Entity extraction: Find mentions of places, products, people, etc.
2. Claim extraction: Extract assertions (prices, hours, quality judgments)
3. Entity resolution: Merge duplicate entity mentions
4. Claim validation: Detect contradictions and assess confidence

Design principles:
- All extraction is traceable to evidence units
- Confidence scores are preserved throughout
- Contradictions are surfaced, not hidden
- Models are swappable via AI adapter layer
"""

from douyin_knowledge.extraction.claim_extractor import ClaimExtractor
from douyin_knowledge.extraction.entity_extractor import EntityExtractor
from douyin_knowledge.extraction.entity_resolver import EntityResolver
from douyin_knowledge.extraction.orchestrator import ProcessingOrchestrator

__all__ = [
    "EntityExtractor",
    "ClaimExtractor",
    "EntityResolver",
    "ProcessingOrchestrator",
]
