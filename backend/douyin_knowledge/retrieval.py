import os
import re

from sqlalchemy import select, text

from .ai import synthesize
from .index import embed
from .models import Claim, Entity, Evidence, KnowledgeItem, Message, Source, VectorDocument


def plan(question: str) -> dict:
    personal = any(
        word in question for word in ("我收藏", "我存", "我的收藏", "之前收藏", "saved", "my saves")
    )
    general = any(
        word in question for word in ("一般来说", "不看我的", "通用知识", "general knowledge")
    )
    hybrid = any(word in question for word in ("结合我的收藏和", "结合我收藏的", "hybrid"))
    scope = "hybrid" if hybrid else "general" if general else "personal"
    intent = (
        "synthesize"
        if any(word in question for word in ("综合", "共同", "比较", "对比"))
        else "find"
        if any(word in question for word in ("找", "哪些", "有没有", "是不是"))
        else "answer"
    )
    return {"scope": scope, "intent": intent, "query": question, "personal_explicit": personal}


def _terms(question: str) -> list[str]:
    cleaned = question
    for phrase in (
        "我收藏的视频里",
        "我收藏过的",
        "我收藏的",
        "我的收藏",
        "我之前",
        "有没有",
        "哪些",
        "关于",
        "综合",
        "怎么",
        "什么",
        "介绍",
        "帮我",
        "之前",
        "收藏",
        "视频",
        "the",
        "my",
        "saved",
    ):
        cleaned = cleaned.replace(phrase, " ")
    return re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{2,}", cleaned)


def search(session, question: str, limit: int = 8) -> list[dict]:
    terms = _terms(question)
    scores = {}
    methods = {}
    for source in session.scalars(select(Source).where(Source.status == "ready")):
        blob = " ".join([source.title, source.caption, source.transcript]).casefold()
        score = sum(3 for term in terms if term.casefold() in blob)
        if score:
            scores[source.id] = score
            methods.setdefault(source.id, set()).add("structured")
    for claim, entity, source in session.execute(
        select(Claim, Entity, Source)
        .join(Entity, Claim.entity_id == Entity.id)
        .join(Source, Claim.source_id == Source.id)
        .where(Claim.run_id == Source.current_run_id, Source.status == "ready")
    ):
        if any(term.casefold() in entity.name.casefold() for term in terms):
            scores[source.id] = scores.get(source.id, 0) + 5
            methods.setdefault(source.id, set()).add("entity")
    if terms:
        query = " OR ".join('"' + term.replace('"', "") + '"' for term in terms)
        rows = session.execute(
            text(
                "SELECT source_id, bm25(search_fts) AS rank FROM search_fts WHERE search_fts MATCH :query LIMIT 30"
            ),
            {"query": query},
        ).all()
        for source_id, rank in rows:
            if session.get(Source, source_id).status != "ready":
                continue
            scores[source_id] = scores.get(source_id, 0) + 2 + min(3, abs(rank))
            methods.setdefault(source_id, set()).add("fts")
    query_model, query_vector = embed(" ".join(terms) or question)
    for doc in session.scalars(select(VectorDocument)):
        if doc.model != query_model:
            continue
        if session.get(Source, doc.source_id).status != "ready":
            continue
        similarity = sum(a * b for a, b in zip(query_vector, doc.vector))
        if similarity >= (0.3 if query_model == "local-hash-v1" else 0.45):
            scores[doc.source_id] = scores.get(doc.source_id, 0) + similarity * 3
            methods.setdefault(doc.source_id, set()).add("semantic")
    output = []
    for source_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]:
        source = session.get(Source, source_id)
        if source.status != "ready":
            continue
        item = session.scalar(
            select(KnowledgeItem).where(KnowledgeItem.run_id == source.current_run_id)
        )
        claims = session.scalars(select(Claim).where(Claim.run_id == source.current_run_id)).all()
        cited = []
        for claim in claims:
            ev = session.get(Evidence, claim.evidence_id)
            cited.append(
                {
                    "claim_id": claim.id,
                    "text": claim.value,
                    "evidence_id": ev.id,
                    "evidence_text": ev.text,
                    "evidence_kind": ev.kind,
                    "source_id": source.id,
                    "source_url": source.url,
                    "source_title": source.title,
                }
            )
        output.append(
            {
                "source_id": source.id,
                "title": source.title,
                "url": source.url,
                "summary": item.summary if item else "",
                "score": round(score, 3),
                "methods": sorted(methods.get(source_id, [])),
                "claims": cited,
            }
        )
    return output


def answer(session, question: str) -> dict:
    query_plan = plan(question)
    if query_plan["scope"] == "general":
        body = (
            synthesize(question, "general", [])[0]
            if os.getenv("DK_OPENAI_API_KEY")
            else "此问题要求通用知识；本地演示模式不生成通用模型回答。请配置 AI 提供方。"
        )
        result = {
            "scope": "general",
            "answer": body,
            "citations": [],
            "results": [],
            "plan": query_plan,
        }
    else:
        results = search(session, question)
        citations = [claim for result in results for claim in result["claims"]]
        if citations:
            if os.getenv("DK_OPENAI_API_KEY"):
                body, cited_ids = synthesize(question, query_plan["scope"], citations)
                citations = [claim for claim in citations if claim["claim_id"] in cited_ids]
                if not citations and query_plan["scope"] == "personal":
                    body = "在已处理的收藏中没有找到足够的可引用证据。"
            else:
                lines = [f"根据你收藏的内容，找到 {len(results)} 个相关来源："]
                for result in results:
                    statements = result["claims"][:3]
                    if statements:
                        lines.append(
                            f"- {result['title']}：" + "；".join(c["text"] for c in statements)
                        )
                body = "\n".join(lines)
        else:
            body = "在已处理的收藏中没有找到足够的可引用证据。"
        result = {
            "scope": query_plan["scope"],
            "answer": body,
            "citations": citations,
            "results": results,
            "plan": query_plan,
        }
    session.add(
        Message(
            question=question,
            answer=result["answer"],
            scope=result["scope"],
            citations=[
                {
                    "claim_id": c["claim_id"],
                    "evidence_id": c["evidence_id"],
                    "source_id": c["source_id"],
                }
                for c in result["citations"]
            ],
        )
    )
    session.commit()
    return result
