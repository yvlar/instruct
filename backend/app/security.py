"""Local identity/ACL store. SQLite transactions are the authorization boundary."""

import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import portalocker
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException

DB_SCHEMA = 1
ROLES = {"reader", "manager", "admin"}
PASSWORDS = PasswordHasher()  # maintained Argon2id RFC 9106 low-memory profile
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))


def utc():
    return datetime.now(timezone.utc).isoformat()


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def validate_password(password):
    if not 12 <= len(password) <= 256:
        raise HTTPException(
            422, "Le mot de passe doit contenir de 12 à 256 caractères."
        )


@contextmanager
def maintenance_lock(config, *, exclusive=False):
    root = Path(config.state_path)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "maintenance.lock").open("a+b") as handle:
        try:
            portalocker.lock(
                handle,
                (portalocker.LOCK_EX if exclusive else portalocker.LOCK_SH)
                | portalocker.LOCK_NB,
            )
        except portalocker.exceptions.LockException as exc:
            raise HTTPException(
                503, "Maintenance en cours; réessayez plus tard."
            ) from exc
        try:
            yield
        finally:
            portalocker.unlock(handle)


class SecurityStore:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.state_path)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / "security.sqlite3"
        with maintenance_lock(config, exclusive=True):
            with self.connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, DB_SCHEMA):
                    raise RuntimeError("Schéma de sécurité incompatible")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('reader','manager','admin')),
                        active INTEGER NOT NULL DEFAULT 1);
                    CREATE TABLE IF NOT EXISTS groups (
                        id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
                    CREATE TABLE IF NOT EXISTS user_groups (
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                        group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
                        PRIMARY KEY(user_id, group_id));
                    CREATE TABLE IF NOT EXISTS documents (
                        id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1,
                        current_hash TEXT, indexed_hash TEXT, revision TEXT,
                        indexed_at TEXT, size INTEGER, pages INTEGER);
                    CREATE TABLE IF NOT EXISTS document_groups (
                        document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
                        group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
                        PRIMARY KEY(document_id, group_id));
                    CREATE TABLE IF NOT EXISTS versions (
                        document_id TEXT REFERENCES documents(id), hash TEXT NOT NULL,
                        created_at TEXT NOT NULL, pages INTEGER NOT NULL,
                        PRIMARY KEY(document_id, hash));
                    CREATE TABLE IF NOT EXISTS sessions (
                        token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                        expires REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS attempts (
                        key TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS audit (
                        id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor INTEGER,
                        action TEXT NOT NULL, resource TEXT NOT NULL, result TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS audit_at ON audit(at);
                    CREATE TABLE IF NOT EXISTS configuration (
                        key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                    PRAGMA user_version=1;
                """)
                db.execute(
                    "INSERT OR IGNORE INTO configuration VALUES ('audit_retention_days', ?)",
                    (config.audit_retention_days,),
                )
                db.execute(
                    "INSERT OR IGNORE INTO configuration VALUES ('access_version', 0)"
                )
            self.path.chmod(0o600)
            key = self.root / "session.key"
            if not key.exists():
                key.write_text(secrets.token_urlsafe(48))
            key.chmod(0o600)
            self.secret = key.read_text()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def event(self, db, actor, action, resource, result="success"):
        db.execute(
            "INSERT INTO audit(at, actor, action, resource, result) VALUES (?,?,?,?,?)",
            (utc(), actor, action, str(resource), result),
        )
        if action.startswith(("document.", "account.", "group.")):
            db.execute(
                "UPDATE configuration SET value=value+1 WHERE key='access_version'"
            )
        days = db.execute(
            "SELECT value FROM configuration WHERE key='audit_retention_days'"
        ).fetchone()[0]
        cutoff = datetime.fromtimestamp(
            time.time() - days * 86400, timezone.utc
        ).isoformat()
        db.execute("DELETE FROM audit WHERE at < ?", (cutoff,))

    def audit(self, actor, action, resource, result="success"):
        with self.transaction() as db:
            self.event(db, actor, action, resource, result)

    @staticmethod
    def group_ids(db, table, key, ident):
        return [
            r[0]
            for r in db.execute(f"SELECT group_id FROM {table} WHERE {key}=?", (ident,))
        ]

    def user(self, db, user_id):
        row = db.execute(
            "SELECT id, username, role, active FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Compte introuvable.")
        data = dict(row)
        data["groups"] = self.group_ids(db, "user_groups", "user_id", user_id)
        return data

    def authenticate(self, token):
        with self.connect() as db:
            row = db.execute(
                "SELECT user_id FROM sessions JOIN users ON users.id=sessions.user_id "
                "WHERE token_hash=? AND expires>? AND active=1",
                (token_hash(token or ""), time.time()),
            ).fetchone()
            if row is None:
                raise HTTPException(401, "Connexion requise ou session expirée.")
            return self.user(db, row[0])

    def login(self, username, password, ip):
        name = username.lower().strip()
        now = time.time()
        # Reserve a slot before Argon2, including concurrent requests and unknown users.
        limited = False
        reservations = []
        with self.transaction() as db:
            db.execute("DELETE FROM attempts WHERE started<?", (now - 900,))
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            for key, limit in (
                ("name:" + name, self.config.login_attempts),
                ("ip:" + ip, self.config.login_attempts * 10),
            ):
                hashed = token_hash(key)
                row = db.execute(
                    "SELECT count, started FROM attempts WHERE key=?", (hashed,)
                ).fetchone()
                if row and row[0] >= limit:
                    limited = True
                db.execute(
                    "INSERT INTO attempts VALUES (?,?,1) ON CONFLICT(key) "
                    "DO UPDATE SET count=count+1",
                    (hashed, now),
                )
                reservations.append((hashed, row["started"] if row else now))
            if limited:
                self.event(db, None, "login", "session", "rate_limited")
        if limited:
            raise HTTPException(429, "Trop de tentatives; réessayez dans 15 minutes.")
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        valid = False
        try:
            valid = PASSWORDS.verify(
                row["password_hash"] if row else DUMMY_HASH, password
            )
        except (VerificationError, InvalidHashError):
            pass
        with self.transaction() as db:
            # Recheck active/password under the write transaction (reset/login race).
            current = db.execute(
                "SELECT * FROM users WHERE username=?", (name,)
            ).fetchone()
            if (
                not valid
                or not current
                or not current["active"]
                or current["password_hash"] != row["password_hash"]
            ):
                self.event(
                    db, row["id"] if row else None, "login", "session", "failure"
                )
                token = None
            else:
                token = secrets.token_urlsafe(32)
                db.execute(
                    "INSERT INTO sessions VALUES (?,?,?)",
                    (
                        token_hash(token),
                        row["id"],
                        now + self.config.session_seconds,
                    ),
                )
                if PASSWORDS.check_needs_rehash(current["password_hash"]):
                    db.execute(
                        "UPDATE users SET password_hash=? WHERE id=?",
                        (PASSWORDS.hash(password), row["id"]),
                    )
                self.event(db, row["id"], "login", "session")
                # Release only this successful attempt, never other failures or
                # in-flight requests. An expired/replaced window is not ours.
                db.executemany(
                    "UPDATE attempts SET count=count-1 "
                    "WHERE key=? AND started=? AND count>0",
                    reservations,
                )
        if token is None:
            raise HTTPException(401, "Identifiants invalides.")
        return token

    def logout(self, token, actor):
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
            self.event(db, actor, "logout", "session")

    @staticmethod
    def set_groups(db, table, key, ident, groups):
        if any(
            not db.execute("SELECT id FROM groups WHERE id=?", (g,)).fetchone()
            for g in groups
        ):
            raise HTTPException(422, "Groupe inconnu.")
        db.execute(f"DELETE FROM {table} WHERE {key}=?", (ident,))
        db.executemany(
            f"INSERT INTO {table} VALUES (?,?)",
            ((ident, g) for g in sorted(set(groups))),
        )

    def create_user(
        self,
        username,
        password,
        role="reader",
        groups=(),
        actor=None,
        *,
        bootstrap=False,
    ):
        name = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", name) or role not in ROLES:
            raise HTTPException(422, "Identifiant ou rôle invalide.")
        validate_password(password)
        hashed = PASSWORDS.hash(password)
        with self.transaction() as db:
            if (
                bootstrap
                and db.execute(
                    "SELECT 1 FROM users WHERE role='admin' AND active=1"
                ).fetchone()
            ):
                raise HTTPException(
                    409, "Un administrateur existe déjà; utilisez recover-admin."
                )
            try:
                cursor = db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",
                    (name, hashed, role),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "Cet identifiant existe déjà.") from exc
            ident = cursor.lastrowid
            self.set_groups(db, "user_groups", "user_id", ident, groups)
            self.event(db, actor, "account.create", ident)
            return self.user(db, ident)

    def update_user(self, user_id, *, role, active, groups, actor):
        with self.transaction() as db:
            old = self.user(db, user_id)
            if role not in ROLES:
                raise HTTPException(422, "Rôle invalide.")
            admins = db.execute(
                "SELECT count(*) FROM users WHERE role='admin' AND active=1"
            ).fetchone()[0]
            if (
                old["role"] == "admin"
                and old["active"]
                and (role != "admin" or not active)
                and admins <= 1
            ):
                raise HTTPException(
                    409, "Le dernier administrateur actif doit être conservé."
                )
            db.execute(
                "UPDATE users SET role=?,active=? WHERE id=?", (role, active, user_id)
            )
            self.set_groups(db, "user_groups", "user_id", user_id, groups)
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            for action in ("account.update", "account.role", "account.groups"):
                self.event(db, actor, action, user_id)
            if not active:
                self.event(db, actor, "account.disable", user_id)
            return self.user(db, user_id)

    def reset_password(self, user_id, password, actor, *, current=None, recover=False):
        validate_password(password)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Compte introuvable.")
            if current is not None:
                try:
                    PASSWORDS.verify(row["password_hash"], current)
                except (VerificationError, InvalidHashError) as exc:
                    raise HTTPException(403, "Mot de passe actuel incorrect.") from exc
            db.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (PASSWORDS.hash(password), user_id),
            )
            if recover:
                db.execute(
                    "UPDATE users SET role='admin',active=1 WHERE id=?", (user_id,)
                )
                db.execute("DELETE FROM attempts")
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.event(
                db, actor, "account.recover" if recover else "account.password", user_id
            )

    def allowed_documents(self, user):
        with self.connect() as db:
            if user["role"] == "admin":
                rows = db.execute("SELECT path FROM documents WHERE active=1")
            else:
                rows = db.execute(
                    "SELECT DISTINCT d.path FROM documents d "
                    "JOIN document_groups dg ON dg.document_id=d.id "
                    "JOIN user_groups ug ON ug.group_id=dg.group_id "
                    "WHERE ug.user_id=? AND d.active=1",
                    (user["id"],),
                )
            return {row[0] for row in rows}

    def require_document(self, ident, user, *, manage=False):
        if manage and user["role"] not in {"admin", "manager"}:
            raise HTTPException(403, "Gestion documentaire non autorisée.")
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM documents WHERE id=? AND active=1", (ident,)
            ).fetchone()
        if row is None or row["path"] not in self.allowed_documents(user):
            raise HTTPException(404, "Document introuvable.")
        return dict(row)
