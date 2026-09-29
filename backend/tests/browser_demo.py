"""Isolated manual browser fixture. Synthetic credentials; never used by the app.

Run from backend: PYTHONPATH=.:tests python tests/browser_demo.py
It restores a genuine backup before starting the HTTP server. No GPU/network calls.
"""

import asyncio
import tempfile
from pathlib import Path

import httpx
import uvicorn
from qdrant_client import QdrantClient

from app.backup import backup, restore
from app.config import Settings
from app.main import create_app
from app.services import KnowledgeBase
from test_indexing import FakeOllama, FaultyQdrant
from test_security import PASSWORD, pdf_bytes, sign_in


def main():
    with tempfile.TemporaryDirectory(prefix="instruct-browser-") as temp:
        root = Path(temp)
        config = Settings(
            _env_file=None,
            state_path=str(root / "state"),
            documents_path=str(root / "documents"),
            index_lock_path=str(root / "locks"),
        )
        Path(config.documents_path).mkdir()

        def kb_for(cfg, qdrant):
            return KnowledgeBase(
                config=cfg,
                qdrant=qdrant,
                http=httpx.AsyncClient(transport=httpx.MockTransport(FakeOllama())),
            )

        source = kb_for(config, FaultyQdrant())
        app = create_app(config, kb=source)
        app.state.security.create_user("admin", PASSWORD, "admin", bootstrap=True)
        admin = sign_in(app)
        ga = admin.post("/api/admin/groups", json={"name": "Maintenance"}).json()["id"]
        gb = admin.post("/api/admin/groups", json={"name": "Production"}).json()["id"]
        for name, group in (("alice", ga), ("bobby", gb)):
            admin.post(
                "/api/admin/users",
                json={"username": name, "password": PASSWORD, "groups": [group]},
            )
        for path, group, text in (
            ("maintenance/pompe.pdf", ga, "ALPHA - Pression de la pompe : 42 kPa."),
            ("production/vanne.pdf", gb, "BETA - Vanne confidentielle : 99 kPa."),
        ):
            result = admin.post(
                "/api/documents",
                data={"path": path, "groups": str(group)},
                files={"file": ("guide.pdf", pdf_bytes(text), "application/pdf")},
            )
            assert result.status_code == 201
        assert admin.post("/api/ingest").json()["added"] == 2
        archive = root / "backup.tar.gz"
        backup(config, source, archive)
        admin.close()
        asyncio.run(source.close())
        target = config.model_copy(
            update={
                "state_path": str(root / "restore-state"),
                "document_state_path": str(root / "restore-state") + "-manager",
                "lexical_index_path": str(root / "restore-state") + "-lexical",
                "documents_path": str(root / "restore-documents"),
                "index_lock_path": str(root / "restore-locks"),
                "qdrant_collection": "restored",
            }
        )
        qpath = str(root / "restored-qdrant")
        restored = kb_for(
            target, QdrantClient(path=qpath, force_disable_check_same_thread=True)
        )
        restore(target, restored, archive)
        asyncio.run(restored.close())
        restored = kb_for(
            target, QdrantClient(path=qpath, force_disable_check_same_thread=True)
        )
        app = create_app(target, kb=restored)
        print(
            "Restored synthetic installation. Frontend: http://localhost:3000",
            flush=True,
        )
        uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
