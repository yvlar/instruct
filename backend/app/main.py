import asyncio
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .document_storage import DocumentProblem, validate_path, validate_pdf
from .documents import DocumentManager
from .indexing import IndexErrorBase, index_lock
from .schemas import Answer, IngestionResult, Question
from .services import KnowledgeBase, knowledge_base


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = DocumentManager(
        knowledge_base.settings, lambda: KnowledgeBase(config=knowledge_base.settings)
    )
    manager.start()
    app.state.documents = manager
    yield
    await asyncio.to_thread(manager.close)
    await knowledge_base.close()


app = FastAPI(title="Instruct IA", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(DocumentProblem)
async def document_error(_: Request, exc: DocumentProblem):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


@app.exception_handler(IndexErrorBase)
async def index_error(_: Request, exc: IndexErrorBase):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.middleware("http")
async def local_mutations(request: Request, call_next):
    # Prevent cross-origin form/fetch writes to this unauthenticated local service.
    origin = request.headers.get("origin")
    if (
        request.method in ("POST", "PUT", "DELETE")
        and origin
        and origin
        not in (
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        )
    ):
        return JSONResponse(
            status_code=403, content={"detail": "Origine non autorisée."}
        )
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/ingest", response_model=IngestionResult)
async def ingest(request: Request, allow_empty: bool = False) -> IngestionResult:
    manager = request.app.state.documents
    try:
        with manager.admission(), index_lock(knowledge_base.settings):
            result = await manager.synchronize(knowledge_base, allow_empty=allow_empty)
            return IngestionResult(**result)
    except (IndexErrorBase, DocumentProblem):
        raise
    except Exception as exc:
        raise HTTPException(
            503, "Ingestion impossible; vérifiez les services locaux."
        ) from exc


@app.post("/api/ask", response_model=Answer)
async def ask(payload: Question) -> Answer:
    try:
        return Answer(**await knowledge_base.ask(payload.question))
    except IndexErrorBase:
        raise
    except Exception as exc:
        raise HTTPException(
            503, "Assistant indisponible; vérifiez les services locaux."
        ) from exc


@app.get("/api/documents")
async def documents(
    request: Request,
    q: str = Query("", max_length=240),
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        return await asyncio.to_thread(
            request.app.state.documents.listing,
            knowledge_base,
            q,
            status,
            page,
            page_size,
        )
    except (IndexErrorBase, DocumentProblem):
        raise
    except Exception as exc:
        raise HTTPException(
            503, "Liste indisponible. Vérifiez Qdrant et le dossier documentaire."
        ) from exc


@app.put("/api/documents", status_code=201)
async def upload(
    request: Request,
    name: str = Query(..., max_length=120),
    folder: str = Query("", max_length=240),
    replace_id: str | None = None,
):
    manager = request.app.state.documents
    validate_path(name, folder)
    if request.headers.get("content-type", "").split(";")[0] != "application/pdf":
        raise HTTPException(
            415, "Envoyez le contenu PDF avec Content-Type: application/pdf."
        )
    maximum = manager.config.max_pdf_bytes
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > maximum):
        raise HTTPException(
            413, f"Le PDF dépasse la limite de {maximum // (1024 * 1024)} Mo."
        )
    if not manager.upload_slots.acquire(blocking=False):
        raise HTTPException(
            409, "Deux envois sont déjà en cours. Réessayez après leur fin."
        )
    staged = manager.state.root / "staging" / f"{uuid.uuid4().hex}.pdf"
    keep = False
    try:
        size = 0
        with staged.open("xb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(
                        413, "Le PDF dépasse la taille maximale autorisée."
                    )
                # ASGI chunks are bounded by the server; large PDFs are never held in memory.
                await asyncio.to_thread(stream.write, chunk)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = await asyncio.to_thread(validate_pdf, staged)
        acceptance = asyncio.create_task(
            asyncio.to_thread(
                manager.accept_upload,
                staged,
                metadata,
                name,
                folder,
                replace_id,
                knowledge_base,
            )
        )
        try:
            result = await asyncio.shield(acceptance)
        except asyncio.CancelledError:
            result = await acceptance
            keep = result["replacement_pending"]
            raise
        keep = result["replacement_pending"]
        return result
    finally:
        if not keep:
            staged.unlink(missing_ok=True)
        manager.upload_slots.release()


@app.post("/api/documents/sync", status_code=202)
async def synchronize_documents(request: Request):
    return await asyncio.to_thread(
        request.app.state.documents.submit, "sync", knowledge_base
    )


@app.post("/api/documents/{identifier}/index", status_code=202)
async def index_document(identifier: str, request: Request):
    return await asyncio.to_thread(
        request.app.state.documents.submit, "index", knowledge_base, identifier
    )


@app.post("/api/documents/{identifier}/remove", status_code=202)
async def remove_document(identifier: str, request: Request):
    return await asyncio.to_thread(
        request.app.state.documents.submit, "remove", knowledge_base, identifier
    )


@app.post("/api/document-jobs/{identifier}/retry", status_code=202)
async def retry_job(identifier: str, request: Request):
    return await asyncio.to_thread(
        request.app.state.documents.retry, identifier, knowledge_base
    )


@app.get("/api/documents/{identifier}/source")
async def source_info(
    identifier: str, request: Request, version: str, page: int = Query(1, ge=1)
):
    await asyncio.to_thread(
        request.app.state.documents.source, identifier, version, page
    )
    return {
        "document_id": identifier,
        "version": version,
        "page": page,
        "url": f"/api/documents/{identifier}/file?version={version}&page={page}",
    }


@app.get("/api/documents/{identifier}/file")
async def source_file(
    identifier: str,
    request: Request,
    version: str,
    page: int = Query(1, ge=1),
    download: bool = False,
):
    path = await asyncio.to_thread(
        request.app.state.documents.source, identifier, version, page
    )
    # The only served path is an immutable server-owned snapshot, never a query-supplied path.
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"source-{identifier}.pdf",
        content_disposition_type="attachment" if download else "inline",
        headers={"Content-Security-Policy": "sandbox", "Cache-Control": "no-store"},
    )
