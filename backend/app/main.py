import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .documents import Documents
from .indexing import IndexErrorBase
from .schemas import Answer, IngestionResult, Question
from .security import SecurityStore, maintenance_lock
from .services import KnowledgeBase


class Login(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class NewUser(Login):
    role: str = "reader"
    groups: list[int] = Field(default_factory=list, max_length=100)


class UserUpdate(BaseModel):
    role: str
    active: bool
    groups: list[int] = Field(default_factory=list, max_length=100)


class Password(BaseModel):
    password: str = Field(min_length=12, max_length=256)
    current: str = Field(default="", max_length=256)


class Group(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w .-]+$")


class Grants(BaseModel):
    groups: list[int] = Field(max_length=100)


class Configuration(BaseModel):
    audit_retention_days: int = Field(ge=1, le=3650)


class RequestGuard:
    """Hold a shared maintenance lock through the final ASGI response byte."""

    def __init__(self, app, config, security):
        self.app, self.config, self.security = app, config, security

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        try:
            with maintenance_lock(self.config):
                if (self.security.root / "RESTORE_INCOMPLETE").exists():
                    raise HTTPException(
                        503, "Restauration incomplète; intervention locale requise."
                    )
                request = Request(scope)
                if scope["path"].startswith("/api/") and scope["path"] not in {
                    "/api/auth/session",
                    "/api/auth/login",
                }:
                    self.security.authenticate(scope.get("session", {}).get("token"))
                # Reject oversized direct-backend bodies before multipart parsing.
                max_body = (
                    self.config.max_pdf_bytes + 1048576
                    if scope["path"] == "/api/documents"
                    else 65536
                )
                try:
                    declared = int(request.headers.get("content-length", "0"))
                except ValueError as exc:
                    raise HTTPException(400, "Taille de requête invalide.") from exc
                if declared < 0 or declared > max_body:
                    raise HTTPException(413, "Requête trop volumineuse.")
                consumed = 0

                async def limited_receive():
                    nonlocal consumed
                    message = await receive()
                    if message["type"] == "http.request":
                        consumed += len(message.get("body", b""))
                        if consumed > max_body:
                            raise HTTPException(413, "Requête trop volumineuse.")
                    return message

                if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                    request = Request(scope)
                    expected = scope.get("session", {}).get("csrf", "")
                    actual = request.headers.get("x-csrf-token", "")
                    if (
                        request.headers.get("origin") != self.config.app_origin
                        or not expected
                        or not secrets.compare_digest(actual, expected)
                    ):
                        raise HTTPException(403, "Origine ou jeton CSRF invalide.")

                async def secure_send(message):
                    if message["type"] == "http.response.start":
                        message.setdefault("headers", []).extend(
                            [
                                (b"cache-control", b"no-store, private"),
                                (b"x-content-type-options", b"nosniff"),
                                (b"referrer-policy", b"no-referrer"),
                            ]
                        )
                    await send(message)

                await self.app(scope, limited_receive, secure_send)
        except HTTPException as exc:
            await JSONResponse(
                {"detail": exc.detail},
                exc.status_code,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)


def create_app(config=None, *, kb=None):
    config = config or settings
    security = SecurityStore(config)
    knowledge_base = kb or KnowledgeBase(config=config)
    documents = Documents(config, security, knowledge_base)

    @asynccontextmanager
    async def lifespan(_):
        yield
        await knowledge_base.close()

    app = FastAPI(
        title="Instruct IA",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.security = security
    app.state.documents = documents
    app.state.knowledge_base = knowledge_base
    app.add_middleware(RequestGuard, config=config, security=security)
    app.add_middleware(
        SessionMiddleware,
        secret_key=security.secret,
        session_cookie="instruct_session",
        max_age=config.session_seconds,
        same_site="strict",
        https_only=config.cookie_secure,
    )

    def current(request: Request):
        return security.authenticate(request.session.get("token"))

    def admin(user=Depends(current)):
        if user["role"] != "admin":
            raise HTTPException(403, "Administration non autorisée.")
        return user

    @app.exception_handler(IndexErrorBase)
    async def index_error(_, exc):
        return JSONResponse({"detail": str(exc)}, 503)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/api/auth/session")
    async def session(request: Request):
        request.session.setdefault("csrf", secrets.token_urlsafe(32))
        try:
            user = current(request)
        except HTTPException:
            request.session.pop("token", None)
            user = None
        with security.connect() as db:
            version = db.execute(
                "SELECT value FROM configuration WHERE key='access_version'"
            ).fetchone()[0]
        return {
            "user": user,
            "csrf": request.session["csrf"],
            "access_version": version,
        }

    @app.post("/api/auth/login")
    async def login(payload: Login, request: Request):
        token = security.login(
            payload.username,
            payload.password,
            request.client.host if request.client else "unknown",
        )
        old = request.session.get("token")
        if old:
            security.logout(old, None)
        request.session.clear()
        request.session.update(token=token, csrf=secrets.token_urlsafe(32))
        return await session(request)

    @app.post("/api/auth/logout")
    async def logout(request: Request, user=Depends(current)):
        security.logout(request.session["token"], user["id"])
        request.session.clear()
        return {"ok": True}

    @app.post("/api/auth/password")
    async def password(payload: Password, request: Request, user=Depends(current)):
        security.reset_password(
            user["id"], payload.password, user["id"], current=payload.current
        )
        request.session.clear()
        return {"ok": True}

    @app.get("/api/groups")
    async def groups(user=Depends(current)):
        with security.connect() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM groups ORDER BY name")
                if user["role"] == "admin" or r["id"] in user["groups"]
            ]

    @app.get("/api/admin/users")
    async def users(_=Depends(admin)):
        with security.connect() as db:
            return [
                security.user(db, r[0])
                for r in db.execute("SELECT id FROM users ORDER BY username")
            ]

    @app.post("/api/admin/users", status_code=201)
    async def add_user(payload: NewUser, user=Depends(admin)):
        return security.create_user(
            payload.username, payload.password, payload.role, payload.groups, user["id"]
        )

    @app.put("/api/admin/users/{ident}")
    async def update_user(ident: int, payload: UserUpdate, user=Depends(admin)):
        return security.update_user(ident, **payload.model_dump(), actor=user["id"])

    @app.post("/api/admin/users/{ident}/password")
    async def reset_user(ident: int, payload: Password, user=Depends(admin)):
        security.reset_password(ident, payload.password, user["id"])
        return {"ok": True}

    @app.post("/api/admin/groups", status_code=201)
    async def add_group(payload: Group, user=Depends(admin)):
        with security.transaction() as db:
            if db.execute(
                "SELECT 1 FROM groups WHERE name=?", (payload.name,)
            ).fetchone():
                raise HTTPException(409, "Ce groupe existe déjà.")
            ident = db.execute(
                "INSERT INTO groups(name) VALUES (?)", (payload.name,)
            ).lastrowid
            security.event(db, user["id"], "group.create", ident)
        return {"id": ident, "name": payload.name}

    @app.put("/api/admin/groups/{ident}")
    async def rename_group(ident: int, payload: Group, user=Depends(admin)):
        with security.transaction() as db:
            if db.execute(
                "SELECT 1 FROM groups WHERE name=? AND id!=?", (payload.name, ident)
            ).fetchone():
                raise HTTPException(409, "Ce groupe existe déjà.")
            if (
                db.execute(
                    "UPDATE groups SET name=? WHERE id=?", (payload.name, ident)
                ).rowcount
                == 0
            ):
                raise HTTPException(404, "Groupe introuvable.")
            security.event(db, user["id"], "group.update", ident)
        return {"ok": True}

    @app.delete("/api/admin/groups/{ident}")
    async def delete_group(ident: int, user=Depends(admin)):
        with security.transaction() as db:
            db.execute("DELETE FROM groups WHERE id=?", (ident,))
            security.event(db, user["id"], "group.delete", ident)
        return {"ok": True}

    @app.put("/api/admin/documents/{ident}/groups")
    async def grants(ident: str, payload: Grants, user=Depends(admin)):
        security.require_document(ident, user)
        with security.transaction() as db:
            security.set_groups(
                db, "document_groups", "document_id", ident, payload.groups
            )
            security.event(db, user["id"], "document.permissions", ident)
        return {"ok": True}

    @app.get("/api/admin/audit")
    async def audit(offset: int = 0, limit: int = 50, _=Depends(admin)):
        if offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, "Pagination invalide.")
        with security.connect() as db:
            total = db.execute("SELECT count(*) FROM audit").fetchone()[0]
            rows = db.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return {"total": total, "offset": offset, "items": [dict(r) for r in rows]}

    @app.get("/api/admin/configuration")
    async def configuration(_=Depends(admin)):
        with security.connect() as db:
            return {
                r[0]: r[1] for r in db.execute("SELECT key,value FROM configuration")
            }

    @app.put("/api/admin/configuration")
    async def update_configuration(payload: Configuration, user=Depends(admin)):
        with security.transaction() as db:
            db.execute(
                "UPDATE configuration SET value=? WHERE key='audit_retention_days'",
                (payload.audit_retention_days,),
            )
            security.event(
                db, user["id"], "configuration.update", "audit_retention_days"
            )
        return payload

    @app.get("/api/documents")
    async def list_documents(user=Depends(current)):
        return documents.list(user)

    @app.post("/api/documents", status_code=201)
    async def upload(
        request: Request,
        file: UploadFile = File(),
        path: str = Form(),
        groups: list[int] = Form(default=[]),
        user=Depends(current),
    ):
        if user["role"] == "reader":
            raise HTTPException(403, "Gestion documentaire non autorisée.")
        data = await file.read(config.max_pdf_bytes + 1)
        # Membership may have changed while the upload was being received.
        user = current(request)
        ident = documents.upload(path, data, groups, user)
        return {"id": ident}

    @app.delete("/api/documents/{ident}")
    async def remove(ident: str, user=Depends(current)):
        documents.remove(ident, user)
        return {"ok": True}

    @app.get("/api/documents/{ident}/versions")
    async def versions(ident: str, user=Depends(current)):
        security.require_document(ident, user)
        with security.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT hash,created_at,pages FROM versions WHERE document_id=? ORDER BY created_at DESC",
                    (ident,),
                )
            ]

    @app.get("/api/documents/{ident}/file")
    async def file(
        ident: str,
        request: Request,
        version: str | None = None,
        download: bool = False,
        user=Depends(current),
    ):
        data = documents.read(ident, user, version)
        security.require_document(ident, current(request))
        return Response(
            data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": ("attachment" if download else "inline")
                + '; filename="document.pdf"'
            },
        )

    @app.post("/api/documents/{ident}/sync", response_model=IngestionResult)
    async def sync(ident: str, request: Request, user=Depends(current)):
        result = await documents.synchronize(
            user, ident, resolve_user=lambda: current(request)
        )
        security.require_document(ident, current(request), manage=True)
        return result

    @app.post("/api/ingest", response_model=IngestionResult)
    async def ingest(request: Request, allow_empty: bool = False, user=Depends(admin)):
        try:
            result = await documents.synchronize(
                user, allow_empty=allow_empty, resolve_user=lambda: current(request)
            )
            admin(current(request))
            return result
        except (HTTPException, IndexErrorBase):
            raise
        except Exception as exc:
            raise HTTPException(
                503, "Ingestion impossible; vérifiez les services locaux."
            ) from exc

    @app.post("/api/ask", response_model=Answer)
    async def ask(payload: Question, request: Request, user=Depends(current)):
        def authorized():
            return documents.searchable(current(request))

        try:
            result = await knowledge_base.ask(
                payload.question, authorized_documents=authorized
            )
            allowed = authorized()
            if any(
                s["document"] not in allowed
                or s.get("revision") != allowed[s["document"]]
                for s in result["sources"]
            ):
                return {
                    "answer": "Les droits ont changé; relancez la recherche.",
                    "grounded": False,
                    "sources": [],
                }
            by_path = {d["path"]: d for d in documents.list(current(request))}
            if any(
                source["document"] not in by_path
                or source.get("revision") != by_path[source["document"]]["revision"]
                for source in result["sources"]
            ):
                return {
                    "answer": "Les droits ont changé; relancez la recherche.",
                    "grounded": False,
                    "sources": [],
                }
            for source in result["sources"]:
                doc = by_path[source["document"]]
                source["document_id"] = doc["id"]
                source["version"] = doc["indexed_hash"]
                source["url"] = (
                    f"/api/documents/{doc['id']}/file?version={doc['indexed_hash']}#page={source['page']}"
                )
            return result
        except (HTTPException, IndexErrorBase):
            raise
        except Exception as exc:
            raise HTTPException(
                503, "Assistant indisponible; vérifiez les services locaux."
            ) from exc

    return app
