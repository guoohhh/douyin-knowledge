from sqlalchemy import select

from .models import ProcessingRule, Source, SourceCollectionMembership

PRIORITY = {"source": 0, "creator": 2, "collection": 3, "semantic_type": 4, "keyword": 5}


def evaluate(session, source: Source) -> tuple[str, str]:
    rules = session.scalars(select(ProcessingRule).where(ProcessingRule.enabled.is_(True))).all()
    collections = set(
        session.scalars(
            select(SourceCollectionMembership.collection_name).where(
                SourceCollectionMembership.source_id == source.id
            )
        ).all()
    )
    collections.add(source.collection)
    matches = []
    for rule in rules:
        matched = (
            (rule.dimension == "source" and rule.value == source.external_id)
            or (rule.dimension == "creator" and rule.value == source.creator_id)
            or (rule.dimension == "collection" and rule.value in collections)
            or (rule.dimension == "semantic_type" and rule.value == source.semantic_type)
            or (
                rule.dimension == "keyword"
                and rule.value.casefold() in (source.title + " " + source.caption).casefold()
            )
        )
        if matched:
            rank = (
                0
                if rule.dimension == "source"
                else 1
                if rule.action == "always_process"
                else PRIORITY[rule.dimension]
            )
            matches.append((rank, rule.created_at, rule))
    if not matches:
        return "process", "default"
    rule = min(matches, key=lambda item: (item[0], item[1]))[2]
    return rule.action, f"rule:{rule.id} ({rule.dimension}={rule.value})"
