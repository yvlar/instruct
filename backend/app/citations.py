"""Validate provenance, never use retrieval scores as evidence of truth."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REFUSAL = "Information non trouvée dans les instructions disponibles."


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    passage_id: str
    quote: str = Field(min_length=1, max_length=16000)


class GeneratedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answer: str = Field(min_length=1, max_length=12000)
    citations: list[Citation] = Field(max_length=20)


def refusal():
    return {"answer": REFUSAL, "sources": [], "grounded": False}


def validate_answer(content, passages):
    try:
        generated = GeneratedAnswer.model_validate_json(content)
    except (ValidationError, ValueError, TypeError):
        return refusal()
    if (
        not generated.answer.strip()
        or not generated.citations
        or REFUSAL.casefold() in generated.answer.casefold()
    ):
        return refusal()
    known = {p.id: p for p in passages}
    sources, seen = [], set()
    for citation in generated.citations:
        passage = known.get(citation.passage_id)
        if (
            passage is None
            or not citation.quote.strip()
            or citation.quote not in passage.text
        ):
            return refusal()
        if passage.id in seen:
            continue
        seen.add(passage.id)
        sources.append(
            {
                "document": passage.document,
                "page": passage.page,
                "excerpt": citation.quote,
                "score": round(passage.score, 6),
                "passage_id": passage.id,
                "revision": passage.revision,
                "fingerprint": passage.fingerprint,
            }
        )
    return {"answer": generated.answer, "sources": sources, "grounded": True}
