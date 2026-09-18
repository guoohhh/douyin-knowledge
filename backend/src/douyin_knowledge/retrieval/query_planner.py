"""Query planner: analyze queries and select retrieval strategy.

QueryPlanner classifies queries and decides which retrieval paths to use:

Query types:
- factual: "港大附近哪家餐厅人均80?" → wiki lookup + keyword
- recommendation: "推荐港大附近好吃的" → vector search + quality signals
- comparison: "好运和添好运哪个好?" → entity lookup + field comparison
- exploration: "港大周边美食" → hybrid search with topic filter
- clarification: "那家店几点开?" → context + entity resolution

Strategy selection:
- Named entities detected → prioritize entity/wiki lookup
- Subjective terms (好吃, 推荐) → prioritize vector search
- Specific attributes (价格, 时间) → prioritize wiki fields
- Topic queries → combine vector + keyword + topic filter
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class QueryPlan:
    """Execution plan for a query."""

    query_type: str
    """factual | recommendation | comparison | exploration | clarification"""

    strategies: list[str]
    """List of retrieval strategies to use: vector | keyword | wiki | entity"""

    entities: list[str]
    """Detected entity names"""

    intent: str
    """Natural language description of query intent"""

    filters: dict[str, Any]
    """Metadata filters to apply"""

    confidence: float
    """Confidence in query understanding (0-1)"""


class QueryPlanner:
    """Analyze queries and plan retrieval strategy."""

    def __init__(self):
        """Initialize query planner."""
        self.recommendation_keywords = [
            "推荐", "好吃", "值得", "必吃", "网红", "火爆",
            "recommend", "suggestion", "best", "top",
        ]
        self.comparison_keywords = [
            "哪个", "对比", "比较", "vs", "还是",
            "which", "compare", "better", "versus",
        ]

    def plan(self, query: str, context: dict[str, Any] | None = None) -> QueryPlan:
        """Create a retrieval plan for a query.

        Args:
            query: User query
            context: Optional context (conversation history, location, etc.)

        Returns:
            Query plan
        """
        query_lower = query.lower()

        # Detect query type
        query_type = self._classify_query(query_lower)

        # Extract entities
        entities = self._extract_entities(query)

        # Select strategies
        strategies = self._select_strategies(query_type, entities, query_lower)

        # Build filters
        filters = self._build_filters(query_lower, context)

        # Describe intent
        intent = self._describe_intent(query_type, entities, query)

        return QueryPlan(
            query_type=query_type,
            strategies=strategies,
            entities=entities,
            intent=intent,
            filters=filters,
            confidence=0.8,  # Simple fixed confidence for now
        )

    def _classify_query(self, query: str) -> str:
        """Classify query type.

        Args:
            query: Query text (lowercase)

        Returns:
            Query type
        """
        # Check for comparison
        if any(kw in query for kw in self.comparison_keywords):
            return "comparison"

        # Check for recommendation
        if any(kw in query for kw in self.recommendation_keywords):
            return "recommendation"

        # Check for exploration (broad topic queries)
        if any(term in query for term in ["周边", "附近", "一带", "area", "nearby"]):
            return "exploration"

        # Check for clarification (short query with pronouns)
        if any(pronoun in query for pronoun in ["那个", "这个", "它", "that", "this"]):
            return "clarification"

        # Default to factual
        return "factual"

    def _extract_entities(self, query: str) -> list[str]:
        """Extract entity names from query.

        Simple pattern-based extraction for now.
        TODO: Use NER model for better accuracy.

        Args:
            query: Query text

        Returns:
            List of entity names
        """
        entities = []

        # Look for quoted names
        import re
        quoted = re.findall(r'["""\'](.*?)["""\']', query)
        entities.extend(quoted)

        # Look for common restaurant/place name patterns
        # This is very basic - real implementation should use NER
        place_patterns = [
            r'([一-鿿]{2,6}(?:茶餐厅|餐厅|饭店|酒楼|美食|咖啡|茶楼))',
            r'([一-鿿]{2,6}(?:店|坊|馆|轩|阁|楼))',
        ]

        for pattern in place_patterns:
            matches = re.findall(pattern, query)
            entities.extend(matches)

        # Remove duplicates while preserving order
        seen = set()
        unique_entities = []
        for entity in entities:
            if entity not in seen:
                seen.add(entity)
                unique_entities.append(entity)

        return unique_entities

    def _select_strategies(
        self,
        query_type: str,
        entities: list[str],
        query: str,
    ) -> list[str]:
        """Select retrieval strategies based on query analysis.

        Args:
            query_type: Classified query type
            entities: Detected entities
            query: Query text (lowercase)

        Returns:
            List of strategies to use
        """
        strategies = []

        # Strategy selection logic
        if query_type == "factual":
            if entities:
                strategies.append("wiki")
                strategies.append("entity")
            strategies.append("keyword")
            strategies.append("vector")

        elif query_type == "recommendation":
            strategies.append("vector")  # Primary for subjective queries
            if entities:
                strategies.append("entity")
            strategies.append("keyword")

        elif query_type == "comparison":
            if entities:
                strategies.append("wiki")  # Get structured data for comparison
                strategies.append("entity")
            strategies.append("keyword")

        elif query_type == "exploration":
            strategies.append("vector")
            strategies.append("keyword")
            if entities:
                strategies.append("entity")

        elif query_type == "clarification":
            # Depends heavily on context
            if entities:
                strategies.append("entity")
                strategies.append("wiki")
            strategies.append("keyword")

        # Ensure at least one strategy
        if not strategies:
            strategies = ["vector", "keyword"]

        return strategies

    def _build_filters(
        self,
        query: str,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build metadata filters from query and context.

        Args:
            query: Query text (lowercase)
            context: Optional context

        Returns:
            Filter dictionary
        """
        filters: dict[str, Any] = {}

        # Extract time filters
        if "最近" in query or "recent" in query:
            filters["recency"] = "recent"  # Last 30 days
        elif "今天" in query or "today" in query:
            filters["recency"] = "today"

        # Extract location filters from context
        if context and "location" in context:
            filters["location"] = context["location"]

        return filters

    def _describe_intent(
        self,
        query_type: str,
        entities: list[str],
        query: str,
    ) -> str:
        """Generate natural language description of query intent.

        Args:
            query_type: Query type
            entities: Detected entities
            query: Original query

        Returns:
            Intent description
        """
        if query_type == "recommendation":
            if entities:
                return f"User wants recommendations related to {', '.join(entities)}"
            return "User wants general recommendations"

        elif query_type == "comparison":
            if len(entities) >= 2:
                return f"User wants to compare {' and '.join(entities)}"
            return "User wants to compare options"

        elif query_type == "factual":
            if entities:
                return f"User wants factual information about {', '.join(entities)}"
            return "User wants factual information"

        elif query_type == "exploration":
            return "User wants to explore options in an area or topic"

        elif query_type == "clarification":
            return "User is asking for clarification about a previous topic"

        return "User query intent unclear"
