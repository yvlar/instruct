"""Real synthetic PDFs, actual extraction/search, scripted model in CI."""

import json

import httpx
import pytest
from app.config import Settings
from app.services import KnowledgeBase
from evaluation.run_grounding import load_data, matches, write_pdfs
from test_grounding import ScriptedOllama, run
from test_indexing import FaultyQdrant


@pytest.mark.parametrize("case", load_data()["cases"], ids=lambda c: c["id"])
def test_demo_pdf_grounding_contract(tmp_path, case):
    data = load_data()
    root = tmp_path / "pdf"
    write_pdfs(root, {name: data["documents"][name] for name in case["documents"]})
    ollama = ScriptedOllama()

    def select(passages):
        # Scripted decisions exercise the contract, not live model understanding.
        if not case["grounded"]:
            return {
                "status": "ambiguous" if case["id"] == "ambigu" else "insufficient",
                "answer": [],
            }
        return {
            "status": "answered",
            "answer": [
                {"source_id": p["source_id"], "quote": expected["contains"]}
                for expected in case["evidence"]
                for p in passages
                if expected["contains"] in p["text"]
            ],
        }

    ollama.select = select
    kb = KnowledgeBase(
        config=Settings(
            _env_file=None,
            documents_path=str(root),
            index_lock_path=str(tmp_path / "locks"),
        ),
        qdrant=FaultyQdrant(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(ollama)),
    )
    try:
        assert run(kb.ingest())["failed"] == 0
        result = run(kb.ask(case["question"]))
        assert matches(case, result), json.dumps(result)
        assert len(ollama.chat_requests) <= 1
        if case["id"] == "injection":
            assert not ollama.chat_requests
    finally:
        run(kb.close())
