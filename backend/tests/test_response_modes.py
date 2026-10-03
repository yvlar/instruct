"""Modes traverse existing retrieval, citations and real authorization, offline."""

# Fixtures imported below are intentionally injected by pytest.
# ruff: noqa: F811
import asyncio
import json

import httpx
import pytest
from app.config import Settings
from app.ollama import Capability, OllamaAdapter, ResponseProblem
from app.schemas import Question
from pydantic import ValidationError
from test_grounding import env, run  # noqa: F401
from test_security import secured  # noqa: F401


def test_default_and_unknown_modes():
    assert Question(question="Question?").mode == "fast"
    for mode in [None, "slow", "high", "Rapide", 1, True]:
        with pytest.raises(ValidationError):
            Question(question="Question?", mode=mode)


@pytest.mark.parametrize(
    "mode,think,budget", [("fast", False, 768), ("reflection", True, 1536)]
)
@pytest.mark.parametrize("valid", [True, False])
def test_generation_modes_use_one_call_and_same_citation_guard(
    env, mode, think, budget, valid
):
    run(env.kb.ingest())
    if not valid:
        env.ollama.select = lambda _: {"status": "insufficient", "answer": []}
    env.ollama.message_extra = {
        "thinking": "Secret trace with invented pressure 999 kPa."
    }
    result = run(env.kb.ask("Où ranger les cartes bleues?", mode=mode))
    assert result["grounded"] is valid
    assert len(env.ollama.chat_requests) == 1
    payload = env.ollama.chat_requests[0]
    assert payload["think"] is think
    assert payload["options"]["num_predict"] == budget
    assert payload["keep_alive"] == 120
    assert ("format" in payload) is (mode == "fast")
    assert "999" not in json.dumps(result) and "thinking" not in json.dumps(result)
    if valid:
        assert result["sources"][0]["document"] == "demo/rangement.pdf"
        assert result["claims"][0]["text"] == result["sources"][0]["excerpt"]
    else:
        assert result["sources"] == [] and result["claims"] == []


@pytest.mark.parametrize("mode", ["fast", "reflection"])
def test_invented_value_with_real_id_is_refused_in_both_modes(env, mode):
    run(env.kb.ingest())
    env.ollama.select = lambda p: {
        "status": "answered",
        "answer": [
            {"source_id": p[0]["source_id"], "quote": "Régler la pression à 999 kPa."}
        ],
    }
    result = run(env.kb.ask("Où ranger les cartes?", mode=mode))
    assert not result["grounded"] and not result["sources"]


@pytest.mark.parametrize("mode", ["fast", "reflection"])
def test_incomplete_answer_is_an_explicit_error(env, mode):
    run(env.kb.ingest())
    env.ollama.finish = {"done": True, "done_reason": "length"}
    with pytest.raises(ResponseProblem, match="INCOMPLETE_GENERATION"):
        run(env.kb.ask("Où ranger les cartes?", mode=mode))
    assert len(env.ollama.chat_requests) == 1


QWEN = {
    "capabilities": ["completion", "thinking"],
    "details": {"family": "qwen3"},
    "template": "{{ if .Think }}<think>{{ else }}</think>{{ end }}",
}


@pytest.mark.parametrize(
    "version,metadata,override,fast,reflection",
    [
        ("0.12.3", QWEN, "auto", True, True),
        (
            "0.12.3",
            {k: v for k, v in QWEN.items() if k != "capabilities"},
            "auto",
            True,
            True,
        ),
        ("0.8.0", QWEN, "auto", False, False),
        ("0.8.0", QWEN, "boolean", False, False),
        ("0.12.3", {"capabilities": ["completion"]}, "auto", True, False),
        ("0.12.3", {"capabilities": ["completion"]}, "boolean", True, False),
        ("0.12.3", {"capabilities": ["embedding"]}, "auto", False, False),
        (
            "0.12.3",
            {
                "capabilities": ["completion", "thinking"],
                "details": {"family": "gptoss"},
            },
            "auto",
            False,
            False,
        ),
        ("0.12.3", {}, "auto", False, False),
        (None, {}, "auto", False, False),
        (None, {}, "boolean", True, True),
        (None, {}, "none", True, False),
        ("0.12.3", QWEN, "none", False, False),
        ("0.12.3", {**QWEN, "remote_model": "cloud-name"}, "boolean", False, False),
    ],
)
def test_capabilities_model_version_and_explicit_fallback(
    version, metadata, override, fast, reflection
):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": version})
        assert json.loads(request.content)["model"] == "custom-model:local"
        return httpx.Response(200, json=metadata)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            adapter = OllamaAdapter(
                Settings(
                    _env_file=None,
                    ollama_model="custom-model:local",
                    ollama_think_support=override,
                ),
                lambda: http,
            )
            a, b = await asyncio.gather(adapter.capabilities(), adapter.capabilities())
            assert a is b
            assert (a.fast, a.reflection) == (fast, reflection)
            assert a.public()["search"]["available"]
            if not reflection:
                with pytest.raises(ResponseProblem, match="MODE_UNAVAILABLE"):
                    await adapter.require("reflection")

    run(scenario())
    assert calls == ["/api/version", "/api/show"]


