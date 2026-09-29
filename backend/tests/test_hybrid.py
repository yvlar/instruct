"""Real SQLite/PDF/embedded Qdrant, controlled embeddings and generation, no GPU."""

import json
import sqlite3
from dataclasses import replace

import fitz
import httpx
import pytest
from app.answering import REFUSAL, render_answer, selected_hits
from app.chunking import chunk_text
from app.indexing import CONTROL_ID, IndexErrorBase, PdfSource
from app.lexical import LexicalIndex, exact_match
from app.retrieval import Passage, fuse, select_context
from qdrant_client import models
from test_indexing import Interrupted, run


def p(identifier="p1", text="Couper l'alimentation.", **kwargs):
    return Passage(
        identifier, "maintenance/presse.pdf", "rev1", "fp1", 2, text, **kwargs
    )


def lexical_hits(env, question):
    _, manifests = env.kb.store.read()
    return env.kb.lexical.search(question, manifests, 24)


def test_paraphrase_uses_semantic_path_without_lexical_overlap(env, monkeypatch):
    env.files.files = {"guide.pdf": ["Actionner le sectionneur.", "Ranger les outils."]}
    run(env.kb.ingest())
    points = env.qdrant.scroll(env.config.qdrant_collection, limit=10)[0]
    target = next(pt for pt in points if "sectionneur" in pt.payload["text"])
    monkeypatch.setattr(
        env.qdrant,
        "query_points",
        lambda *args, **kwargs: type("Results", (), {"points": [target]})(),
    )
    question = "Comment couper toute alimentation électrique?"
    assert lexical_hits(env, question) == []
    hits = run(env.kb.retrieve(question))
    assert hits[0].id == target.id
    assert run(env.kb.ask(question))["grounded"] is True


def test_exact_code_survives_missing_semantic_candidate(env, monkeypatch):
    env.files.files = {
        "piece.pdf": ["Remplacer le joint AB-204/X de la presse Orion."],
        "voisin.pdf": ["Remplacer le joint AB-205/X de la presse Orion."],
    }
    run(env.kb.ingest())
    points = env.qdrant.scroll(env.config.qdrant_collection, limit=10)[0]
    neighbour = next(pt for pt in points if "205" in pt.payload["text"])
    monkeypatch.setattr(
        env.qdrant,
        "query_points",
        lambda *args, **kwargs: type("Results", (), {"points": [neighbour]})(),
    )
    env.config.top_k = 1
    hits = run(env.kb.retrieve("Quel joint AB-204/X faut-il remplacer?"))
    assert hits[0].document == "piece.pdf"
    assert (
        run(env.kb.ask("Quel joint AB-204/X faut-il remplacer?"))["sources"][0][
            "document"
        ]
        == "piece.pdf"
    )


@pytest.mark.parametrize(
    "query,text,neighbour",
    [
        ("12,5 bar", "Régler à 12.5 bar.", "Régler à 12,6 bar."),
        ("12.5 bar", "Régler à 12,5 bar.", "Régler à 12,5 kPa."),
        ("-5 °C", "Conserver à -5 °C.", "Conserver à 5 °C."),
        ("0,25 mm", "Écart : 0.25 mm.", "Écart : 10.25 mm."),
        ("20 N·m", "Serrer à 20 N.m.", "Serrer à 200 N.m."),
        ("12ABC", "Pièce 12ABC.", "Pièce 12ABD."),
        ("AB.204/9", "Pièce AB.204/9.", "Pièce CD.204/9."),
        ("12 bar", "Régler à 12bar.", "Régler à 12kPa."),
        (
            "arrêt d’urgence",
            "Actionner l'arrêt d'urgence.",
            "Actionner le bouton normal.",
        ),
    ],
)
def test_exact_tokens_and_units_are_not_confused(env, query, text, neighbour):
    env.files.files = {"exact.pdf": [text], "voisin.pdf": [neighbour]}
    run(env.kb.ingest())
    assert exact_match(query, text)
    assert not exact_match(query, neighbour)
    assert lexical_hits(env, query)[0]["document"] == "exact.pdf"
    assert run(env.kb.retrieve(query))[0].document == "exact.pdf"


def test_rank_fusion_deduplicates_by_id_and_keeps_citation_identity():
    one = p()
    two = replace(one, id="p2", text="Vérifier le voyant.")
    hits = fuse("procédure", [one, two], [one, one])
    assert len(hits) == 2 and hits[0].id == "p1"
    assert hits[0].score == pytest.approx(2 / 61)
    assert (hits[0].document, hits[0].page, hits[0].revision, hits[0].fingerprint) == (
        one.document,
        2,
        "rev1",
        "fp1",
    )
    assert fuse("procédure", [one, two], [two, one]) == fuse(
        "procédure", [one, two], [two, one]
    )
    assert len(fuse("procédure", [one], [replace(one, id="duplicate")])) == 1


