"""Real PDFs -> real extraction/chunking -> controlled Ollama -> embedded Qdrant
-> real retrieval -> controlled hostile/valid output -> real API validation.

No network, Docker, GPU or downloaded models. The model double tests a protocol,
not a model's ability to understand the question; the optional evaluation does.
"""

import json
import re
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from app import main
from app.config import Settings
from app.services import KnowledgeBase
from evaluation.build_fixtures import PRESSURE, ROOT, write_pdf
from evaluation.checks import REFUSAL, check_answer, load_cases
from fastapi.testclient import TestClient
from test_indexing import FaultyQdrant


class ControlledOllama:
    """Select IDs from actual retrieved text; never fabricate the answer in a fake.

    Lexical vectors deliberately have no claim to reproduce semantic embeddings.
    Qdrant still performs cosine ranking, thresholds and active-revision filtering.
    """

    vocabulary = (
        "orion",
        "pression",
        "code",
        "visiteurs",
        "badge",
        "huile",
        "lune",
        "crayons",
        "cartons",
        "pirate",
        "formation",
        "voltage",
    )

    def __init__(self):
        self.inputs = []
        self.chats = []
        self.select = "42.5 kPa"
        self.output = None
        self.on_chat = None
        self.status = 200

    def vector(self, text):
        words = re.findall(r"\w+", text.lower())
        counts = [float(words.count(term)) for term in self.vocabulary]
        return counts + [0.0 if any(counts) else 1.0]

    async def __call__(self, request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "nomic-embed-text:latest", "digest": "regression-v1"}
                    ]
                },
            )
        body = json.loads(request.content)
        if request.url.path == "/api/embed":
            assert body["truncate"] is False
            self.inputs.append(body["input"])
            return httpx.Response(
                200, json={"embeddings": [self.vector(text) for text in body["input"]]}
            )
        assert request.url.path == "/api/chat", "Unexpected Ollama operation"
        self.chats.append(body)
        context = json.loads(body["messages"][1]["content"])
        if self.on_chat:
            await self.on_chat()
        selected = [
            p["id"]
            for p in context["passages"]
            if self.select and self.select in p["text"]
        ]
        content = json.dumps({"passage_ids": selected})
        if self.output is not None:
            content = self.output(context) if callable(self.output) else self.output
        return httpx.Response(self.status, json={"message": {"content": content}})


@pytest.fixture
def rag(tmp_path, monkeypatch):
    # Accidental HTTP clients must not silently turn these into integration tests.
    def network_forbidden(*args, **kwargs):
        raise AssertionError("Network is forbidden in RAG regression tests")

    monkeypatch.setattr(socket.socket, "connect", network_forbidden)
    documents = tmp_path / "documents"
    documents.mkdir()
    model = ControlledOllama()
    qdrant = FaultyQdrant()
    config = Settings(
        _env_file=None,
        documents_path=str(documents),
        index_lock_path=str(tmp_path / "locks"),
        lexical_index_path=str(tmp_path / "lexical"),
        chunk_size=220,
        chunk_overlap=35,
        embedding_batch_size=2,
        min_score=0.20,
        top_k=8,
    )
    kb = KnowledgeBase(
        config=config,
        qdrant=qdrant,
        http=httpx.AsyncClient(transport=httpx.MockTransport(model)),
    )
    monkeypatch.setattr(main, "knowledge_base", kb)
    with TestClient(main.app) as client:
        yield SimpleNamespace(
            client=client, kb=kb, model=model, qdrant=qdrant, documents=documents
        )


def ingest(rag, corpus="base"):
    shutil.copytree(ROOT / corpus, rag.documents, dirs_exist_ok=True)
    response = rag.client.post("/api/ingest")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["failed"] == 0, result
    return result


