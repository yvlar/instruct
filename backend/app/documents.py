"""One bounded local worker; SQLite journal survives process interruption."""

import asyncio
import json
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import fitz
import portalocker

from .document_storage import (
    DocumentProblem,
    DocumentState,
    file_hash,
    now,
    validate_path,
)
from .indexing import IndexErrorBase, document_id, index_lock


class DocumentManager:
    def __init__(self, config, factory):
        self.config = config
        self.state = DocumentState(config)
        self.factory = factory
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="documents"
        )
        self.owner = None
        self.upload_slots = threading.BoundedSemaphore(2)
        self.file_details = {}

    def start(self):
        self.owner = (self.state.root / "worker.lock").open("a+b")
        try:
            portalocker.lock(self.owner, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException:
            self.owner.close()
            self.owner = None
            raise RuntimeError(
                "La gestion documentaire nécessite un seul processus backend."
            ) from None
        for job in self.state.jobs():
            if job["state"] in ("queued", "running"):
                job.update(
                    state="interrupted",
                    stage="Traitement interrompu",
                    error="Le backend a redémarré. Relancez cette tâche.",
                )
                self.state.save_job(job)
                if job["document"]:
                    self.state.record(job["document"], error=job["error"])
        # Incomplete HTTP uploads are never referenced by a document record.
        retained = {
            r["pending"]["name"] for r in self.state.records().values() if r["pending"]
        }
        for path in (self.state.root / "staging").glob("*.pdf"):
            if path.name not in retained:
                path.unlink()

    def close(self):
        self.executor.shutdown(wait=True)
        if self.owner:
            portalocker.unlock(self.owner)
            self.owner.close()
            self.owner = None

    @contextmanager
    def admission(self):
        with (self.state.root / "admission.lock").open("a+b") as handle:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.exceptions.LockException as exc:
                raise DocumentProblem(
                    409, "Une opération documentaire est en cours."
                ) from exc
            try:
                if any(j["state"] in ("queued", "running") for j in self.state.jobs()):
                    raise DocumentProblem(
                        409, "Une tâche est déjà en cours. Attendez sa fin."
                    )
                yield
            finally:
                portalocker.unlock(handle)

    def resolve(self, identifier, kb):
        _, manifests = kb.store.read()
        names = set(self.state.records()) | set(manifests) | set(kb.source.inventory())
        for path in names:
            if document_id(path) == identifier:
                return path
        raise DocumentProblem(404, "Document inconnu.")

    def accept_upload(self, staged, metadata, name, folder, replace_id, kb):
        document = validate_path(name, folder)
        with self.admission(), index_lock(self.config):
            record = self.state.records().get(document, {})
            if record.get("removed"):
                raise DocumentProblem(
                    409,
                    "Ce chemin a été retiré. Choisissez un autre nom pour un nouvel ajout.",
                )
            if record.get("pending"):
                raise DocumentProblem(
                    409,
                    "Un remplacement attend déjà son indexation. Utilisez Réessayer.",
                )
            # Check destination before any copy, using directory descriptors.
            with self.state.parent(document, create=True):
                current = self.state.current_hash(document)
            if replace_id:
                if self.resolve(replace_id, kb) != document or current is None:
                    raise DocumentProblem(
                        409,
                        "Le document à remplacer ne correspond pas à cette destination.",
                    )
                # Retain the previous disk version even if it predates managed snapshots.
                with self.state.open_document(document) as stream:
                    self.state.preserve(document, current, stream)
                self.state.record(
                    document,
                    pending={**metadata, "name": staged.name, "expected": current},
                    error=None,
                    info=metadata,
                )
            elif current is not None:
                raise DocumentProblem(
                    409,
                    "Ce nom existe déjà. Choisissez explicitement Remplacer ou un autre nom.",
                )
            else:
                self.state.install(document, staged)
                self.state.record(document, error=None, info=metadata)
                staged.unlink()
        return {
            "document_id": document_id(document),
            "document": document,
            "replacement_pending": bool(replace_id),
        }

    def submit(self, kind, kb, identifier=None, *, launch=True):
        if kind not in ("sync", "index", "remove"):
            raise DocumentProblem(400, "Action inconnue.")
        with self.admission(), index_lock(self.config):
            document = self.resolve(identifier, kb) if identifier else None
            if kind != "sync" and document is None:
                raise DocumentProblem(400, "Document requis.")
            if kind == "index" and self.state.records().get(document, {}).get(
                "removed"
            ):
                raise DocumentProblem(
                    409, "Document retiré : réessayez son retrait si nécessaire."
                )
            job = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "document": document,
                "state": "queued",
                "stage": "En attente",
                "processed": 0,
                "total": None,
                "current_document": document,
                "created_at": now(),
                "error": None,
                "result": None,
            }
            self.state.save_job(job)
        if launch:
            self.executor.submit(self.execute, job)
        return job.copy()

    def retry(self, identifier, kb):
        job = next((j for j in self.state.jobs() if j["id"] == identifier), None)
        if job is None:
            raise DocumentProblem(404, "Tâche inconnue.")
        if job["state"] not in ("failed", "interrupted"):
            raise DocumentProblem(
                409, "Cette tâche ne nécessite pas de nouvelle tentative."
            )
        return self.submit(
            job["kind"], kb, document_id(job["document"]) if job["document"] else None
        )

    def finish_replacements(self, kb, *, only=None, exclude=()):
        _, manifests = kb.store.read()
        for path, record in self.state.records().items():
            if record["removed"] or path in exclude or (only and path != only):
                continue
            pending = record["pending"]
            if (
                pending
                and manifests.get(path, {}).get("file_hash") == pending["file_hash"]
            ):
                candidate = self.state.root / "staging" / pending["name"]
                current = self.state.current_hash(path)
                # Idempotent after death between atomic rename and journal update.
                if current != pending["file_hash"]:
                    self.state.install(path, candidate, expected=pending["expected"])
                self.state.record(path, pending=None, error=None)
                candidate.unlink(missing_ok=True)

    def remove(self, document, kb):
        record = self.state.records().get(document, {})
        self.state.record(
            document, removed=1
        )  # durable search tombstone, before any destructive action
        if not record.get("archived"):
            try:
                current = self.state.current_hash(document)
            except DocumentProblem:
                # Missing parent after an externally removed file is safe only if inventory confirms absence.
                if document in kb.source.inventory():
                    raise
                current = None
            if current:
                with self.state.open_document(document) as stream:
                    snapshot = self.state.preserve(document, current, stream)
                archive = self.state.root / "archive" / document_id(document)
                archive.mkdir(exist_ok=True)
                target = archive / f"{current}.pdf"
                shutil.copyfile(snapshot, target)
                with target.open("rb") as stream:
                    os.fsync(stream.fileno())
                if file_hash(target) != current:
                    raise DocumentProblem(
                        503, "Archive non vérifiée; le fichier est conservé."
                    )
                with self.state.parent(document) as (fd, name):
                    if self.state.current_hash(document) != current:
                        raise DocumentProblem(
                            409, "Le fichier a changé pendant le retrait. Réessayez."
                        )
                    os.unlink(name, dir_fd=fd)
                    os.fsync(fd)
                self.state.record(
                    document, archived=str(target.relative_to(self.state.root))
                )
        control, manifests = kb.store.read()
        if document in manifests:
            kb.store.remove(document)
        if control:
            _, manifests = kb.store.read()
            kb.store.collect_garbage(manifests)
        if record.get("pending"):
            (self.state.root / "staging" / record["pending"]["name"]).unlink(
                missing_ok=True
            )
        self.state.record(document, pending=None, error=None, removed=2)

    async def synchronize(self, kb, *, only=None, allow_empty=False, progress=None):
        # Called with the shared mutation lock. Tombstones always win over disk inventory.
        for path, record in self.state.records().items():
            if record["removed"] == 1 and (only is None or path == only):
                self.remove(path, kb)
        result = await kb.ingest(
            allow_empty=allow_empty, only=only, progress=progress, lock_held=True
        )
        failed_paths = {e["document"] for e in result["errors"]}
        # Do not finalize an ambiguous manifest write until a successful retry.
        self.finish_replacements(kb, only=only, exclude=failed_paths)
        for path in kb.source.inventory():
            if (only is None or path == only) and path not in failed_paths:
                self.state.record(path, error=None)
        for error in result["errors"]:
            self.state.record(error["document"], error=error["code"])
        return result

    def execute(self, job):
        async def work():
            kb = self.factory()
            try:
                with index_lock(self.config):
                    job.update(state="running", stage="Préparation")
                    self.state.save_job(job)
                    if job["kind"] == "remove":
                        job.update(stage="Archivage et retrait des index", total=1)
                        self.state.save_job(job)
                        self.remove(job["document"], kb)
                        job.update(processed=1, result={"removed": 1})
                    else:

                        def progress(stage, processed, total, document):
                            job.update(
                                stage=stage,
                                processed=processed,
                                total=total,
                                current_document=document,
                            )
                            self.state.save_job(job)

                        result = await self.synchronize(
                            kb, only=job["document"], progress=progress
                        )
                        job["result"] = result
                        if result["failed"] or result["cleanup_pending"]:
                            raise DocumentProblem(
                                503,
                                "Traitement partiel. Consultez les documents en erreur puis réessayez; un nettoyage peut rester nécessaire.",
                            )
                    job.update(state="completed", stage="Terminé", finished_at=now())
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, (IndexErrorBase, DocumentProblem))
                    else "Traitement impossible. Vérifiez les services locaux et l’espace disque, puis réessayez."
                )
                job.update(
                    state="failed",
                    stage="Échec du traitement",
                    error=message,
                    finished_at=now(),
                )
                if job["document"]:
                    self.state.record(job["document"], error=message)
            finally:
                self.state.save_job(job)
                await kb.close()

        asyncio.run(work())

    def listing(self, kb, query="", status=None, page=1, page_size=20):
        _, manifests = kb.store.read()
        inventory = kb.source.inventory()
        records = self.state.records()
        jobs = self.state.jobs()
        active = next((j for j in jobs if j["state"] in ("queued", "running")), None)
        rows = []
        for path in sorted(set(inventory) | set(manifests) | set(records)):
            record = records.get(path, {})
            if (
                record.get("removed")
                and not record.get("error")
                and not (active and active["document"] == path)
            ):
                continue
            manifest = manifests.get(path, {})
            pending = record.get("pending")
            details = {"pages": None, "size": None, "file_hash": None}
            if path in inventory:
                cache = self.file_details.get(path)
                if cache and cache[0] == (inventory[path], manifest.get("indexed_at")):
                    details = cache[1]
                else:
                    try:
                        version = kb.source.fingerprint(path)
                        info = json.loads(record["info"]) if record.get("info") else {}
                        pages = (
                            manifest.get("pages")
                            if version == manifest.get("file_hash")
                            else info.get("pages")
                            if version == info.get("file_hash")
                            else None
                        )
                        details = {
                            "pages": pages,
                            "size": inventory[path][2],
                            "file_hash": version,
                        }
                        self.file_details[path] = (
                            (inventory[path], manifest.get("indexed_at")),
                            details,
                        )
                    except Exception:
                        details["size"] = inventory[path][2]
            state = "pending" if not manifest else "available"
            if manifest and (
                pending or manifest.get("file_hash") != details["file_hash"]
            ):
                state = "modified"
            if (
                record.get("error")
                or path not in inventory
                or details["file_hash"] is None
            ):
                state = "error"
            if active and (active["document"] is None or active["document"] == path):
                state = "running"
            if query.casefold() not in path.casefold() or (status and status != state):
                continue
            rows.append(
                {
                    "id": document_id(path),
                    "document": path,
                    "name": path.rsplit("/", 1)[-1],
                    "folder": path.rsplit("/", 1)[0] if "/" in path else "",
                    **details,
                    "version": manifest.get("file_hash"),
                    "indexed_at": manifest.get("indexed_at"),
                    "state": state,
                    "error": record.get("error"),
                    "retired": bool(record.get("removed")),
                    "replacement_pending": bool(pending),
                }
            )
        start = (page - 1) * page_size
        return {
            "items": rows[start : start + page_size],
            "total": len(rows),
            "page": page,
            "page_size": page_size,
            "jobs": jobs[:10],
            "max_pdf_bytes": self.config.max_pdf_bytes,
        }

    def source(self, identifier, version, page):
        path = self.state.version_path(identifier, version)
        if not path.is_file() or path.is_symlink() or file_hash(path) != version:
            raise DocumentProblem(
                410,
                "Cette source n’est plus disponible dans sa version citée. Relancez la question après synchronisation.",
            )
        with fitz.open(path) as pdf:
            if page < 1 or page > len(pdf):
                raise DocumentProblem(400, "Page absente de cette version du PDF.")
        return path
