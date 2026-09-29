"""Portable bounded-memory backups; no filesystem copies of a running Qdrant DB."""

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from qdrant_client import models

from .documents import atomic_write, safe_path
from .indexing import SCHEMA_VERSION, index_lock, require_completed
from .security import DB_SCHEMA, SecurityStore, maintenance_lock, utc

BACKUP_SCHEMA = 1


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_tree(source, target):
    source = Path(source)
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Répertoire absent ou lien symbolique interdit")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for parent, directories, files in os.walk(source):
        for name in directories + files:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError("Lien symbolique interdit dans la sauvegarde")
        for name in files:
            path = Path(parent) / name
            dest = target / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(path, dest)
            dest.chmod(0o600)


def validate_catalogue(root):
    with sqlite3.connect(root / "security.sqlite3") as db:
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA user_version").fetchone()[0] != DB_SCHEMA:
            raise ValueError("Schéma SQLite incompatible")
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("Intégrité SQLite invalide")
        for row in db.execute("SELECT * FROM versions"):
            hashed = row["hash"]
            if len(hashed) != 64 or any(c not in "0123456789abcdef" for c in hashed):
                raise ValueError("Empreinte de version invalide")
            version = root / "versions" / f"{hashed}.pdf"
            if not version.is_file() or checksum(version) != hashed:
                raise ValueError("Version PDF absente ou altérée")
        for row in db.execute("SELECT * FROM documents WHERE active=1"):
            path = safe_path(root / "documents", row["path"])
            if not path.is_file() or checksum(path) != row["current_hash"]:
                raise ValueError(
                    "PDF et catalogue incohérents; synchronisez avant de sauvegarder"
                )


def export_index(kb, root):
    collections = []
    for kind, name in (
        ("passages", kb.settings.qdrant_collection),
        ("manifest", kb.store.manifest_collection),
    ):
        if not kb.qdrant.collection_exists(name):
            continue
        info = kb.qdrant.get_collection(name)
        vectors = info.config.params.vectors
        config = (
            vectors.model_dump(mode="json")
            if isinstance(vectors, models.VectorParams)
            else {}
        )
        count = 0
        with (root / f"{kind}.jsonl").open("w") as output:
            offset = None
            while True:
                records, offset = kb.qdrant.scroll(
                    name, offset=offset, limit=128, with_vectors=True, with_payload=True
                )
                for record in records:
                    point = models.PointStruct(
                        id=record.id, vector=record.vector, payload=record.payload
                    )
                    output.write(point.model_dump_json() + "\n")
                    count += 1
                if offset is None:
                    break
        if count != kb.qdrant.count(name, exact=True).count:
            raise ValueError("Index modifié pendant la sauvegarde")
        collections.append({"kind": kind, "vectors": config, "count": count})
    return collections


