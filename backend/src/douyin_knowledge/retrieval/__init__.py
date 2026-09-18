"""Retrieval layer: hybrid search and query planning.

The retrieval layer bridges user queries and knowledge:
    Query → Retrieval → Sources/Entities/Wiki → Answer

Retrieval strategies:
- Vector search: semantic similarity over embeddings
- Keyword search: exact/fuzzy text matching
- Wiki lookup: direct entity/topic access
- Hybrid: combine multiple strategies with score fusion

Query planner decides which strategy to use based on query type.

Design principles:
- Query understanding comes first (intent, entities, constraints)
- Multiple retrieval paths improve recall
- Results are ranked and deduplicated
- Citations preserve the evidence chain
"""

from douyin_knowledge.retrieval.keyword_search import KeywordSearcher
from douyin_knowledge.retrieval.query_planner import QueryPlanner
from douyin_knowledge.retrieval.retriever import HybridRetriever
from douyin_knowledge.retrieval.vector_store import VectorStore

__all__ = [
    "VectorStore",
    "KeywordSearcher",
    "QueryPlanner",
    "HybridRetriever",
]
