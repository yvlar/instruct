import hashlib
import re
from pathlib import Path

import fitz
import httpx
from qdrant_client import QdrantClient, models

from .config import settings


SYSTEM_PROMPT = """Tu es un assistant d'instructions de travail industrielles.
Réponds uniquement avec le CONTEXTE fourni. N'invente jamais une étape, une valeur,
une consigne de sécurité ou un équipement. Si le contexte est insuffisant, réponds
exactement : « Information non trouvée dans les instructions disponibles. »
Cite les sources dans le texte sous la forme [document, p. X]. Réponds en français,
clairement et sans ajouter de connaissance générale. Rappelle de vérifier la version
officielle du document avant d'exécuter une procédure.
"""


def chunk_text(text: str, size: int = 1400, overlap: int = 250) -> list[str]:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []
    chunks, start = [], 0
    while start < len(clean):
        end = min(start + size, len(clean))
        if end < len(clean):
            boundary = clean.rfind(". ", start, end)
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append(clean[start:end])
        if end == len(clean):
            break
        start = max(start + 1, end - overlap)
    return chunks


class KnowledgeBase:
    def __init__(self) -> None:
        self.qdrant = QdrantClient(url=settings.qdrant_url)
        self.http = httpx.AsyncClient(timeout=120)

    async def embed(self, text: str) -> list[float]:
        response = await self.http.post(
            f"{settings.ollama_url}/api/embed",
            json={"model": settings.embedding_model, "input": text},
        )
        response.raise_for_status()
        return response.json()["embeddings"][0]

    async def ensure_collection(self) -> None:
        if self.qdrant.collection_exists(settings.qdrant_collection):
            return
        vector = await self.embed("initialisation")
        self.qdrant.create_collection(
            settings.qdrant_collection,
            vectors_config=models.VectorParams(size=len(vector), distance=models.Distance.COSINE),
        )

    async def ingest(self) -> tuple[int, int]:
        await self.ensure_collection()
        pdfs = sorted(Path(settings.documents_path).rglob("*.pdf"))
        points = []
        for pdf in pdfs:
            with fitz.open(pdf) as doc:
                for page_number, page in enumerate(doc, start=1):
                    for index, chunk in enumerate(chunk_text(page.get_text())):
                        digest = hashlib.sha256(f"{pdf}:{page_number}:{index}:{chunk}".encode()).hexdigest()
                        points.append(models.PointStruct(
                            id=digest,
                            vector=await self.embed(chunk),
                            payload={"document": pdf.name, "page": page_number, "text": chunk},
                        ))
        if points:
            self.qdrant.upsert(settings.qdrant_collection, points=points, wait=True)
        return len(pdfs), len(points)

    async def ask(self, question: str) -> dict:
        await self.ensure_collection()
        hits = self.qdrant.query_points(
            settings.qdrant_collection,
            query=await self.embed(question),
            limit=settings.top_k,
            score_threshold=settings.min_score,
            with_payload=True,
        ).points
        if not hits:
            return {"answer": "Information non trouvée dans les instructions disponibles.", "sources": [], "grounded": False}
        context = "\n\n".join(
            f"SOURCE {i}: [{h.payload['document']}, p. {h.payload['page']}]\n{h.payload['text']}"
            for i, h in enumerate(hits, start=1)
        )
        response = await self.http.post(
            f"{settings.ollama_url}/api/chat",
            json={"model": settings.ollama_model, "stream": False, "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"CONTEXTE:\n{context}\n\nQUESTION:\n{question}"},
            ], "options": {"temperature": 0.1}},
        )
        response.raise_for_status()
        return {
            "answer": response.json()["message"]["content"],
            "grounded": True,
            "sources": [{"document": h.payload["document"], "page": h.payload["page"],
                         "excerpt": h.payload["text"][:300], "score": round(h.score, 3)} for h in hits],
        }


knowledge_base = KnowledgeBase()