def backup(config, kb, archive):
    archive = Path(archive).resolve()
    for private in (
        Path(config.state_path).resolve(),
        Path(config.documents_path).resolve(),
    ):
        if archive.is_relative_to(private):
            raise ValueError("Placez la sauvegarde hors des répertoires de données")
    if archive.exists():
        raise FileExistsError("L’archive existe déjà")
    security = SecurityStore(config)
    with maintenance_lock(config, exclusive=True), index_lock(config):
        security.audit(None, "backup.start", "archive")
        try:
            # Read validates the existing manifest contract before exporting.
            kb.store.read()
            with tempfile.TemporaryDirectory(prefix="instruct-backup-") as temp:
                root = Path(temp)
                copy_tree(config.documents_path, root / "documents")
                copy_tree(security.root / "versions", root / "versions")
                collections = export_index(kb, root)
                security.audit(None, "backup.finish", "archive")
                with (
                    security.connect() as source,
                    sqlite3.connect(root / "security.sqlite3") as dest,
                ):
                    source.backup(dest)
                    # Neither live sessions nor brute-force state belong in an archive.
                    dest.execute("DELETE FROM sessions")
                    dest.execute("DELETE FROM attempts")
                validate_catalogue(root)
                manifest = {
                    "backup_schema": BACKUP_SCHEMA,
                    "security_schema": DB_SCHEMA,
                    "index_schema": SCHEMA_VERSION,
                    "created_at": utc(),
                    "collections": collections,
                    "settings": {
                        key: getattr(config, key)
                        for key in (
                            "embedding_model",
                            "ollama_model",
                            "chunk_size",
                            "chunk_overlap",
                        )
                    },
                    "files": {
                        p.relative_to(root).as_posix(): {
                            "sha256": checksum(p),
                            "bytes": p.stat().st_size,
                        }
                        for p in sorted(root.rglob("*"))
                        if p.is_file()
                    },
                }
                (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
                archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    with (
                        os.fdopen(fd, "wb") as stream,
                        tarfile.open(fileobj=stream, mode="w:gz") as tar,
                    ):
                        for file in sorted(root.rglob("*")):
                            if file.is_file():
                                tar.add(
                                    file,
                                    arcname=file.relative_to(root).as_posix(),
                                    recursive=False,
                                )
                except BaseException:
                    archive.unlink(missing_ok=True)
                    raise
        except BaseException:
            security.audit(None, "backup.finish", "archive", "failure")
            raise
    return manifest


def unpack_verified(archive, target):
    # Never extractall: reject links, special files, duplicates and path traversal.
    seen = set()
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if (
                not member.isfile()
                or path.is_absolute()
                or any(p in {"..", "."} for p in path.parts)
                or "\\" in member.name
                or member.name in seen
                or str(path) != member.name
            ):
                raise ValueError("Entrée d’archive interdite")
            if len(seen) >= 100000 or member.size > 2 * 1024**3:
                raise ValueError("Limite d’archive dépassée")
            total += member.size
            if total > 20 * 1024**3:
                raise ValueError("Archive supérieure à 20 Gio")
            seen.add(member.name)
            dest = target / member.name
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tar.extractfile(member) as source, dest.open("wb") as output:
                shutil.copyfileobj(source, output)
            dest.chmod(0o600)
    manifest = json.loads((target / "manifest.json").read_text())
    if (
        manifest.get("backup_schema"),
        manifest.get("security_schema"),
        manifest.get("index_schema"),
    ) != (BACKUP_SCHEMA, DB_SCHEMA, SCHEMA_VERSION):
        raise ValueError("Version de sauvegarde incompatible")
    if set(manifest["files"]) != seen - {"manifest.json"}:
        raise ValueError("Manifeste incomplet")
    for name, expected in manifest["files"].items():
        path = target / name
        if (
            path.stat().st_size != expected["bytes"]
            or checksum(path) != expected["sha256"]
        ):
            raise ValueError("Contrôle d’intégrité de l’archive échoué")
    (target / "documents").mkdir(exist_ok=True)
    (target / "versions").mkdir(exist_ok=True)
    validate_catalogue(target)
    kinds = set()
    for collection in manifest["collections"]:
        kind = collection["kind"]
        if kind not in {"passages", "manifest"} or kind in kinds:
            raise ValueError("Collection invalide")
        kinds.add(kind)
        if kind == "passages":
            models.VectorParams.model_validate(collection["vectors"])
        count = 0
        with (target / f"{kind}.jsonl").open() as source:
            for line in source:
                models.PointStruct.model_validate_json(line)
                count += 1
        if count != collection["count"]:
            raise ValueError("Nombre de points invalide")
    if kinds not in (set(), {"passages", "manifest"}):
        raise ValueError("Index incomplet")
    return manifest


def restore(config, kb, archive, *, overwrite=False):
    state = Path(config.state_path)
    docs = Path(config.documents_path)
    if (
        state.resolve() == docs.resolve()
        or state.resolve().is_relative_to(docs.resolve())
        or docs.resolve().is_relative_to(state.resolve())
    ):
        raise ValueError("Les répertoires documents et état doivent être distincts")
    names = {
        "passages": config.qdrant_collection,
        "manifest": kb.store.manifest_collection,
    }
    # Validate everything possible before touching the destination.
    with tempfile.TemporaryDirectory(prefix="instruct-restore-") as temp:
        staged = Path(temp)
        manifest = unpack_verified(archive, staged)
        for key, value in manifest["settings"].items():
            if getattr(config, key) != value:
                raise ValueError(f"Configuration incompatible: {key}")
        with maintenance_lock(config, exclusive=True), index_lock(config):
            occupied = (
                (state / "security.sqlite3").exists()
                or (docs.exists() and any(docs.iterdir()))
                or any(kb.qdrant.collection_exists(n) for n in names.values())
            )
            if occupied and not overwrite:
                raise FileExistsError("Installation existante; --overwrite est requis")
            if docs.is_symlink() or state.is_symlink():
                raise ValueError("Lien symbolique de destination interdit")
            marker = state / "RESTORE_INCOMPLETE"
            marker.write_text(utc())
            try:
                for name in names.values():
                    if kb.qdrant.collection_exists(name):
                        kb.qdrant.delete_collection(name)
                for collection in manifest["collections"]:
                    kind = collection["kind"]
                    vectors = (
                        models.VectorParams.model_validate(collection["vectors"])
                        if kind == "passages"
                        else {}
                    )
                    kb.qdrant.create_collection(names[kind], vectors_config=vectors)
                    batch = []
                    with (staged / f"{kind}.jsonl").open() as source:
                        for line in source:
                            batch.append(models.PointStruct.model_validate_json(line))
                            if len(batch) == 128:
                                require_completed(
                                    kb.qdrant.upsert(
                                        names[kind], points=batch, wait=True
                                    )
                                )
                                batch.clear()
                    if batch:
                        require_completed(
                            kb.qdrant.upsert(names[kind], points=batch, wait=True)
                        )
                    if (
                        kb.qdrant.count(names[kind], exact=True).count
                        != collection["count"]
                    ):
                        raise ValueError("Restauration Qdrant incomplète")
                kb.store.read()
                if docs.exists():
                    # Preserve the directory itself: it may be a Docker bind mount.
                    for child in docs.iterdir():
                        if child.is_dir() and not child.is_symlink():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                copy_tree(staged / "documents", docs)
                versions = state / "versions"
                if versions.exists():
                    shutil.rmtree(versions)
                copy_tree(staged / "versions", versions)
                with sqlite3.connect(staged / "security.sqlite3") as db:
                    db.execute("DELETE FROM sessions")
                    db.execute("DELETE FROM attempts")
                    db.execute(
                        "INSERT INTO audit(at,actor,action,resource,result) VALUES (?,NULL,'restore.finish','archive','success')",
                        (utc(),),
                    )
                # All app connections have been closed before the exclusive lock.
                for suffix in ("-wal", "-shm", "-journal"):
                    (state / f"security.sqlite3{suffix}").unlink(missing_ok=True)
                atomic_write(
                    state / "security.sqlite3",
                    (staged / "security.sqlite3").read_bytes(),
                )
                atomic_write(state / "session.key", secrets.token_urlsafe(48).encode())
                marker.unlink()
            except BaseException:
                # Leave a durable fail-closed marker. Rerun with --overwrite after fixing.
                if (state / "security.sqlite3").is_file():
                    try:
                        with sqlite3.connect(state / "security.sqlite3") as db:
                            db.execute(
                                "INSERT INTO audit(at,actor,action,resource,result) VALUES (?,NULL,'restore.finish','archive','failure')",
                                (utc(),),
                            )
                    except sqlite3.Error:
                        pass
                raise
    return manifest
