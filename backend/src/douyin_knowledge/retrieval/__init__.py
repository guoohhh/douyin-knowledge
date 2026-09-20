"""Retrieval layer: hybrid search.

The retrieval layer bridges user queries and knowledge:
    Query → Retrieval → Sources/Entities/Wiki → Answer

Retrieval strategies:
- Vector search: semantic similarity over embeddings
- Keyword search: exact/fuzzy text matching
- Hybrid: combine multiple strategies with RRF fusion

Design principles:
- Multiple retrieval paths improve recall
- Results are ranked and deduplicated with RRF
- Citations preserve the evidence chain
- Currency filter (DB-004) ensures only current runs are retrieved
"""

from douyin_knowledge.retrieval.keyword_search import KeywordSearcher
from douyin_knowledge.retrieval.retriever import HybridRetriever
from douyin_knowledge.retrieval.vector_store import VectorStore

__all__ = [
    "VectorStore",
    "KeywordSearcher",
    "HybridRetriever",
]
