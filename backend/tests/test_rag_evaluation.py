import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from app.services import KnowledgeBase
from evaluation import run
from evaluation.checks import load_cases
from test_indexing import FaultyQdrant
from test_rag_regression import ControlledOllama


@pytest.mark.parametrize("malformed", [False, True])
def test_report_detects_failures_and_only_cleans_its_collections(
    monkeypatch, malformed
):
    stores = []

    def create_kb(*, config):
        model = ControlledOllama()

        def select(context):
            if malformed:
                return "broken model output"
            question = context["question"]
            term = "ZX-417" if "ZX-417" in question else "kPa"
            identifiers = (
                []
                if "huile" in question
                else [p["id"] for p in context["passages"] if term in p["text"]]
            )
            return json.dumps({"passage_ids": identifiers})

        model.output = select
        qdrant = FaultyQdrant()
        deleted = []
        original = qdrant.delete_collection

        def delete(name):
            deleted.append(name)
            return original(name)

        monkeypatch.setattr(qdrant, "delete_collection", delete)
        stores.append((config.qdrant_collection, deleted))
        return KnowledgeBase(
            config=config,
            qdrant=qdrant,
            http=httpx.AsyncClient(transport=httpx.MockTransport(model)),
        )

    monkeypatch.setattr(run, "KnowledgeBase", create_kb)
    args = SimpleNamespace(
        chat_model="synthetic",
        embedding_model="nomic-embed-text",
        qdrant_url="http://not-used.invalid",
        ollama_url="http://not-used.invalid",
        min_score=0.20,
    )
    report = asyncio.run(run.evaluate(args))
    assert report["passed"] is (not malformed)
    assert len(report["questions"]) == len(load_cases())
    for entry in report["questions"]:
        assert entry["duration_seconds"] >= 0
        assert {
            "answer",
            "sources",
            "expected_refusal",
            "refused",
            "failures",
        } <= entry.keys()
    assert report["cleanup_errors"] == []
    assert len(stores) == 3
    for name, deleted in stores:
        assert name.startswith("instruct_eval_")
        assert deleted == [name + "__manifest", name]
