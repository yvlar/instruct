"""Durable document revisions. Only a committed manifest makes vectors searchable."""

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import fitz
import portalocker
from qdrant_client import models

from .chunking import chunk_text

SCHEMA_VERSION = 2
PIPELINE_VERSION = 1  # Bump whenever normalization/extraction semantics change.
CONTROL_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "instruct:index-control:v2"))


class IndexErrorBase(RuntimeError):
    """An actionable, content-free error safe to expose through the API."""


class DocumentError(IndexErrorBase):
    pass


def require_completed(result):
    if result.status != models.UpdateStatus.COMPLETED:
        raise RuntimeError("Qdrant write completion is not confirmed")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def document_id(document: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"instruct:document:{document}"))


def condition(key: str, value: str | int) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def active_filter(manifests: dict) -> models.Filter:
    return models.Filter(
        must=[
            condition("schema", SCHEMA_VERSION),
            models.FieldCondition(
                key="revision",
                match=models.MatchAny(any=[m["revision"] for m in manifests.values()]),
            ),
        ]
    )


@contextmanager
def index_lock(settings, *, shared=False):
    """Nonblocking OS lock: shared across workers, released even on process death."""
    directory = Path(settings.index_lock_path)
    directory.mkdir(parents=True, exist_ok=True)
    name = digest([settings.qdrant_url, settings.qdrant_collection])
    flags = portalocker.LOCK_SH if shared else portalocker.LOCK_EX
    with (directory / f"{name}.lock").open("a+b") as handle:
        try:
            portalocker.lock(handle, flags | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException as exc:
            raise IndexErrorBase(
                "INDEX_BUSY: réessayez après l'indexation en cours."
            ) from exc
        try:
            yield
        finally:
            portalocker.unlock(handle)


class PdfSource:
    def __init__(self, root: str, size: int, overlap: int):
        self.root = Path(root)
        self.size = size
        self.overlap = overlap

    def inventory(self) -> dict[str, tuple]:
        """Fail closed on incomplete traversal; do not follow symlinks."""

        def fail(error):
            raise error

        found = {}
        try:
            if not self.root.is_dir() or self.root.is_symlink():
                raise OSError("Invalid documents directory")
            for parent, directories, files in os.walk(self.root, onerror=fail):
                for name in directories:
                    if (Path(parent) / name).is_symlink():
                        raise OSError("Symlink directory")
                for name in files:
                    path = Path(parent) / name
                    if path.suffix.lower() != ".pdf":
                        continue
                    if path.is_symlink() or not path.is_file():
                        raise OSError("Invalid PDF path")
                    stat = path.stat()
                    found[path.relative_to(self.root).as_posix()] = (
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                    )
        except OSError as exc:
            raise IndexErrorBase(
                "DOCUMENTS_UNAVAILABLE: dossier absent ou illisible."
            ) from exc
        return found

    def fingerprint(self, document: str) -> str:
        with (self.root / document).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def passages(self, document: str, expected_hash: str):
        # A disk snapshot keeps extraction consistent without retaining the PDF in RAM.
        with tempfile.TemporaryDirectory(prefix="instruct-pdf-") as directory:
            snapshot = Path(directory) / "source.pdf"
            with (
                (self.root / document).open("rb") as source,
                snapshot.open("wb") as target,
            ):
                shutil.copyfileobj(source, target, length=1024 * 1024)
            with snapshot.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected_hash:
                    raise DocumentError("SOURCE_CHANGED")
            with fitz.open(snapshot) as pdf:
                for page_number, page in enumerate(pdf, start=1):
                    for text in chunk_text(page.get_text(), self.size, self.overlap):
                        yield page_number, text


class RevisionStore:
    def __init__(self, qdrant, collection: str):
        self.qdrant = qdrant
        self.collection = collection
        self.manifest_collection = f"{collection}__manifest"

    def read(self) -> tuple[dict | None, dict]:
        control = None
        manifests = {}
        if self.qdrant.collection_exists(self.manifest_collection):
            offset = None
            while True:
                records, offset = self.qdrant.scroll(
                    self.manifest_collection,
                    limit=128,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for record in records:
                    payload = record.payload or {}
                    if payload.get("schema") != SCHEMA_VERSION:
                        raise IndexErrorBase(
                            "INDEX_INCOMPATIBLE: reconstruisez dans une nouvelle collection."
                        )
                    if (
                        str(record.id) == CONTROL_ID
                        and payload.get("kind") == "control"
                    ):
                        control = payload
                    elif (
                        payload.get("kind") == "document"
                        and isinstance(payload.get("document"), str)
                        and str(record.id) == document_id(payload["document"])
                        and isinstance(payload.get("revision"), str)
                        and isinstance(payload.get("fingerprint"), str)
                        and isinstance(payload.get("chunks"), int)
                        and payload["chunks"] > 0
                    ):
                        manifests[payload["document"]] = payload
                    else:
                        raise IndexErrorBase("INDEX_INCOMPATIBLE: manifeste invalide.")
                if offset is None:
                    break
        exists = self.qdrant.collection_exists(self.collection)
        if control is None:
            if manifests or (
                exists and self.qdrant.count(self.collection, exact=True).count
            ):
                raise IndexErrorBase(
                    "LEGACY_INDEX: index sans manifeste. Choisissez une nouvelle "
                    "QDRANT_COLLECTION et reconstruisez; l'ancien index est conservé."
                )
        elif not exists:
            raise IndexErrorBase("INDEX_INCOMPLETE: collection de passages manquante.")
        return control, manifests

    def initialize(self, identity: dict, vector_size: int) -> dict:
        if not self.qdrant.collection_exists(self.collection):
            self.qdrant.create_collection(
                self.collection,
                vectors_config=models.VectorParams(
                    size=vector_size, distance=models.Distance.COSINE
                ),
            )
        config = self.qdrant.get_collection(self.collection).config.params.vectors
        if not isinstance(config, models.VectorParams) or config.size != vector_size:
            raise IndexErrorBase(
                "INDEX_INCOMPATIBLE: dimension différente; utilisez une nouvelle collection."
            )
        for key in ("schema", "revision"):
            self.qdrant.create_payload_index(
                self.collection,
                field_name=key,
                field_schema=models.PayloadSchemaType.INTEGER
                if key == "schema"
                else models.PayloadSchemaType.KEYWORD,
                wait=True,
            )
        if not self.qdrant.collection_exists(self.manifest_collection):
            self.qdrant.create_collection(self.manifest_collection, vectors_config={})
        control = {
            "kind": "control",
            "schema": SCHEMA_VERSION,
            "embedding": identity,
            "vector_size": vector_size,
        }
        require_completed(
            self.qdrant.upsert(
                self.manifest_collection,
                points=[
                    models.PointStruct(id=CONTROL_ID, vector={}, payload=control),
                ],
                wait=True,
            )
        )
        return control

    def publish(
        self, document: str, fingerprint: str, revision: str, chunks: int, **metadata
    ):
        require_completed(
            self.qdrant.upsert(
                self.manifest_collection,
                points=[
                    models.PointStruct(
                        id=document_id(document),
                        vector={},
                        payload={
                            "kind": "document",
                            "schema": SCHEMA_VERSION,
                            "document": document,
                            "fingerprint": fingerprint,
                            "revision": revision,
                            "chunks": chunks,
                            **metadata,
                        },
                    )
                ],
                wait=True,
            )
        )

    def remove(self, document: str):
        # Remove visibility first. Physical deletion is retried by collect_garbage.
        require_completed(
            self.qdrant.delete(
                self.manifest_collection,
                points_selector=models.PointIdsList(points=[document_id(document)]),
                wait=True,
            )
        )

    def collect_garbage(self, manifests: dict):
        """Delete obsolete/staged versions only; never touch legacy/foreign points."""
        selector = models.Filter(must=[condition("schema", SCHEMA_VERSION)])
        if manifests:
            selector.must_not = [active_filter(manifests).must[1]]
        require_completed(
            self.qdrant.delete(self.collection, points_selector=selector, wait=True)
        )
