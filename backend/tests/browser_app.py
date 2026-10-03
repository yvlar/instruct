"""Isolated browser fixture: real API/PDF/index pipeline, controlled local services.

Run only for tests. Never mounted by the production Docker image.
"""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
from app import main
from app.config import Settings
from app.services import KnowledgeBase
from test_indexing import FakeOllama, FaultyQdrant

sandbox = tempfile.TemporaryDirectory(prefix="instruct-browser-")
root = Path(sandbox.name)
(root / "documents").mkdir()
config = Settings(
    _env_file=None,
    app_origin="http://127.0.0.1:3000",
    state_path=str(root / "security"),
    documents_path=str(root / "documents"),
    document_state_path=str(root / "state"),
    index_lock_path=str(root / "locks"),
    lexical_index_path=str(root / "lexical"),
)
qdrant = FaultyQdrant()
qdrant.close = lambda: None
ollama = FakeOllama()


async def controlled_ollama(request):
    if request.url.path == "/api/chat":
        body = json.loads(request.content)
        assert "DEMO-42" in body["messages"][-1]["content"]
        await asyncio.sleep(0.6 if body.get("think") else 0.15)
    return ollama(request)


def factory(**kwargs):
    return KnowledgeBase(
        config=config,
        qdrant=qdrant,
        http=httpx.AsyncClient(transport=httpx.MockTransport(controlled_ollama)),
    )


app = main.create_app(config, kb=factory())
app.state.library.factory = factory
app.state.security.create_user(
    "admin", "Synthetic-password-123", "admin", bootstrap=True
)


@app.get("/__test/metrics")
async def metrics():
    return {
        "chats": len(ollama.chat_requests),
        "embeddings": len(ollama.inputs),
        "requests": ollama.chat_requests,
    }
