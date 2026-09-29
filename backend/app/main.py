from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .indexing import IndexErrorBase
from .schemas import Answer, IngestionResult, Question
from .services import knowledge_base


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await knowledge_base.close()


app = FastAPI(title="Instruct IA", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/ingest", response_model=IngestionResult)
async def ingest(allow_empty: bool = False) -> IngestionResult:
    try:
        return IngestionResult(**await knowledge_base.ingest(allow_empty=allow_empty))
    except IndexErrorBase as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            503, "Ingestion impossible; vérifiez les services locaux."
        ) from exc


@app.post("/api/ask", response_model=Answer)
async def ask(payload: Question) -> Answer:
    try:
        return Answer(**await knowledge_base.ask(payload.question))
    except IndexErrorBase as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            503, "Assistant indisponible; vérifiez les services locaux."
        ) from exc
