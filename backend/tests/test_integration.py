"""Real PDF + persistent embedded Qdrant; optional existing local HTTP services."""

import asyncio
import os
import uuid
import warnings
from pathlib import Path

import fitz
import httpx
import pytest
from app.config import Settings
from app.indexing import active_filter
from app.services import KnowledgeBase
from qdrant_client import QdrantClient
from test_indexing import FakeOllama


def write_pdf(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), text)
        pdf.save(path)


def test_real_pdf_and_persistent_qdrant_survive_restart(tmp_path):
    async def scenario():
        root = tmp_path / "documents"
        path = root / "maintenance" / "fiche.pdf"
        write_pdf(path, "Ancienne instruction de maintenance.")
        config = Settings(
            _env_file=None,
            state_path=str(tmp_path / "state"),
            documents_path=str(root),
            document_state_path=str(tmp_path / "state"),
            index_lock_path=str(tmp_path / "locks"),
            lexical_index_path=str(tmp_path / "lexical"),
            embedding_batch_size=2,
        )
        storage = str(tmp_path / "qdrant")
        ollama = FakeOllama()
        kb = KnowledgeBase(
            config=config,
            qdrant=QdrantClient(path=storage),
            http=httpx.AsyncClient(transport=httpx.MockTransport(ollama)),
        )
        with warnings.catch_warnings():
            warnings.simplefilter(
                "ignore", UserWarning
            )  # Local Qdrant ignores payload indexes.
            assert (await kb.ingest())["added"] == 1
        await kb.close()
        kb = KnowledgeBase(
            config=config,
            qdrant=QdrantClient(path=storage),
            http=httpx.AsyncClient(transport=httpx.MockTransport(ollama)),
        )
        try:
            calls = len(ollama.inputs)
            assert (await kb.ingest())["unchanged"] == 1
            assert len(ollama.inputs) == calls
            # A corrupt replacement is rejected by real PyMuPDF.
            path.write_bytes(b"not a PDF")
            assert (await kb.ingest())["errors"][0]["code"] == "EXTRACTION_FAILED"
            old = await kb.ask("Instruction de maintenance?")
            assert "Ancienne" in old["sources"][0]["excerpt"]
            path.unlink()
            write_pdf(path, "Nouvelle instruction de maintenance.")
            assert (await kb.ingest())["modified"] == 1
            answer = await kb.ask("Instruction de maintenance?")
            assert answer["sources"][0]["document"] == "maintenance/fiche.pdf"
            assert "Nouvelle" in answer["sources"][0]["excerpt"]
            path.unlink()
            assert (await kb.ingest(allow_empty=True))["deleted"] == 1
            assert not (await kb.ask("Instruction?"))["grounded"]
        finally:
            await kb.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    not (
        os.getenv("INSTRUCT_TEST_QDRANT_URL") and os.getenv("INSTRUCT_TEST_OLLAMA_URL")
    ),
    reason="Set INSTRUCT_TEST_QDRANT_URL and INSTRUCT_TEST_OLLAMA_URL for existing local services",
)
def test_existing_qdrant_and_ollama_http_services(tmp_path):
    """No model pulls, no chat generation, only disposable namespaced collections."""

    async def scenario():
        root = tmp_path / "documents"
        pdf = root / "test.pdf"
        write_pdf(pdf, "Une instruction synthetique pour le test d'indexation.")
        config = Settings(
            _env_file=None,
            state_path=str(tmp_path / "state"),
            documents_path=str(root),
            document_state_path=str(tmp_path / "state"),
            qdrant_url=os.environ["INSTRUCT_TEST_QDRANT_URL"],
            ollama_url=os.environ["INSTRUCT_TEST_OLLAMA_URL"],
            embedding_model=os.getenv(
                "INSTRUCT_TEST_EMBEDDING_MODEL", "nomic-embed-text"
            ),
            qdrant_collection=f"instruct_test_{uuid.uuid4().hex}",
            index_lock_path=str(tmp_path / "locks"),
            lexical_index_path=str(tmp_path / "lexical"),
        )
        kb = KnowledgeBase(config=config)
        try:
            assert (await kb.ingest())["added"] == 1
            assert (await kb.ingest())["unchanged"] == 1
            pdf.unlink()
            write_pdf(pdf, "Une instruction modifiee pour le test.")
            assert (await kb.ingest())["modified"] == 1
            _, manifests = kb.store.read()
            hits = kb.qdrant.query_points(
                config.qdrant_collection,
                query=await kb.embed("instruction modifiee"),
                query_filter=active_filter(manifests),
                with_payload=True,
            ).points
            assert hits and "modifiee" in hits[0].payload["text"]
            assert kb.qdrant.count(config.qdrant_collection).count == 1
            pdf.unlink()
            assert (await kb.ingest(allow_empty=True))["deleted"] == 1
        finally:
            for name in (kb.store.manifest_collection, config.qdrant_collection):
                if kb.qdrant.collection_exists(name):
                    kb.qdrant.delete_collection(name)
            await kb.close()

    asyncio.run(scenario())