def test_pdf_headings_numbered_steps_and_pages_are_preserved(tmp_path):
    path = tmp_path / "guide.pdf"
    with fitz.open() as pdf:
        first = pdf.new_page()
        first.insert_text((72, 72), "MAINTENANCE ORION", fontsize=16)
        first.insert_text(
            (72, 112), "1. Couper l'alimentation.\nAttendre le voyant vert."
        )
        first.insert_text(
            (72, 168), "2. Poser le joint AB-204/X.\nVerifier le serrage."
        )
        second = pdf.new_page()
        second.insert_text((72, 72), "CONTROLE FINAL", fontsize=16)
        second.insert_text((72, 112), "3. Regler a 12,5 bar.")
        pdf.save(path)
    source = PdfSource(str(tmp_path), 160, 20)
    passages = list(source.passages("guide.pdf", source.fingerprint("guide.pdf")))
    assert len(passages) == 3
    assert passages[0] == (
        1,
        "MAINTENANCE ORION\n\n1. Couper l'alimentation.\nAttendre le voyant vert.",
    )
    assert passages[1][0] == 1 and "MAINTENANCE ORION\n\n2." in passages[1][1]
    assert passages[2] == (2, "CONTROLE FINAL\n\n3. Regler a 12,5 bar.")
    assert all(len(text) <= 160 for _, text in passages)


def test_chunking_never_loses_short_headings_or_long_steps():
    text = (
        "TITRE\n\nSOUS TITRE\n\n1. " + "Important. " * 50 + "\n\n2. Terminer.\n\nANNEXE"
    )
    chunks = chunk_text(text, 120, 20)
    joined = "\n".join(chunks)
    assert all(
        term in joined
        for term in ["TITRE", "SOUS TITRE", "1.", "2. Terminer.", "ANNEXE"]
    )
    assert all(len(c) <= 120 for c in chunks)
    assert not any("1." in c and "2. Terminer." in c for c in chunks)


def test_context_budget_is_hard_and_never_truncates_an_exact_step():
    one, two = p(), p("p2", "Une autre étape.")
    cost = len(one.context()) + 2
    assert select_context([one, two], max_passages=4, max_chars=cost) == [one]
    assert select_context([one, two], max_passages=4, max_chars=cost - 1) == []
    assert select_context([one, two], max_passages=1, max_chars=8000) == [one]


def test_modification_deletion_and_unchanged_run_sync_both_indexes(env):
    env.files.files = {"guide.pdf": ["Joint ZX-101."], "keep.pdf": ["Vis BC-303."]}
    run(env.kb.ingest())
    old = lexical_hits(env, "ZX-101")[0]
    env.files.files["guide.pdf"] = ["Joint ZX-202."]
    assert run(env.kb.ingest())["modified"] == 1
    new = lexical_hits(env, "ZX-202")[0]
    assert new["revision"] != old["revision"] and new["id"] != old["id"]
    assert lexical_hits(env, "ZX-101") == []
    assert run(env.kb.ingest())["unchanged"] == 2
    with env.kb.lexical.connect() as db:
        assert db.execute("SELECT count(*) FROM passages").fetchone()[0] == 2
    del env.files.files["guide.pdf"]
    assert run(env.kb.ingest())["deleted"] == 1
    assert lexical_hits(env, "ZX-202") == []
    with env.kb.lexical.connect() as db:
        assert db.execute("SELECT count(*) FROM passages").fetchone()[0] == 1


def test_lexical_write_failure_cannot_publish_a_partial_revision(env, monkeypatch):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Nouvelle procédure."]

    def fail(*args):
        raise sqlite3.OperationalError("private database details")

    monkeypatch.setattr(LexicalIndex, "upsert", fail)
    result = run(env.kb.ingest())
    assert result["errors"][0]["code"] == "LEXICAL_WRITE_FAILED"
    assert lexical_hits(env, "ancienne")[0]["text"] == "Ancienne procédure."
    assert not lexical_hits(env, "nouvelle")


@pytest.mark.parametrize("ambiguous_reply", [False, True])
def test_published_revision_is_lexically_complete_after_interruption(
    env, ambiguous_reply
):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Nouveau joint XR-202."]
    env.qdrant.interrupt_after_manifest = not ambiguous_reply
    env.qdrant.manifest_reply_lost = ambiguous_reply
    if ambiguous_reply:
        assert run(env.kb.ingest())["failed"] == 1
    else:
        with pytest.raises(Interrupted):
            run(env.kb.ingest())
    assert lexical_hits(env, "XR-202")[0]["text"] == "Nouveau joint XR-202."
    assert lexical_hits(env, "ancienne") == []


def test_missing_sidecar_fails_explicitly_then_repairs_without_embeddings(env):
    run(env.kb.ingest())
    inputs = len(env.ollama.inputs)
    env.kb.lexical.path.unlink()
    with pytest.raises(IndexErrorBase, match="LEXICAL_INDEX_INCOMPLETE"):
        run(env.kb.ask("Procédure?"))
    assert run(env.kb.ingest())["unchanged"] == 1
    assert len(env.ollama.inputs) == inputs
    assert lexical_hits(env, "ancienne")