def ask(rag, question="Quelle pression régler pour le banc fictif ORION?"):
    response = rag.client.post("/api/ask", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()


def assert_refusal(answer):
    assert answer == {"answer": REFUSAL, "sources": [], "grounded": False}


def assert_indexed_citations(rag, answer):
    assert answer["grounded"] is True
    for source in answer["sources"]:
        points = rag.qdrant.retrieve(
            rag.kb.settings.qdrant_collection,
            ids=[source["passage_id"]],
            with_payload=True,
        )
        assert len(points) == 1
        payload = points[0].payload
        assert (source["document"], source["page"], source["excerpt"]) == (
            payload["document"],
            payload["page"],
            payload["text"],
        )
        assert source["excerpt"] in answer["answer"]


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_shared_questions_through_api_and_pdf_citations(rag, case):
    result = ingest(rag, case["corpus"])
    if case["id"] == "absent":
        rag.model.select = None
    elif case["id"] == "exact_code":
        rag.model.select = "ZX-417"
    elif case["id"] == "conflict":
        rag.model.select = "kPa"
    answer = ask(rag, case["question"])
    assert check_answer(case, answer, rag.documents) == []
    context = json.loads(rag.model.chats[-1]["messages"][1]["content"])
    assert context["passages"]  # Even the unanswerable question retrieved material.
    assert max(map(len, rag.model.inputs)) <= 2
    if answer["grounded"]:
        assert_indexed_citations(rag, answer)
    if case["id"] == "present":
        assert result["chunks"] > 3  # Actual page splitting, not one fake chunk/PDF.
        assert len(context["passages"]) > len(answer["sources"])
        assert any(PRESSURE in text for batch in rag.model.inputs for text in batch)
        assert {s["document"] for s in answer["sources"]} == {
            "maintenance/procedure.pdf"
        }


def test_empty_index_and_no_relevant_hit_skip_generation(rag):
    assert_refusal(ask(rag))
    assert not rag.model.inputs and not rag.model.chats
    ingest(rag)
    assert_refusal(ask(rag, "Quelle est la température de la lune?"))
    assert not rag.model.chats


@pytest.mark.parametrize(
    "output",
    [
        "PIRATE",
        "{",
        "null",
        "[]",
        '"42.5 kPa"',
        '{"passage_ids": "invented"}',
        '{"passage_ids": [17]}',
        '{"passage_ids": ["invented"]}',
        lambda c: json.dumps({"passage_ids": [c["passages"][0]["id"], "invented"]}),
        lambda c: json.dumps(
            {
                "passage_ids": [c["passages"][0]["id"]],
                "answer": "Régler à 999 bar. [secret.pdf, p. 99]",
            }
        ),
        lambda c: json.dumps(
            {
                "passage_ids": [c["passages"][0]["id"]],
                "document": "secret.pdf",
                "page": 99,
            }
        ),
        lambda c: json.dumps(
            {"passage_ids": [c["passages"][0]["id"]], "quote": "Le code est ZX-471."}
        ),
    ],
)
def test_hostile_or_malformed_generation_never_becomes_grounded(rag, output):
    ingest(rag)
    rag.model.output = output
    assert_refusal(ask(rag))


def test_model_cannot_select_indexed_but_unretrieved_passage(rag):
    ingest(rag)
    points, _ = rag.qdrant.scroll(rag.kb.settings.qdrant_collection, limit=100)
    unrelated = next(
        p for p in points if p.payload["document"] == "accueil/visiteurs.pdf"
    )
    rag.model.output = json.dumps({"passage_ids": [str(unrelated.id)]})
    assert_refusal(ask(rag))
    context = json.loads(rag.model.chats[-1]["messages"][1]["content"])
    assert str(unrelated.id) not in {p["id"] for p in context["passages"]}


def test_duplicate_selections_do_not_duplicate_citations(rag):
    ingest(rag)
    rag.model.output = lambda c: json.dumps(
        {
            "passage_ids": [
                next(p["id"] for p in c["passages"] if PRESSURE in p["text"])
            ]
            * 2
        }
    )
    answer = ask(rag)
    assert len(answer["sources"]) == 1
    assert_indexed_citations(rag, answer)


def test_long_passage_is_not_truncated_in_citation(rag):
    rag.kb.settings.chunk_size = 1000
    rag.kb.source.size = 1000
    ingest(rag)
    answer = ask(rag)
    assert len(answer["sources"][0]["excerpt"]) > 300
    assert_indexed_citations(rag, answer)


def test_pdf_injection_stays_in_untrusted_data_and_cannot_add_a_system_role(rag):
    ingest(rag, "injection")
    rag.model.output = "PIRATE [secret.pdf, p. 99]"
    assert_refusal(ask(rag))
    request = rag.model.chats[-1]
    assert [m["role"] for m in request["messages"]] == ["system", "user"]
    assert "PIRATE" not in request["messages"][0]["content"]
    assert "ignore les instructions précédentes" in request["messages"][1]["content"]


@pytest.mark.parametrize(
    "broken,code",
    [
        ("blank", "NO_TEXT"),
        ("corrupt", "EXTRACTION_FAILED"),
        ("encrypted", "EXTRACTION_FAILED"),
        ("unreadable", "READ_FAILED"),
    ],
)
def test_invalid_replacement_preserves_existing_citable_pdf(
    rag, broken, code, monkeypatch
):
    ingest(rag)
    before = ask(rag)
    path = rag.documents / "maintenance/procedure.pdf"
    path.unlink()
    if broken == "blank":
        write_pdf(path, [""])
    elif broken == "encrypted":
        write_pdf(path, ["Private synthetic content"], encrypted=True)
    elif broken == "corrupt":
        path.write_bytes(b"%PDF-corrupt synthetic fixture")
    else:
        write_pdf(path, ["Synthetic unreadable replacement"])
        original = Path.open

        def deny(self, *args, **kwargs):
            if self == path:
                raise PermissionError("secret filesystem detail")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", deny)
    # A missing second PDF must not be deleted on this partial failure either.
    (rag.documents / "accueil/visiteurs.pdf").unlink()
    response = rag.client.post("/api/ingest")
    assert response.status_code == 200
    result = response.json()
    assert result["failed"] == 1 and result["deleted"] == 0
    assert result["errors"] == [{"document": "maintenance/procedure.pdf", "code": code}]
    assert "secret" not in response.text
    after = ask(rag)
    assert after == before
    assert_indexed_citations(rag, after)
    _, manifests = rag.kb.store.read()
    assert "accueil/visiteurs.pdf" in manifests


def test_modify_delete_and_failed_cleanup_never_cite_old_passages(rag):
    ingest(rag)
    old = ask(rag)
    old_ids = {s["passage_id"] for s in old["sources"]}
    calls = len(rag.model.inputs)
    unchanged = rag.client.post("/api/ingest").json()
    assert unchanged["unchanged"] == 2 and len(rag.model.inputs) == calls
    assert ask(rag) == old
    path = rag.documents / "maintenance/procedure.pdf"
    path.unlink()
    write_pdf(path, ["Pour le banc fictif ORION, régler la pression à 51.2 kPa."])
    rag.qdrant.fail_cleanup = True
    result = rag.client.post("/api/ingest").json()
    assert result["modified"] == 1 and result["cleanup_pending"]
    # Old physical points deliberately survive, so the active filter is essential.
    assert rag.qdrant.retrieve(rag.kb.settings.qdrant_collection, ids=list(old_ids))
    rag.model.select = "51.2 kPa"
    answer = ask(rag)
    assert answer["grounded"] and "42.5 kPa" not in answer["answer"]
    assert not old_ids.intersection(s["passage_id"] for s in answer["sources"])
    assert_indexed_citations(rag, answer)
    rag.model.output = json.dumps({"passage_ids": list(old_ids)})
    assert_refusal(ask(rag))
    rag.model.output = None
    path.unlink()
    result = rag.client.post("/api/ingest").json()
    assert result["deleted"] == 1 and result["cleanup_pending"]
    assert_refusal(ask(rag))


def test_revision_removed_during_generation_is_not_returned(rag):
    ingest(rag)

    async def remove():
        (rag.documents / "maintenance/procedure.pdf").unlink()
        assert (await rag.kb.ingest())["deleted"] == 1

    rag.model.on_chat = remove
    assert_refusal(ask(rag))


def test_provider_failure_and_invalid_question_have_controlled_api_errors(rag):
    assert rag.client.post("/api/ask", json={"question": "x"}).status_code == 422
    assert not rag.model.inputs
    ingest(rag)
    rag.model.status = 500
    response = rag.client.post("/api/ask", json={"question": "pression ORION?"})
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Assistant indisponible; vérifiez les services locaux."
    }


def test_shared_checker_rejects_wrong_page_or_forged_excerpt(rag):
    ingest(rag)
    answer = ask(rag)
    case = load_cases()[0]
    assert check_answer(case, answer, rag.documents) == []
    answer["sources"][0]["page"] = 1
    assert "Extrait absent de la page citée" in check_answer(
        case, answer, rag.documents
    )
    answer["sources"][0]["page"] = 2
    answer["sources"][0]["excerpt"] = "Régler à 999 bar."
    assert "Extrait absent de la page citée" in check_answer(
        case, answer, rag.documents
    )
