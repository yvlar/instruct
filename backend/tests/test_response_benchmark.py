import argparse
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from benchmarks import response_modes as bench
from qdrant_client import QdrantClient
from test_grounding import ScriptedOllama


def test_full_benchmark(tmp_path, monkeypatch):
    ollama = ScriptedOllama()
    ollama.message_extra = {"thinking": "SECRET_THINKING", "cookie": "SECRET_COOKIE"}
    resident = []

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": resident})
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/generate":
            resident.clear()
            return httpx.Response(200, json={"done": True})
        if request.url.path in {"/api/chat", "/api/embed"}:
            resident[:] = [{"name": body["model"], "size": 100, "size_vram": 80}]
        if request.url.path == "/api/chat":
            prompt = json.loads(body["messages"][1]["content"])["QUESTION"]

            def select(passages):
                if "masse" in prompt:
                    return {"status": "insufficient", "answer": []}
                return {
                    "status": "answered",
                    "answer": [
                        {"source_id": p["source_id"], "quote": p["text"]}
                        for p in passages
                        if "vertes" in prompt or "bleues" in p["text"]
                    ],
                }

            ollama.select = select
        response = ollama(request)
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                json={
                    **response.json(),
                    "eval_count": 42,
                    "load_duration": 123,
                    "total_duration": 456,
                    "error": "SECRET_DOCUMENT",
                },
            )
        return response

    real_http = bench.MeasuredHTTP
    monkeypatch.setattr(
        bench,
        "MeasuredHTTP",
        lambda url: real_http(url, transport=httpx.MockTransport(handler)),
    )
    qdrant = QdrantClient(":memory:")
    qdrant.close = lambda: None
    monkeypatch.setattr(bench, "QdrantClient", lambda **_: qdrant)
    monkeypatch.setattr(
        bench, "system_snapshot", lambda: {"ram_kib": None, "gpu_mib": None}
    )
    monkeypatch.setenv("DOCUMENT_STATE_PATH", str(tmp_path / "private"))
    output = tmp_path / "report.json"
    args = SimpleNamespace(
        ollama_url="http://localhost:11434",
        qdrant_url="http://localhost:6333",
        model="qwen3:8b",
        embedding_model="nomic-embed-text",
        repeat=1,
        output=str(output),
    )
    assert asyncio.run(bench.benchmark(args)) == 0
    serialized = output.read_text()
    report = json.loads(serialized)
    assert len(report["runs"]) == 18
    assert qdrant.get_collections().collections == []
    assert not (tmp_path / "private").exists()
    for secret in (
        "SECRET_",
        "thinking",
        "csrf",
        "cookie",
        "password",
        "Les cartes",
        "question",
        "excerpt",
    ):
        assert secret not in serialized
    for row in report["runs"]:
        assert row["http_status"] == 200
        assert row["api_seconds"] >= row["probe_seconds"] >= 0
        if row["phase"] == "cold":
            assert row["cold_verified"] is True and row["resident_before"] == []
        if row["mode"] == "search":
            assert row["quality"]["passed"] and row["quality"]["chat_calls"] == 0
        for call in row["ollama"]:
            if call["endpoint"] == "/api/chat":
                assert call["eval_count"] == 42 and call["load_duration"] == 123
                assert call["before"][0]["model"] == "nomic-embed-text"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://user:secret@localhost",
        "http://localhost/?token=x",
    ],
)
def test_remote_or_secret_url_rejected(url):
    with pytest.raises(argparse.ArgumentTypeError):
        bench.local_url(url)


def test_optional_tools_missing(monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("private error")

    monkeypatch.setattr(bench.subprocess, "run", unavailable)
    monkeypatch.setattr(bench.Path, "read_text", unavailable)
    assert bench.system_snapshot() == {"ram_kib": None, "gpu_mib": None}


def test_missing_ps_does_not_claim_cold():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(503, json={"error": "secret"})
            )
        ) as http:
            assert await bench.residency(http, "http://localhost") is None
            assert await bench.unload(http, "http://localhost") is False

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["grounded", "page", "excerpt", "claim", "answer"])
def test_quality_rejects_regression(tmp_path, mutation):
    bench.write_pdfs(tmp_path, bench.DOCUMENTS)
    quote = bench.DOCUMENTS["bleues.pdf"][0]
    result = {
        "grounded": True,
        "answer": quote + " [1]",
        "claims": [{"text": quote, "source_ids": ["s"]}],
        "sources": [
            {
                "source_id": "s",
                "passage_id": "p",
                "document": "bleues.pdf",
                "page": 1,
                "excerpt": quote,
            }
        ],
    }
    calls = [{"endpoint": "/api/chat"}]
    assert bench.quality(bench.CASES[0], result, "fast", tmp_path, calls)["passed"]
    if mutation == "grounded":
        result["grounded"] = False
    elif mutation == "page":
        result["sources"][0]["page"] = 2
    elif mutation == "excerpt":
        result["sources"][0]["excerpt"] = "Invented quote"
    elif mutation == "claim":
        result["claims"][0]["source_ids"] = ["invented"]
    else:
        result["answer"] = "Incomplete answer [1]"
    assert not bench.quality(bench.CASES[0], result, "fast", tmp_path, calls)["passed"]
