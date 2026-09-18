"""Rebuildable FTS and compact local vector projection."""

import hashlib
import math
import os
import re

import httpx
from sqlalchemy import select, text

from .models import Claim, Evidence, KnowledgeItem, Source, VectorDocument

SIZE = 256


def vectorize(value: str) -> list[float]:
    value = value.casefold()
    tokens = re.findall(r"[\u4e00-\u9fff]|[a-z0-9]+", value)
    features = tokens + [tokens[i] + tokens[i + 1] for i in range(len(tokens) - 1)]
    result = [0.0] * SIZE
    for feature in features:
        digest = hashlib.blake2b(feature.encode(), digest_size=4).digest()
        result[int.from_bytes(digest, "little") % SIZE] += 1
    norm = math.sqrt(sum(v * v for v in result)) or 1
    return [v / norm for v in result]


def embed(value: str) -> tuple[str, list[float]]:
    if not os.getenv("DK_OPENAI_API_KEY"):
        return "local-hash-v1", vectorize(value)
    model = os.getenv("DK_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    response = httpx.post(
        os.getenv("DK_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/embeddings",
        headers={"Authorization": "Bearer " + os.environ["DK_OPENAI_API_KEY"]},
        json={"model": model, "input": value},
        timeout=60,
    )
    response.raise_for_status()
    return model, response.json()["data"][0]["embedding"]


def index_text(session, source: Source, item: KnowledgeItem) -> str:
    evidence = session.scalars(
        select(Evidence)
        .join(Claim, Claim.evidence_id == Evidence.id)
        .where(Claim.run_id == source.current_run_id)
        .distinct()
    ).all()
    return " ".join(
        [source.title, source.caption, source.transcript, item.summary]
        + [unit.text for unit in evidence]
    )


def rebuild(session):
    session.execute(text("DELETE FROM search_fts"))
    session.query(VectorDocument).delete()
    count = 0
    for source in session.scalars(select(Source).where(Source.status == "ready")):
        item = session.scalar(
            select(KnowledgeItem).where(KnowledgeItem.run_id == source.current_run_id)
        )
        if not item:
            continue
        content = index_text(session, source, item)
        session.execute(
            text("INSERT INTO search_fts(source_id,text) VALUES (:id,:text)"),
            {"id": source.id, "text": content},
        )
        model, vector = embed(content[:4000])
        session.add(VectorDocument(source_id=source.id, model=model, vector=vector))
        count += 1
    session.commit()
    return count


def index_one(session, source: Source, item: KnowledgeItem):
    content = index_text(session, source, item)
    session.execute(text("DELETE FROM search_fts WHERE source_id=:id"), {"id": source.id})
    session.execute(
        text("INSERT INTO search_fts(source_id,text) VALUES (:id,:text)"),
        {"id": source.id, "text": content},
    )
    row = session.get(VectorDocument, source.id)
    model, vector = embed(content[:4000])
    if row:
        row.model, row.vector = model, vector
    else:
        session.add(VectorDocument(source_id=source.id, model=model, vector=vector))
