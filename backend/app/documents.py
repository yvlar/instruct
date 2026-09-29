"""PDF catalogue, private immutable versions and scoped synchronizations."""

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath

import fitz
from fastapi import HTTPException

from .indexing import document_id, index_lock
from .security import utc


def safe_path(root, relative):
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or relative != PurePosixPath(relative).as_posix()
        or "\\" in relative
        or "\x00" in relative
    ):
        raise HTTPException(422, "Chemin PDF invalide.")
    if (
        PurePosixPath(relative).is_absolute()
        or any(p.startswith(".") for p in parts)
        or not relative.lower().endswith(".pdf")
    ):
        raise HTTPException(422, "Chemin PDF invalide.")
    root = Path(root)
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise HTTPException(422, "Les liens symboliques sont interdits.")
    if root.is_symlink() or not current.resolve().is_relative_to(root.resolve()):
        raise HTTPException(422, "Chemin PDF invalide.")
    return current


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".instruct-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Documents:
    def __init__(self, config, security, kb):
        self.config = config
        self.security = security
        self.kb = kb
        self.versions = Path(config.state_path) / "versions"
        self.versions.mkdir(exist_ok=True, mode=0o700)

    def inspect(self, data):
        if len(data) > self.config.max_pdf_bytes:
            raise HTTPException(413, "PDF trop volumineux.")
        try:
            with fitz.open(stream=data, filetype="pdf") as pdf:
                if pdf.is_encrypted or not 1 <= len(pdf) <= 10000:
                    raise ValueError("Invalid PDF")
                return hashlib.sha256(data).hexdigest(), len(pdf)
        except Exception as exc:
            raise HTTPException(422, "PDF invalide, chiffré ou trop long.") from exc

    def record(self, path, data, *, actor, groups=None, action="document.discover"):
        hashed, pages = self.inspect(data)
        ident = document_id(path)
        archive = self.versions / f"{hashed}.pdf"
        if not archive.exists():
            atomic_write(archive, data)
        with self.security.transaction() as db:
            previous = db.execute(
                "SELECT current_hash FROM documents WHERE id=?", (ident,)
            ).fetchone()
            db.execute(
                "INSERT INTO documents(id,path,current_hash,size,pages) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET active=1,current_hash=excluded.current_hash,"
                "size=excluded.size,pages=excluded.pages",
                (ident, path, hashed, len(data), pages),
            )
            db.execute(
                "INSERT OR IGNORE INTO versions VALUES (?,?,?,?)",
                (ident, hashed, utc(), pages),
            )
            if groups is not None:
                self.security.set_groups(
                    db, "document_groups", "document_id", ident, groups
                )
                self.security.event(db, actor, "document.permissions", ident)
            if (
                previous is None
                or previous[0] != hashed
                or action != "document.discover"
            ):
                self.security.event(db, actor, action, ident)
        return ident

    def upload(self, path, data, groups, user):
        if user["role"] not in {"admin", "manager"}:
            raise HTTPException(403, "Gestion documentaire non autorisée.")
        target = safe_path(self.config.documents_path, path)
        ident = document_id(path)
        with index_lock(self.config):
            with self.security.connect() as db:
                previous = db.execute(
                    "SELECT * FROM documents WHERE id=?", (ident,)
                ).fetchone()
                known = {r[0] for r in db.execute("SELECT id FROM groups")}
            if previous:
                if not previous["active"] and user["role"] != "admin":
                    raise HTTPException(404, "Document introuvable.")
                if previous["active"]:
                    self.security.require_document(ident, user, manage=True)
                groups = None  # replacement never broadens existing permissions
            elif (
                not groups
                or not set(groups) <= known
                or (user["role"] != "admin" and not set(groups) <= set(user["groups"]))
            ):
                raise HTTPException(403, "Choisissez des groupes de votre périmètre.")
            # A locally imported, unassigned file cannot be claimed by a manager.
            if not previous and target.exists() and user["role"] != "admin":
                raise HTTPException(404, "Document introuvable.")
            self.inspect(data)
            atomic_write(target, data)
            return self.record(
                path,
                data,
                actor=user["id"],
                groups=groups,
                action="document.replace" if previous else "document.add",
            )

    def remove(self, ident, user):
        with index_lock(self.config):
            doc = self.security.require_document(ident, user, manage=True)
            # Revoke before removing bytes; a failure leaves content inaccessible.
            with self.security.transaction() as db:
                db.execute("UPDATE documents SET active=0 WHERE id=?", (ident,))
                self.security.event(db, user["id"], "document.remove", ident)
            safe_path(self.config.documents_path, doc["path"]).unlink(missing_ok=True)

    def list(self, user):
        allowed = self.security.allowed_documents(user)
        with self.security.connect() as db:
            results = []
            for row in db.execute(
                "SELECT * FROM documents WHERE active=1 ORDER BY path"
            ):
                if row["path"] not in allowed:
                    continue
                doc = dict(row)
                doc["groups"] = self.security.group_ids(
                    db, "document_groups", "document_id", doc["id"]
                )
                doc["status"] = (
                    "disponible"
                    if doc["indexed_hash"]
                    and doc["indexed_hash"] == doc["current_hash"]
                    else "à indexer"
                )
                results.append(doc)
            return results

    def searchable(self, user):
        return {
            d["path"]: d["revision"]
            for d in self.list(user)
            if d["status"] == "disponible"
        }

    async def synchronize(
        self, user, ident=None, *, allow_empty=False, resolve_user=None
    ):
        if ident is None and user["role"] != "admin":
            raise HTTPException(
                403, "Synchronisation globale réservée à l’administration."
            )
        if ident is not None:
            doc = self.security.require_document(ident, user, manage=True)
            scope = {doc["path"]}
        else:
            scope = None

        def authorize(path):
            fresh = resolve_user() if resolve_user else user
            if ident is None:
                if fresh["role"] != "admin":
                    raise HTTPException(403, "Synchronisation globale non autorisée.")
            else:
                self.security.require_document(document_id(path), fresh, manage=True)

        self.security.audit(user["id"], "sync.start", ident or "catalogue")
        try:
            # ingest holds this same lock, so pass the owned guard explicitly.
            with index_lock(self.config):
                inventory = self.kb.source.inventory()
                _, previous_manifests = self.kb.store.read()
                if (
                    not inventory
                    and previous_manifests
                    and ident is None
                    and not allow_empty
                ):
                    from .indexing import IndexErrorBase

                    raise IndexErrorBase(
                        "EMPTY_DOCUMENTS: confirmez avec allow_empty=true."
                    )
                selected = set(inventory) if scope is None else set(inventory) & scope
                for path in sorted(selected):
                    authorize(path)
                    target = safe_path(self.config.documents_path, path)
                    if target.stat().st_size > self.config.max_pdf_bytes:
                        raise HTTPException(413, "PDF trop volumineux.")
                    self.record(path, target.read_bytes(), actor=user["id"])
                with self.security.transaction() as db:
                    for row in db.execute(
                        "SELECT id,path FROM documents WHERE active=1"
                    ).fetchall():
                        if row["path"] not in inventory and (
                            scope is None or row["path"] in scope
                        ):
                            db.execute(
                                "UPDATE documents SET active=0 WHERE id=?", (row["id"],)
                            )
                            self.security.event(
                                db, user["id"], "document.remove", row["id"]
                            )
                result = await self.kb.ingest(
                    allowed_documents=scope,
                    lock_held=True,
                    allow_empty=allow_empty or ident is not None,
                    authorize=authorize,
                )
                _, manifests = self.kb.store.read()
                failed = {e["document"] for e in result["errors"]}
                with self.security.transaction() as db:
                    for path in selected - failed:
                        manifest = manifests.get(path)
                        if manifest:
                            db.execute(
                                "UPDATE documents SET indexed_hash=current_hash,revision=?,indexed_at=? WHERE path=?",
                                (manifest["revision"], utc(), path),
                            )
                self.security.audit(
                    user["id"],
                    "sync.finish",
                    ident or "catalogue",
                    "partial" if result["failed"] else "success",
                )
                return result
        except BaseException:
            self.security.audit(
                user["id"], "sync.finish", ident or "catalogue", "failure"
            )
            raise

    def read(self, ident, user, version=None):
        doc = self.security.require_document(ident, user)
        hashed = version or doc["current_hash"]
        with self.security.connect() as db:
            if not db.execute(
                "SELECT 1 FROM versions WHERE document_id=? AND hash=?", (ident, hashed)
            ).fetchone():
                raise HTTPException(404, "Version introuvable.")
        path = self.versions / f"{hashed}.pdf"
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "Version introuvable.")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != hashed:
            raise HTTPException(503, "Intégrité du PDF non vérifiée.")
        return data
