"""Small local journal and immutable PDF snapshots; never browser-supplied paths."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import fitz

from .indexing import DocumentError, PdfSource, document_id


def now():
    return datetime.now(timezone.utc).isoformat()


class DocumentProblem(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def validate_path(name: str, folder: str = "") -> str:
    parts = [*folder.split("/"), name] if folder else [name]
    if (
        len("/".join(parts)) > 240
        or any(
            not part
            or len(part) > 120
            or part.startswith(".")
            or part.endswith((".", " "))
            or not all(c.isalnum() or c in " ._-" for c in part)
            for part in parts
        )
        or not name.lower().endswith(".pdf")
    ):
        raise DocumentProblem(
            400,
            "Nom ou dossier interdit. Utilisez un chemin relatif sans lien symbolique.",
        )
    return "/".join(parts)


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_pdf(path):
    try:
        with Path(path).open("rb") as stream:
            if not stream.read(8).startswith(b"%PDF-"):
                raise ValueError("signature")
        with fitz.open(path) as pdf:
            if not pdf.is_pdf or pdf.is_repaired or pdf.needs_pass or not len(pdf):
                raise ValueError("invalid, damaged or encrypted")
            # Force page-tree validation, without retaining page text in memory.
            for page in pdf:
                _ = page.rect
            return {
                "pages": len(pdf),
                "size": Path(path).stat().st_size,
                "file_hash": file_hash(path),
            }
    except Exception as exc:
        raise DocumentProblem(
            400, "PDF invalide, endommagé ou chiffré. Aucun document n’a été remplacé."
        ) from exc


class DocumentState:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.document_state_path).absolute()
        documents = Path(config.documents_path).absolute()
        if self.root.is_symlink() or self.root.resolve().is_relative_to(
            documents.resolve()
        ):
            raise ValueError(
                "DOCUMENT_STATE_PATH must be outside DOCUMENTS_PATH and not a symlink"
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in ("staging", "versions", "archive"):
            (self.root / directory).mkdir(exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    path TEXT PRIMARY KEY, pending TEXT, removed INTEGER NOT NULL DEFAULT 0,
                    error TEXT, archived TEXT, info TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "documents.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def records(self):
        with self.db() as db:
            return {
                row["path"]: {
                    **dict(row),
                    "pending": json.loads(row["pending"]) if row["pending"] else None,
                }
                for row in db.execute("SELECT * FROM documents")
            }

    def record(self, path, **fields):
        if set(fields) - {"pending", "removed", "error", "archived", "info"}:
            raise ValueError("invalid field")
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO documents(path) VALUES(?)", (path,))
            for key, value in fields.items():
                if key in ("pending", "info") and value is not None:
                    value = json.dumps(value)
                db.execute(f"UPDATE documents SET {key}=? WHERE path=?", (value, path))

    def jobs(self):
        with self.db() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT data FROM jobs ORDER BY rowid DESC LIMIT 100"
                )
            ]

    def save_job(self, job):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO jobs VALUES(?,?)", (job["id"], json.dumps(job))
            )

    @contextmanager
    def parent(self, document, *, create=False):
        """Anchor every component with openat/O_NOFOLLOW, including the root.

        Directory symlinks cannot redirect upload, replace, delete, or file reads.
        Docker's /documents mount and state volume are administrator-controlled.
        """
        parts = document.split("/")
        validate_path(parts[-1], "/".join(parts[:-1]))
        descriptors = []
        try:
            fd = os.open(
                self.config.documents_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            descriptors.append(fd)
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o750, dir_fd=fd)
                    except FileExistsError:
                        pass
                fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                descriptors.append(fd)
            yield fd, parts[-1]
        except (OSError, ValueError) as exc:
            raise DocumentProblem(
                400,
                "Dossier inaccessible ou chemin interdit (les liens symboliques sont refusés).",
            ) from exc
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    @contextmanager
    def open_document(self, document):
        with self.parent(document) as (fd, name):
            with os.fdopen(
                os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd), "rb"
            ) as stream:
                yield stream

    def current_hash(self, document):
        with self.parent(document) as (fd, name):
            try:
                stream_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return None
            with os.fdopen(stream_fd, "rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()

    def install(self, document, candidate, *, expected=None):
        with self.parent(document, create=True) as (fd, name):
            temporary = f".upload-{uuid.uuid4().hex}"
            try:
                out_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o640,
                    dir_fd=fd,
                )
                with (
                    os.fdopen(out_fd, "wb") as target,
                    Path(candidate).open("rb") as source,
                ):
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                if expected is not None:
                    if self.current_hash(document) != expected:
                        raise DocumentProblem(
                            409,
                            "Le fichier a changé sur disque. Le remplacement est conservé pour vérification.",
                        )
                    os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                else:
                    try:
                        os.link(
                            temporary,
                            name,
                            src_dir_fd=fd,
                            dst_dir_fd=fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError as exc:
                        raise DocumentProblem(
                            409,
                            "Ce nom existe déjà. Choisissez Remplacer ou un autre nom.",
                        ) from exc
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass

    def version_path(self, identifier, version):
        if not re.fullmatch(r"[0-9a-f-]{36}", identifier) or not re.fullmatch(
            r"[0-9a-f]{64}", version
        ):
            raise DocumentProblem(404, "Source inconnue.")
        return self.root / "versions" / identifier / f"{version}.pdf"

    def preserve(self, document, version, source):
        path = self.version_path(document_id(document), version)
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                if file_hash(temporary) != version:
                    raise DocumentError("SOURCE_CHANGED")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        if file_hash(path) != version:
            raise DocumentError("VERSION_DAMAGED")
        return path


class ManagedPdfSource(PdfSource):
    """The existing pipeline reads staged replacements and ignores retired paths."""

    def __init__(self, config, state=None):
        super().__init__(config.documents_path, config.chunk_size, config.chunk_overlap)
        self.state = state or DocumentState(config)

    def inventory(self):
        found = super().inventory()
        for path, record in self.state.records().items():
            if record["removed"]:
                found.pop(path, None)
            elif record["pending"]:
                stat = (self.state.root / "staging" / record["pending"]["name"]).stat()
                found[path] = (
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
        return found

    @contextmanager
    def stream(self, document):
        record = self.state.records().get(document, {})
        if record.get("pending"):
            with (self.state.root / "staging" / record["pending"]["name"]).open(
                "rb"
            ) as source:
                yield source
        else:
            with self.state.open_document(document) as source:
                yield source

    def fingerprint(self, document):
        pending = self.state.records().get(document, {}).get("pending")
        if pending and self.state.current_hash(document) not in (
            pending["expected"],
            pending["file_hash"],
        ):
            raise DocumentError("SOURCE_CHANGED")
        try:
            with self.stream(document) as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()
        except DocumentProblem as exc:
            raise DocumentError("READ_FAILED") from exc

    def snapshot(self, document, expected_hash):
        with self.stream(document) as stream:
            return self.state.preserve(document, expected_hash, stream)

    def passages(self, document, expected_hash):
        with fitz.open(self.snapshot(document, expected_hash)) as pdf:
            for number, page in enumerate(pdf, 1):
                for text in self.page_passages(page):
                    yield number, text

    def metadata(self, document, expected_hash):
        path = self.snapshot(document, expected_hash)
        with fitz.open(path) as pdf:
            return {
                "file_hash": expected_hash,
                "pages": len(pdf),
                "size": path.stat().st_size,
                "indexed_at": now(),
            }

    def excluded(self):
        return {
            path for path, record in self.state.records().items() if record["removed"]
        }