def test_old_schema_requires_explicit_reconstruction(env):
    run(env.kb.ingest())
    control, _ = env.kb.store.read()
    control["schema"] = 2
    env.qdrant.upsert(
        env.kb.store.manifest_collection,
        points=[models.PointStruct(id=CONTROL_ID, vector={}, payload=control)],
    )
    for operation in [env.kb.ingest(), env.kb.ask("Procédure?")]:
        with pytest.raises(IndexErrorBase, match="INDEX_INCOMPATIBLE"):
            run(operation)


def test_no_relevant_candidates_refuses_without_chat(env):
    env.files.files = {"guide.pdf": ["Vérifier le voyant."]}
    run(env.kb.ingest())
    env.config.min_score = (
        1.0  # Explicit empty semantic result for an unrelated question.
    )

    # Force zero similarity instead of relying on numerical cosine threshold details.
    async def orthogonal(_):
        return [0.5, -1.0, 0.0]

    env.kb.embed = orthogonal
    answer = run(env.kb.ask("Température du four Neptune?"))
    assert answer == {"answer": REFUSAL, "grounded": False, "sources": []}
    assert env.ollama.chat_requests == []


def test_rank_alone_does_not_make_a_generated_refusal_grounded(env):
    run(env.kb.ingest())

    def handler(request):
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps({"passage_ids": []})}},
            )
        return env.ollama(request)

    env.kb._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert run(env.kb.ask("Quelle procédure pour Mars?"))["grounded"] is False
    finally:
        run(env.kb._http.aclose())


@pytest.mark.parametrize(
    "content",
    [
        "plain unstructured answer",
        '{"answer":"Régler à 999 bar.","citations":[]}',
        json.dumps(
            {
                "answer": "Réponse",
                "citations": [{"passage_id": "invented", "quote": "Couper"}],
            }
        ),
        json.dumps(
            {
                "answer": "Réponse",
                "citations": [{"passage_id": "p1", "quote": "Inventé"}],
            }
        ),
    ],
)
def test_invalid_citations_fail_closed(content):
    assert selected_hits(content, [p()]) == []


def test_only_valid_used_passages_are_sources():
    passages = [p(), p("unused", "Inutile.")]
    content = json.dumps({"passage_ids": ["p1"]})
    result = render_answer(selected_hits(content, passages), passages)
    assert result["grounded"] is True
    assert [s["passage_id"] for s in result["sources"]] == ["p1"]
    assert result["sources"][0]["revision"] == "rev1"
    assert "Couper l'alimentation." in result["answer"]


def test_user_query_cannot_inject_fts_syntax(env):
    run(env.kb.ingest())
    for query in ['" OR * NOT (', 'NEAR(ancienne, procedure) "', "a de le"]:
        assert isinstance(lexical_hits(env, query), list)


def test_real_pdf_api_returns_verifiable_passage_metadata(env, tmp_path, monkeypatch):
    from app import main
    from fastapi.testclient import TestClient

    root = tmp_path / "pdfs"
    root.mkdir()
    path = root / "orion.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "PRESSE ORION\n\n1. Poser le joint AB-204/X.")
        pdf.save(path)
    env.kb.source = PdfSource(str(root), 1400, 250)
    monkeypatch.setattr(main, "knowledge_base", env.kb)
    with TestClient(main.app) as client:
        assert client.post("/api/ingest").json()["added"] == 1
        response = client.post("/api/ask", json={"question": "Ou poser AB-204/X?"})
        assert response.status_code == 200
        answer = response.json()
        assert answer["grounded"] is True
        source = answer["sources"][0]
        point = env.qdrant.retrieve(
            env.config.qdrant_collection, ids=[source["passage_id"]]
        )[0]
        assert source["excerpt"] in point.payload["text"]
        assert source["document"] == "orion.pdf" and source["page"] == 1
        assert source["revision"] == point.payload["revision"]
        assert source["fingerprint"] == point.payload["fingerprint"]
        assert len(env.ollama.chat_requests) == 1


def test_incomplete_lexical_revision_rebuilt_in_bounded_batches(env):
    env.files.files = {"guide.pdf": [f"Instruction {i}." for i in range(260)]}
    run(env.kb.ingest())
    inputs = len(env.ollama.inputs)
    with env.kb.lexical.connect() as db:
        db.execute("DELETE FROM passages WHERE page=150")
    with pytest.raises(IndexErrorBase, match="LEXICAL_INDEX_INCOMPLETE"):
        run(env.kb.ask("Instruction?"))
    assert run(env.kb.ingest())["unchanged"] == 1
    assert len(env.ollama.inputs) == inputs
    with env.kb.lexical.connect() as db:
        assert db.execute("SELECT count(*) FROM passages").fetchone()[0] == 260
