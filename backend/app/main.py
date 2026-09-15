from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .schemas import Answer, IngestionResult, Question
from .services import knowledge_base


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await knowledge_base.http.aclose()


app = FastAPI(title="Instruct IA", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"], allow_methods=["*"], allow_headers=["*"])


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/ingest", response_model=IngestionResult)
async def ingest() -> IngestionResult:
    try:
        documents, chunks = await knowledge_base.ingest()
        return IngestionResult(documents=documents, chunks=chunks)
    except Exception as exc:
        raise HTTPException(503, f"Ingestion impossible: {exc}") from exc


@app.post("/api/ask", response_model=Answer)
async def ask(payload: Question) -> Answer:
    try:
        return Answer(**await knowledge_base.ask(payload.question))
    except Exception as exc:
        raise HTTPException(503, f"Assistant indisponible: {exc}") from exc

