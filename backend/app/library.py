"""Administrative lifecycle routes, sharing the authenticated application."""

import asyncio
import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from .document_storage import DocumentProblem, validate_path, validate_pdf
from .indexing import IndexErrorBase


def register_library(app, knowledge_base, admin):
    async def authorized(request: Request, user=Depends(admin)):
        manager = request.app.state.library
        manager.actor.set((user["id"], request.session.get("token")))
        return user

    router = APIRouter(dependencies=[Depends(authorized)])

    @router.get("/api/library/documents")
    async def documents(
        request: Request,
        q: str = Query("", max_length=240),
        status: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        try:
            return await asyncio.to_thread(
                request.app.state.library.listing,
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

    @router.put("/api/library/documents", status_code=201)
    async def upload(
        request: Request,
        name: str = Query(..., max_length=120),
        folder: str = Query("", max_length=240),
        replace_id: str | None = None,
    ):
        manager = request.app.state.library
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

    @router.post("/api/library/documents/sync", status_code=202)
    async def synchronize_documents(request: Request):
        return await asyncio.to_thread(
            request.app.state.library.submit, "sync", knowledge_base
        )

    @router.post("/api/library/documents/{identifier}/index", status_code=202)
    async def index_document(identifier: str, request: Request):
        return await asyncio.to_thread(
            request.app.state.library.submit, "index", knowledge_base, identifier
        )

    @router.post("/api/library/documents/{identifier}/remove", status_code=202)
    async def remove_document(identifier: str, request: Request):
        return await asyncio.to_thread(
            request.app.state.library.submit, "remove", knowledge_base, identifier
        )

    @router.post("/api/library/document-jobs/{identifier}/retry", status_code=202)
    async def retry_job(identifier: str, request: Request):
        return await asyncio.to_thread(
            request.app.state.library.retry, identifier, knowledge_base
        )

    @router.get("/api/library/documents/{identifier}/source")
    async def source_info(
        identifier: str, request: Request, version: str, page: int = Query(1, ge=1)
    ):
        await asyncio.to_thread(
            request.app.state.library.source, identifier, version, page
        )
        return {
            "document_id": identifier,
            "version": version,
            "page": page,
            "url": f"/api/library/documents/{identifier}/file?version={version}&page={page}",
        }

    @router.get("/api/library/documents/{identifier}/file")
    async def source_file(
        identifier: str,
        request: Request,
        version: str,
        page: int = Query(1, ge=1),
        download: bool = False,
    ):
        path = await asyncio.to_thread(
            request.app.state.library.source, identifier, version, page
        )
        # The only served path is an immutable server-owned snapshot, never a query-supplied path.
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"source-{identifier}.pdf",
            content_disposition_type="attachment" if download else "inline",
            headers={"Content-Security-Policy": "sandbox", "Cache-Control": "no-store"},
        )

    app.state.library_upload = upload
    app.include_router(router)
