import math
import uuid
from contextlib import nullcontext

import fitz
import httpx
from qdrant_client import QdrantClient, models

from .answering import VERSION_WARNING
from .config import settings
from .document_storage import ManagedPdfSource
from .grounding import (
    SYSTEM_PROMPT,
    GeneratedAnswer,
    model_message,
    prepare_passages,
    refusal,
    verified_answer,
)
from .indexing import (
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    DocumentError,
    IndexErrorBase,
    RevisionStore,
    active_filter,
    digest,
    document_id,
    index_lock,
    require_completed,
)
from .lexical import LexicalIndex
from .retrieval import Passage, fuse, select_context


class KnowledgeBase:
    def __init__(self, *, config=None, qdrant=None, http=None, source=None) -> None:
        self.settings = config or settings
        self._qdrant = qdrant
        self._http = http
        self.source = source if source is not None else ManagedPdfSource(self.settings)

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
    def lexical(self):
        return LexicalIndex(self.settings)

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
        try:
            self.lexical.upsert(points)
        except Exception as exc:
            raise DocumentError("LEXICAL_WRITE_FAILED") from exc

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
        only: str | None = None,
        progress=None,
        lock_held=False,
        authorize=None,
    ) -> dict:
        def report(stage, processed=0, total=None, document=None):
            if progress:
                progress(stage, processed, total, document)

        with nullcontext() if lock_held else index_lock(self.settings):
            report("Inventaire des PDF")
            full_inventory = self.source.inventory()
            inventory = {
                p: v
                for p, v in full_inventory.items()
                if (allowed_documents is None or p in allowed_documents)
                and (only is None or p == only)
            }
            if only is not None and only not in inventory:
                raise IndexErrorBase("DOCUMENT_MISSING: document absent.")
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
            self.lexical.repair(self.qdrant, self.settings.qdrant_collection, manifests)
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
            for processed, document in enumerate(sorted(inventory)):
                if authorize:
                    authorize(document)
                report("Vérification du fichier", processed, len(inventory), document)
                try:
                    try:
                        file_hash = self.source.fingerprint(document)
                    except OSError as exc:
                        raise DocumentError("READ_FAILED") from exc
                    fingerprint = digest([file_hash, signature])
                    previous = manifests.get(document)
                    if previous and previous["fingerprint"] == fingerprint:
                        # Upgrade old manifests without recomputing embeddings.
                        if hasattr(self.source, "metadata") and not previous.get(
                            "file_hash"
                        ):
                            self.store.publish(
                                document,
                                fingerprint,
                                previous["revision"],
                                previous["chunks"],
                                **self.source.metadata(document, file_hash),
                            )
                        result["unchanged"] += 1
                        report(
                            "Document inchangé", processed + 1, len(inventory), document
                        )
                        continue
                    revision = digest([document, fingerprint])
                    report(
                        "Extraction et embeddings par lots",
                        processed,
                        len(inventory),
                        document,
                    )
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
                        report(
                            "Publication des passages",
                            processed,
                            len(inventory),
                            document,
                        )
                        metadata = (
                            self.source.metadata(document, file_hash)
                            if hasattr(self.source, "metadata")
                            else {}
                        )
                        self.store.publish(
                            document, fingerprint, revision, count, **metadata
                        )
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
                report("Document traité", processed + 1, len(inventory), document)
            # Never derive deletions from a changing or partially unreadable tree.
            if self.source.inventory() != full_inventory:
                raise IndexErrorBase(
                    "DOCUMENTS_CHANGED: relancez la synchronisation; suppressions annulées."
                )
            if not result["failed"] and only is None:
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
            report("Nettoyage des anciens passages", len(inventory), len(inventory))
            if control is not None and not result["failed"]:
                _, committed = self.store.read()
                try:
                    self.store.collect_garbage(committed)
                    self.lexical.collect_garbage(committed)
                except Exception:
                    result["cleanup_pending"] = True
            return result

    async def retrieve(
        self, question: str, *, semantic_only=False, authorized_documents=None
    ) -> list[Passage]:
        with index_lock(self.settings, shared=True):
            control, manifests = self.store.read()
            excluded = (
                self.source.excluded() if hasattr(self.source, "excluded") else set()
            )
            manifests = {
                path: manifest
                for path, manifest in manifests.items()
                if path not in excluded
            }

            def permitted():
                nonlocal manifests
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

            permitted()
            if not manifests:
                return []
            self.check_identity(control, await self.embedding_identity())
            self.lexical.require_ready(manifests)
            vector = await self.embed(question)
            permitted()
            if not manifests:
                return []
            hits = self.qdrant.query_points(
                self.settings.qdrant_collection,
                query=vector,
                query_filter=active_filter(manifests),
                limit=self.settings.top_k
                if semantic_only
                else self.settings.retrieval_candidates,
                score_threshold=self.settings.min_score,
                with_payload=True,
            ).points
            semantic = [
                Passage.from_point(hit)
                for hit in sorted(
                    hits, key=lambda h: (-getattr(h, "score", 0), str(h.id))
                )
            ]
            if semantic_only:
                return semantic
            rows = self.lexical.search(
                question, manifests, self.settings.retrieval_candidates
            )
            lexical = [
                Passage(
                    row["id"],
                    row["document"],
                    row["revision"],
                    row["fingerprint"],
                    row["page"],
                    row["text"],
                )
                for row in rows
            ]
            return fuse(question, semantic, lexical)

    async def ask(self, question: str, *, authorized_documents=None) -> dict:
        candidates = select_context(
            await self.retrieve(question, authorized_documents=authorized_documents),
            max_passages=self.settings.top_k,
            max_chars=self.settings.context_max_chars,
        )

        def permitted_passage(p):
            return p.document in allowed and (
                not isinstance(allowed, dict) or p.revision == allowed[p.document]
            )

        if authorized_documents is not None:
            allowed = authorized_documents()
            candidates = [p for p in candidates if permitted_passage(p)]
        passages = prepare_passages(candidates, question, self.settings)
        if not passages:
            return refusal()
        output_schema = GeneratedAnswer.model_json_schema()
        output_schema["$defs"]["Selection"]["properties"]["source_id"]["enum"] = [
            passage.source_id for passage in passages
        ]
        response = await self.http.post(
            f"{self.settings.ollama_url}/api/chat",
            json={
                "model": self.settings.ollama_model,
                "stream": False,
                "think": False,
                "format": output_schema,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": model_message(question, passages)},
                ],
                "options": {
                    "temperature": 0,
                    "num_ctx": self.settings.ollama_num_ctx,
                    "num_predict": self.settings.ollama_num_predict,
                },
            },
        )
        response.raise_for_status()
        if authorized_documents is not None:
            allowed = authorized_documents()
            if any(not permitted_passage(p) for p in passages):
                return refusal()
        try:
            envelope = response.json()
        except ValueError:
            return refusal()
        result = verified_answer(envelope, passages, self.settings)
        if not result["grounded"]:
            return result
        if len({p.document for p in passages}) > 1:
            result["safety_notice"] += " " + VERSION_WARNING
        # A cited revision may have been removed/replaced during local generation.
        with index_lock(self.settings, shared=True):
            _, current = self.store.read()
            excluded = (
                self.source.excluded() if hasattr(self.source, "excluded") else set()
            )
            if any(
                source["document"] in excluded
                or current.get(source["document"], {}).get("revision")
                != source["revision"]
                for source in result["sources"]
            ):
                return refusal()
            for source in result["sources"]:
                source["url"] = None
                source["document_id"] = document_id(source["document"])
                source["version"] = current[source["document"]].get("file_hash")
        return result


knowledge_base = KnowledgeBase()
