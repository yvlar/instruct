"""Deterministic rank fusion and whole-passage context selection."""

import json
from dataclasses import dataclass

from .lexical import exact_match


@dataclass(frozen=True)
class Passage:
    id: str
    document: str
    revision: str
    fingerprint: str
    page: int
    text: str
    score: float = 0.0

    @classmethod
    def from_point(cls, point):
        p = point.payload
        return cls(
            str(point.id),
            p["document"],
            p["revision"],
            p["fingerprint"],
            p["page"],
            p["text"],
        )

    def context(self):
        # Delimit untrusted PDF text without pretending document names are instructions.
        return json.dumps(
            {
                "id": self.id,
                "document": self.document,
                "page": self.page,
                "revision": self.revision,
                "text": self.text,
            },
            ensure_ascii=False,
        )


def fuse(question, semantic, lexical, *, rrf_k=60):
    passages, scores = {}, {}
    for results in (semantic, lexical):
        seen = set()
        for rank, passage in enumerate(results, 1):
            if passage.id in seen:
                continue
            seen.add(passage.id)
            passages.setdefault(passage.id, passage)
            scores[passage.id] = scores.get(passage.id, 0.0) + 1.0 / (rrf_k + rank)
    ranked = sorted(
        passages.values(),
        key=lambda p: (
            not exact_match(question, p.text),
            -scores[p.id],
            p.document,
            p.page,
            p.id,
        ),
    )
    # A changed PDF can contain duplicate text blocks; keep one reference per page.
    result, seen = [], set()
    for p in ranked:
        key = (p.document, p.revision, p.page, p.text)
        if key not in seen:
            seen.add(key)
            result.append(
                Passage(
                    p.id,
                    p.document,
                    p.revision,
                    p.fingerprint,
                    p.page,
                    p.text,
                    scores[p.id],
                )
            )
    return result


def select_context(passages, *, max_passages, max_chars):
    selected, used = [], 0
    for passage in passages:
        cost = len(passage.context()) + (2 if selected else 0)
        # Never skip the highest-ranked exact hit to make room for a shorter neighbour.
        # No excerpt truncation: a cut-off safety step would be misleading.
        if used + cost > max_chars:
            break
        selected.append(passage)
        used += cost
        if len(selected) == max_passages:
            break
    return selected
