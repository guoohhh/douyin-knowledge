"""Small AI boundary. Extraction receives evidence IDs and must quote source text."""

import json
import os
import re
from typing import Protocol

import httpx
from pydantic import BaseModel, Field


class ExtractedClaim(BaseModel):
    evidence_id: str
    quote: str
    predicate: str = "states"
    value: str
    entity_name: str | None = None
    entity_kind: str = "concept"


class Extraction(BaseModel):
    summary: str
    domain: str = "other"
    form: str = "reference"
    claims: list[ExtractedClaim] = Field(default_factory=list)


class ExtractionProvider(Protocol):
    def extract(self, evidence: list[dict]) -> Extraction: ...


class LocalExtractor:
    def extract(self, evidence: list[dict]) -> Extraction:
        text = " ".join(item["text"] for item in evidence).strip()
        claims = []
        for item in evidence:
            for sentence in re.split(r"[。！？!?\n]", item["text"]):
                sentence = sentence.strip()
                if len(sentence) >= 8:
                    claims.append(
                        ExtractedClaim(evidence_id=item["id"], quote=sentence, value=sentence)
                    )
        return Extraction(summary=text[:240], claims=claims[:20])


class OpenAIExtractor:
    def extract(self, evidence: list[dict]) -> Extraction:
        prompt = "Extract only claims directly supported by the evidence. Use exact evidence_id and quote copied from evidence. Return JSON with summary, domain, form, claims (evidence_id, quote, predicate, value, entity_name, entity_kind). Unknown is preferable to invention."
        response = httpx.post(
            os.getenv("DK_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            + "/chat/completions",
            headers={"Authorization": "Bearer " + os.environ["DK_OPENAI_API_KEY"]},
            json={
                "model": os.getenv("DK_OPENAI_MODEL", "gpt-4.1-mini"),
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
                ],
            },
            timeout=90,
        )
        response.raise_for_status()
        return Extraction.model_validate_json(response.json()["choices"][0]["message"]["content"])


def extractor() -> ExtractionProvider:
    return OpenAIExtractor() if os.getenv("DK_OPENAI_API_KEY") else LocalExtractor()


def synthesize(question: str, scope: str, claims: list[dict]) -> tuple[str, list[str]]:
    if not os.getenv("DK_OPENAI_API_KEY"):
        raise RuntimeError("AI provider is not configured")
    prompt = (
        "Answer in Chinese as JSON: {answer: string, claim_ids: string[]}. "
        "For personal scope, use only the supplied claims and never invent a collection fact. "
        "For hybrid scope, clearly separate collection findings from general context. "
        "For general scope, do not imply that the answer came from the user's collection. "
        "Cite only supplied claim IDs. If evidence is insufficient, say so."
    )
    response = httpx.post(
        os.getenv("DK_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        + "/chat/completions",
        headers={"Authorization": "Bearer " + os.environ["DK_OPENAI_API_KEY"]},
        json={
            "model": os.getenv("DK_OPENAI_MODEL", "gpt-4.1-mini"),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "scope": scope, "claims": claims}, ensure_ascii=False
                    ),
                },
            ],
        },
        timeout=90,
    )
    response.raise_for_status()
    result = json.loads(response.json()["choices"][0]["message"]["content"])
    allowed = {claim["claim_id"] for claim in claims}
    return str(result["answer"]), [item for item in result.get("claim_ids", []) if item in allowed]
