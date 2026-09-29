import math
import uuid
from contextlib import nullcontext

import fitz
import httpx
from qdrant_client import QdrantClient, models

from .config import settings
from .indexing import (
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    DocumentError,
    IndexErrorBase,
    PdfSource,
    RevisionStore,
    active_filter,
    digest,
    index_lock,
    require_completed,
)

SYSTEM_PROMPT = """Tu es un assistant d'instructions de travail industrielles.
Réponds uniquement avec le CONTEXTE fourni. N'invente jamais une étape, une valeur,
une consigne de sécurité ou un équipement. Si le contexte est insuffisant, réponds
exactement : « Information non trouvée dans les instructions disponibles. »
Cite les sources dans le texte sous la forme [document, p. X]. Réponds en français,
clairement et sans ajouter de connaissance générale. Rappelle de vérifier la version
officielle du document avant d'exécuter une procédure.
"""


class KnowledgeBase:
    def __init__(self, *, config=None, qdrant=None, http=None, source=None) -> None:
        self.settings = config or settings
        self._qdrant = qdrant
        self._http = http
        self.source = (
            source
            if source is not None
            else PdfSource(
                self.settings.documents_path,
                self.settings.chunk_size,
                self.settings.chunk_overlap,
            )
        )

    @property
    def qdrant(self):
        if self._qdrant is None:
            self._qdrant = QdrantClient(url=self.settings.qdrant_url)
        return self._qdrant

    @property
    def http(self):
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=120)
        return self._http

    @property
    def store(self):
        return RevisionStore(self.qdrant, self.settings.qdrant_collection)

    async def close(self):
        if self._http is not None:
            await self._http.aclose()
        if self._qdrant is not None:
            self._qdrant.close()

    async def embedding_identity(self) -> dict:
        response = await self.http.get(f"{self.settings.ollama_url}/api/tags")
        response.raise_for_status()
        name = self.settings.embedding_model
        canonical = name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"
        for model in response.json()["models"]:
            if model.get("name") in (name, canonical) and model.get("digest"):
                return {"model": name, "digest": model["digest"]}
        raise IndexErrorBase(
            "EMBEDDING_MODEL_MISSING: installez le modèle configuré dans Ollama."
        )

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        response = await self.http.post(
            f"{self.settings.ollama_url}/api/embed",
            json={
                "model": self.settings.embedding_model,
                "input": texts,
                "truncate": False,
            },
        )
        response.raise_for_status()
        vectors = response.json()["embeddings"]
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("Invalid embedding count")
        size = len(vectors[0]) if vectors else 0
        if not size or any(
            len(vector) != size
            or not all(math.isfinite(v) for v in vector)
            or not any(vector)
            for vector in vectors
        ):
            raise ValueError("Invalid embedding vectors")
        return vectors

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]

    @staticmethod
    def check_identity(control: dict, identity: dict):
        if control.get("embedding") != identity:
            raise IndexErrorBase(
                "EMBEDDING_MODEL_CHANGED: reconstruisez dans une nouvelle "
                "QDRANT_COLLECTION pour ne pas mélanger les espaces vectoriels."
            )

    async def write_batch(
        self, document, revision, fingerprint, batch, start, size, authorize=None
    ):
        if authorize:
            authorize(document)
        try:
            vectors = await self.embed_many([text for _, text in batch])
            if any(len(vector) != size for vector in vectors):
                raise ValueError("Embedding dimension changed")
        except Exception as exc:
            raise DocumentError("EMBEDDING_FAILED") from exc
        if authorize:
            authorize(document)
        points = [
            models.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{revision}:{start + index}")),
                vector=vector,
                payload={
                    "schema": SCHEMA_VERSION,
                    "document": document,
                    "revision": revision,
                    "fingerprint": fingerprint,
                    "page": page,
                    "text": text,
                },
            )
            for index, ((page, text), vector) in enumerate(
                zip(batch, vectors, strict=True)
            )
        ]
        try:
            require_completed(
                self.qdrant.upsert(
                    self.settings.qdrant_collection,
                    points=points,
                    wait=True,
                )
            )
        except Exception as exc:
            raise DocumentError("QDRANT_WRITE_FAILED") from exc

    async def stage_document(
        self, document, file_hash, fingerprint, revision, size, authorize=None
    ):
        batch = []
        count = 0
        try:
            for passage in self.source.passages(document, file_hash):
                batch.append(passage)
                if len(batch) == self.settings.embedding_batch_size:
                    await self.write_batch(
                        document, revision, fingerprint, batch, count, size, authorize
                    )
                    count += len(batch)
                    batch.clear()
            if batch:
                await self.write_batch(
                    document, revision, fingerprint, batch, count, size, authorize
                )
                count += len(batch)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError("EXTRACTION_FAILED") from exc
        if not count:
            raise DocumentError("NO_TEXT")
        return count

    async def ingest(
        self,
        *,
        allow_empty: bool = False,
        allowed_documents=None,
        lock_held=False,
        authorize=None,
    ) -> dict:
        with nullcontext() if lock_held else index_lock(self.settings):
            full_inventory = self.source.inventory()
            inventory = {
                p: v
                for p, v in full_inventory.items()
                if allowed_documents is None or p in allowed_documents
            }
            control, manifests = self.store.read()
            manifests = {
                p: v
                for p, v in manifests.items()
                if allowed_documents is None or p in allowed_documents
            }
            if not inventory and manifests and not allow_empty:
                raise IndexErrorBase(
                    "EMPTY_DOCUMENTS: index conservé. Pour une suppression totale volontaire, "
                    "utilisez POST /api/ingest?allow_empty=true."
                )
            result = dict(
                documents=len(inventory),
                chunks=0,
                added=0,
                modified=0,
                unchanged=0,
                deleted=0,
                failed=0,
                errors=[],
                cleanup_pending=False,
            )
            # Empty synchronizations need neither Ollama nor a new collection.
            identity = await self.embedding_identity() if inventory else None
            if inventory:
                if control is None:
                    vector = await self.embed("initialisation")
                    control = self.store.initialize(identity, len(vector))
                self.check_identity(control, identity)
            signature = digest(
                {
                    "pipeline": PIPELINE_VERSION,
                    "extractor": fitz.VersionBind,
                    "chunk_size": self.settings.chunk_size,
                    "chunk_overlap": self.settings.chunk_overlap,
                    "embedding": identity,
                    "truncate": False,
                }
            )
            for document in sorted(inventory):
                if authorize:
                    authorize(document)
                try:
                    try:
                        file_hash = self.source.fingerprint(document)
                    except OSError as exc:
                        raise DocumentError("READ_FAILED") from exc
                    fingerprint = digest([file_hash, signature])
                    previous = manifests.get(document)
                    if previous and previous["fingerprint"] == fingerprint:
                        result["unchanged"] += 1
                        continue
                    revision = digest([document, fingerprint])
                    count = await self.stage_document(
                        document,
                        file_hash,
                        fingerprint,
                        revision,
                        control["vector_size"],
                        authorize,
                    )
                    # Verify content again before publishing a stable on-disk snapshot.
                    if self.source.fingerprint(document) != file_hash:
                        raise DocumentError("SOURCE_CHANGED")
                    if await self.embedding_identity() != identity:
                        raise DocumentError("EMBEDDING_MODEL_CHANGED")
                    if authorize:
                        authorize(document)
                    try:
                        self.store.publish(document, fingerprint, revision, count)
                    except Exception as exc:
                        raise DocumentError("MANIFEST_WRITE_FAILED") from exc
                    result["modified" if previous else "added"] += 1
                    result["chunks"] += count
                except Exception as exc:
                    result["failed"] += 1
                    result["errors"].append(
                        {
                            "document": document,
                            "code": str(exc)
                            if isinstance(exc, DocumentError)
                            else "DOCUMENT_FAILED",
                        }
                    )
            # Never derive deletions from a changing or partially unreadable tree.
            if self.source.inventory() != full_inventory:
                raise IndexErrorBase(
                    "DOCUMENTS_CHANGED: relancez la synchronisation; suppressions annulées."
                )
            if not result["failed"]:
                for document in sorted(manifests.keys() - inventory.keys()):
                    try:
                        self.store.remove(document)
                        result["deleted"] += 1
                    except Exception:
                        result["failed"] += 1
                        result["errors"].append(
                            {"document": document, "code": "DELETE_FAILED"}
                        )
            # On an ambiguous write failure keep both versions on disk. A later run
            # re-reads durable manifests and safely cleans interrupted work.
            if control is not None and not result["failed"]:
                _, committed = self.store.read()
                try:
                    self.store.collect_garbage(committed)
                except Exception:
                    result["cleanup_pending"] = True
            return result

    async def ask(self, question: str, *, authorized_documents=None) -> dict:
        with index_lock(self.settings, shared=True):
            control, manifests = self.store.read()
            if authorized_documents is not None:
                allowed = authorized_documents()
                manifests = {
                    p: m
                    for p, m in manifests.items()
                    if p in allowed
                    and (not isinstance(allowed, dict) or m["revision"] == allowed[p])
                }
            hits = []
            if manifests:
                self.check_identity(control, await self.embedding_identity())
                vector = await self.embed(question)
                if authorized_documents is not None:
                    allowed = authorized_documents()
                    manifests = {
                        p: m
                        for p, m in manifests.items()
                        if p in allowed
                        and (
                            not isinstance(allowed, dict) or m["revision"] == allowed[p]
                        )
                    }
                if manifests:
                    hits = self.qdrant.query_points(
                        self.settings.qdrant_collection,
                        query=vector,
                        query_filter=active_filter(manifests),
                        limit=self.settings.top_k,
                        score_threshold=self.settings.min_score,
                        with_payload=True,
                    ).points
        # Recheck after awaited embeddings and before any context reaches Ollama.
        if authorized_documents is not None:
            allowed = authorized_documents()
            hits = [
                h
                for h in hits
                if h.payload.get("document") in allowed
                and (
                    not isinstance(allowed, dict)
                    or h.payload.get("revision") == allowed[h.payload["document"]]
                )
            ]
        if not hits:
            return {
                "answer": "Information non trouvée dans les instructions disponibles.",
                "sources": [],
                "grounded": False,
            }
        context = "\n\n".join(
            f"SOURCE {i}: [{h.payload['document']}, p. {h.payload['page']}]\n{h.payload['text']}"
            for i, h in enumerate(hits, start=1)
        )
        response = await self.http.post(
            f"{self.settings.ollama_url}/api/chat",
            json={
                "model": self.settings.ollama_model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"CONTEXTE:\n{context}\n\nQUESTION:\n{question}",
                    },
                ],
                "options": {"temperature": 0.1},
            },
        )
        response.raise_for_status()
        # Revoke the entire answer if any of its context was revoked during generation.
        if authorized_documents is not None:
            allowed = authorized_documents()
            if any(
                h.payload.get("document") not in allowed
                or (
                    isinstance(allowed, dict)
                    and h.payload.get("revision") != allowed[h.payload["document"]]
                )
                for h in hits
            ):
                return {
                    "answer": "Les droits ont changé; relancez la recherche.",
                    "grounded": False,
                    "sources": [],
                }
        return {
            "answer": response.json()["message"]["content"],
            "grounded": True,
            "sources": [
                {
                    "document": h.payload["document"],
                    "revision": h.payload["revision"],
                    "page": h.payload["page"],
                    "excerpt": h.payload["text"][:300],
                    "score": round(h.score, 3),
                }
                for h in hits
            ],
        }


knowledge_base = KnowledgeBase()
