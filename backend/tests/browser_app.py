"""Isolated browser fixture: real API/PDF/index pipeline, controlled local services.

Run only for tests. Never mounted by the production Docker image.
"""

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
    documents_path=str(root / "documents"),
    document_state_path=str(root / "state"),
    index_lock_path=str(root / "locks"),
)
qdrant = FaultyQdrant()
qdrant.close = lambda: None
ollama = FakeOllama()


def controlled_ollama(request):
    if request.url.path == "/api/chat":
        body = json.loads(request.content)
        assert "DEMO-42" in body["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "La procédure fictive indique 12 unités pour la machine DEMO-42 [maintenance/fiche-demo.pdf, p. 2]. Vérifiez la version officielle avant toute opération."
                }
            },
        )
    return ollama(request)


def factory(**kwargs):
    return KnowledgeBase(
        config=config,
        qdrant=qdrant,
        http=httpx.AsyncClient(transport=httpx.MockTransport(controlled_ollama)),
    )


main.knowledge_base = factory()
main.KnowledgeBase = factory
app = main.app