def test_non_thinking_model_omits_think(env):
    run(env.kb.ingest())
    env.kb.ollama._capability = Capability(True, False)
    assert run(env.kb.ask("Où ranger les cartes?"))["grounded"]
    assert "think" not in env.ollama.chat_requests[0]
    with pytest.raises(ResponseProblem, match="MODE_UNAVAILABLE"):
        run(env.kb.ask("Où ranger les cartes?", mode="reflection"))
    assert len(env.ollama.chat_requests) == 1


def test_search_retrieves_with_no_generation_or_generation_capability_probe(
    env, monkeypatch
):
    run(env.kb.ingest())
    before = len(env.ollama.inputs)

    async def forbidden(*args):
        pytest.fail("Search must not probe or generate")

    monkeypatch.setattr(env.kb.ollama, "capabilities", forbidden)
    result = run(env.kb.ask("Où ranger les cartes?", mode="search"))
    assert len(env.ollama.inputs) == before + 1
    assert not env.ollama.chat_requests
    assert result["sources"][0]["page"] == 1
    assert not result["grounded"] and not result["claims"] and result["answer"] == ""


@pytest.mark.parametrize("mode", ["fast", "reflection", "search"])
def test_http_modes_default_validation_permissions_and_no_cached_answers(secured, mode):
    e = secured
    alice, bob = e.login("alice"), e.login("bobby")
    for client, marker, forbidden in [
        (alice, "ALPHA", "BETA"),
        (bob, "BETA", "ALPHA"),
        (alice, "ALPHA", "BETA"),
    ]:
        before = len(e.ollama.chat_requests)
        response = client.post(
            "/api/ask", json={"question": "Quelle pression?", "mode": mode}
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["mode"] == mode
        assert result["kind"] == ("search" if mode == "search" else "answer")
        assert marker in response.text and forbidden not in response.text
        assert response.headers["cache-control"] == "no-store, private"
        assert len(e.ollama.chat_requests) == before + (mode != "search")
        assert client.get(result["sources"][0]["url"].split("#")[0]).status_code == 200
    default = alice.post("/api/ask", json={"question": "Quelle pression?"}).json()
    assert default["mode"] == "fast" and default["kind"] == "answer"
    assert (
        alice.post(
            "/api/ask", json={"question": "Quelle pression?", "mode": "high"}
        ).status_code
        == 422
    )


def test_deepen_searches_again_once_and_does_not_use_original_answer(secured):
    e = secured
    alice = e.login("alice")
    question = "Quelle pression?"
    first = alice.post("/api/ask", json={"question": question}).json()
    before = len(e.ollama.inputs)
    second = alice.post(
        "/api/ask", json={"question": question, "mode": "reflection"}
    ).json()
    assert first["grounded"] and second["grounded"]
    assert len(e.ollama.inputs) == before + 1 and len(e.ollama.chat_requests) == 2
    payload = e.ollama.chat_requests[-1]
    assert payload["think"] is True
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    data = json.loads(payload["messages"][-1]["content"])
    assert set(data) == {"QUESTION", "PASSAGES"} and data["QUESTION"] == question


def test_access_revoked_before_deepen_no_generation_or_stale_cache(secured):
    e = secured
    alice = e.login("alice")
    assert alice.post("/api/ask", json={"question": "Quelle pression?"}).json()[
        "grounded"
    ]
    e.admin.put(f"/api/admin/documents/{e.a}/groups", json={"groups": [e.gb]})
    result = alice.post(
        "/api/ask", json={"question": "Quelle pression?", "mode": "reflection"}
    ).json()
    assert not result["grounded"] and result["sources"] == []
    assert len(e.ollama.chat_requests) == 1


@pytest.mark.parametrize("mode", ["fast", "reflection", "search"])
def test_deadline_explicit_and_slot_released(env, monkeypatch, mode):
    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(env.kb, "retrieve", slow)
    env.config.ask_timeout_seconds = 0.01
    with pytest.raises(ResponseProblem, match="REQUEST_TIMEOUT"):
        run(env.kb.ask("Question trop lente?", mode=mode))
    assert env.kb.ollama._admitted == 0
    assert not env.ollama.chat_requests


def test_bounded_queue_timeout_cancellation_and_release():
    async def scenario():
        config = Settings(
            _env_file=None,
            ask_concurrency=1,
            ask_queue_size=1,
            ask_queue_timeout_seconds=0.1,
        )
        adapter = OllamaAdapter(config, lambda: None)

        async def queued():
            async with adapter.admission():
                return True

        async with adapter.admission():
            task = asyncio.create_task(queued())
            await asyncio.sleep(0)
            with pytest.raises(ResponseProblem, match="QUEUE_FULL"):
                await queued()
            with pytest.raises(ResponseProblem, match="QUEUE_TIMEOUT"):
                await task
            task = asyncio.create_task(queued())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert adapter._admitted == 0
        assert await queued()

    run(scenario())


def test_api_rejects_unsupported_and_timeout_without_generic_masking(
    secured, monkeypatch
):
    e = secured
    e.kb.ollama._capability = Capability(True, False, reason="Modèle sans réflexion.")
    modes = e.admin.get("/api/response-modes").json()["modes"]
    assert not modes["reflection"]["available"]
    response = e.admin.post(
        "/api/ask", json={"question": "Quelle pression?", "mode": "reflection"}
    )
    assert response.status_code == 422 and response.json()["code"] == "MODE_UNAVAILABLE"

    async def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("Private provider details")

    monkeypatch.setattr(e.kb, "retrieve", timeout)
    response = e.admin.post("/api/ask", json={"question": "Quelle pression?"})
    assert response.status_code == 504 and response.json()["code"] == "REQUEST_TIMEOUT"
    assert "Private" not in response.text


@pytest.mark.parametrize("mode", ["fast", "reflection"])
def test_timeout_during_actual_generation_releases_slot_without_retry(env, mode):
    run(env.kb.ingest())
    attempts = []

    async def transport(request):
        if request.url.path == "/api/chat":
            attempts.append(request)
            await asyncio.Event().wait()
        return env.ollama(request)

    original = env.kb._http
    env.kb._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    env.config.ask_timeout_seconds = 0.05
    try:
        with pytest.raises(ResponseProblem, match="REQUEST_TIMEOUT"):
            run(env.kb.ask("Où ranger les cartes?", mode=mode))
        assert len(attempts) == 1 and env.kb.ollama._admitted == 0
    finally:
        run(env.kb._http.aclose())
        env.kb._http = original


@pytest.mark.parametrize("mode", ["fast", "reflection"])
def test_ollama_rejection_disables_mode_and_never_falls_back(env, mode):
    run(env.kb.ingest())
    env.ollama.chat_status = 400
    with pytest.raises(ResponseProblem, match="MODEL_REJECTED_MODE"):
        run(env.kb.ask("Où ranger les cartes?", mode=mode))
    assert not run(env.kb.ollama.capabilities()).public()[mode]["available"]
    with pytest.raises(ResponseProblem, match="MODE_UNAVAILABLE"):
        run(env.kb.ask("Où ranger les cartes?", mode=mode))
    assert len(env.ollama.chat_requests) == 1


def test_missing_model_cannot_be_enabled_by_explicit_override():
    def transport(request):
        return (
            httpx.Response(404, json={"error": "not found"})
            if request.url.path == "/api/show"
            else httpx.Response(200, json={"version": "0.12.3"})
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            adapter = OllamaAdapter(
                Settings(_env_file=None, ollama_think_support="boolean"), lambda: http
            )
            capability = await adapter.capabilities()
            assert not capability.fast and not capability.reflection
            assert "introuvable" in capability.reason

    run(scenario())


def test_concurrent_mode_rejections_keep_both_modes_disabled(env):
    run(env.kb.ingest())
    from app.grounding import prepare_passages

    async def scenario():
        capability = await env.kb.ollama.capabilities()
        passages = prepare_passages(
            await env.kb.retrieve("cartes bleues"), "Question?", env.config
        )
        env.ollama.chat_status = 400
        results = await asyncio.gather(
            *(
                env.kb.ollama.generate("Question?", passages, mode, capability)
                for mode in ("fast", "reflection")
            ),
            return_exceptions=True,
        )
        assert all(isinstance(r, ResponseProblem) for r in results)
        updated = await env.kb.ollama.capabilities()
        assert not updated.fast and not updated.reflection

    run(scenario())
