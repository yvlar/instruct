"""Document lifecycle through real HTTP/PDF/Qdrant filters; no models or Docker."""

import asyncio
import hashlib
import time
from types import SimpleNamespace

import fitz
import httpx
import pytest
from app.config import Settings
from app.documents import DocumentManager
from app.indexing import document_id
from app.services import KnowledgeBase
from fastapi.testclient import TestClient
from test_indexing import FakeOllama, FaultyQdrant, indexed_texts


def pdf_bytes(text="Procedure fictive. Regler la machine DEMO-42 a 12 unites."):
    with fitz.open() as pdf:
        pdf.new_page().insert_text(
            (72, 72), "Document de demonstration, sans valeur operationnelle."
        )
        pdf.new_page().insert_text((72, 72), text)
        return pdf.tobytes()


@pytest.fixture
def api(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    root.mkdir()
    config = Settings(
        _env_file=None,
        documents_path=str(root),
        state_path=str(tmp_path / "security"),
        document_state_path=str(tmp_path / "state"),
        index_lock_path=str(tmp_path / "locks"),
        lexical_index_path=str(tmp_path / "lexical"),
        max_pdf_bytes=1024 * 1024,
    )
    qdrant = FaultyQdrant()
    # Worker clients share the controlled server, not ownership of its lifecycle.
    qdrant.close = lambda: None
    ollama = FakeOllama()

    def factory(**kwargs):
        return KnowledgeBase(
            config=config,
            qdrant=qdrant,
            http=httpx.AsyncClient(transport=httpx.MockTransport(ollama)),
        )

    kb = factory()
    from conftest import authenticated_app

    app, client = authenticated_app(kb)
    app.state.library.factory = factory
    import base64
    import json
    from itsdangerous import TimestampSigner

    token = json.loads(
        base64.b64decode(
            TimestampSigner(app.state.security.secret).unsign(
                client.cookies.get("instruct_session")
            )
        )
    )["token"]
    app.state.library.actor.set((1, token))
    with TestClient(app):
        yield SimpleNamespace(
            client=client,
            kb=kb,
            root=root,
            config=config,
            qdrant=qdrant,
            ollama=ollama,
            manager=app.state.library,
            app=app,
            factory=factory,
            token=token,
        )
    qdrant.client.close()


def upload(api, data=None, *, name="fiche.pdf", folder="maintenance", replace_id=None):
    return api.client.put(
        "/api/library/documents",
        params={
            "name": name,
            "folder": folder,
            **({"replace_id": replace_id} if replace_id else {}),
        },
        content=pdf_bytes() if data is None else data,
        headers={"Content-Type": "application/pdf"},
    )


def wait_job(api, response):
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = next(job for job in api.manager.state.jobs() if job["id"] == identifier)
        if job["state"] not in ("queued", "running"):
            return job
        time.sleep(0.01)
    pytest.fail("Document task did not finish")


def add_indexed(api):
    response = upload(api)
    assert response.status_code == 201, response.text
    identifier = response.json()["document_id"]
    job = wait_job(api, api.client.post(f"/api/library/documents/{identifier}/index"))
    assert job["state"] == "completed", job
    return identifier


def test_valid_upload_list_and_incremental_index(api):
    response = upload(api)
    assert response.status_code == 201
    row = api.client.get("/api/library/documents").json()["items"][0]
    assert row["state"] == "pending" and row["pages"] == 2
    assert row["folder"] == "maintenance" and row["size"] > 0
    job = wait_job(api, api.client.post(f"/api/library/documents/{row['id']}/index"))
    assert job["state"] == "completed" and job["processed"] == 1
    row = api.client.get("/api/library/documents").json()["items"][0]
    assert row["state"] == "available" and row["version"] == row["file_hash"]
    assert row["indexed_at"]
    calls = len(api.ollama.inputs)
    assert (
        wait_job(api, api.client.post("/api/library/documents/sync"))["result"][
            "unchanged"
        ]
        == 1
    )
    assert len(api.ollama.inputs) == calls


@pytest.mark.parametrize("data", [b"this is not PDF", b"%PDF-1.7\ntruncated"])
def test_fake_or_damaged_pdf_rejected(api, data):
    assert upload(api, data).status_code == 400
    assert not list(api.root.rglob("*.pdf"))
    assert not list((api.manager.state.root / "staging").iterdir())


def test_size_limit_content_length_and_chunked_stream(api):
    assert upload(api, b"x" * (api.config.max_pdf_bytes + 1)).status_code == 413
    chunks = iter([b"%PDF-1.7", b"x" * api.config.max_pdf_bytes])
    assert (
        api.client.put(
            "/api/library/documents?name=test.pdf",
            content=chunks,
            headers={"Content-Type": "application/pdf"},
        ).status_code
        == 413
    )
    assert not list((api.manager.state.root / "staging").iterdir())


@pytest.mark.parametrize(
    "name,folder",
    [
        ("../bad.pdf", ""),
        ("x.pdf", "../out"),
        ("x.pdf", "/tmp"),
        ("x.pdf", "a//b"),
        ("x.pdf", "a/./b"),
        ("C:\\bad.pdf", ""),
        ("x.txt", ""),
        ("x.pdf", "a\\b"),
        ("x.pdf", ".hidden"),
    ],
)
def test_forbidden_names_and_folders(api, name, folder):
    assert upload(api, name=name, folder=folder).status_code == 400


def test_symlink_directory_and_file_cannot_escape(api, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(pdf_bytes("PRIVATE"))
    (api.root / "escape").symlink_to(outside, target_is_directory=True)
    assert upload(api, folder="escape").status_code == 400
    assert not (outside / "fiche.pdf").exists()
    (api.root / "escape").unlink()
    (api.root / "fiche.pdf").symlink_to(outside / "secret.pdf")
    before = (outside / "secret.pdf").read_bytes()
    assert upload(api, folder="").status_code == 400
    assert (outside / "secret.pdf").read_bytes() == before


def test_collision_requires_explicit_replacement(api):
    assert upload(api).status_code == 201
    original = (api.root / "maintenance/fiche.pdf").read_bytes()
    assert upload(api, pdf_bytes("Other")).status_code == 409
    assert (api.root / "maintenance/fiche.pdf").read_bytes() == original
    assert upload(api, name="autre.pdf").status_code == 201


def test_replacement_preserves_old_until_success_and_old_source_still_opens(api):
    identifier = add_indexed(api)
    old = (api.root / "maintenance/fiche.pdf").read_bytes()
    version = hashlib.sha256(old).hexdigest()
    new = pdf_bytes("Nouvelle procedure fictive a 25 unites.")
    assert upload(api, new, replace_id=identifier).json()["replacement_pending"]
    assert (api.root / "maintenance/fiche.pdf").read_bytes() == old
    assert upload(api, new, replace_id=identifier).status_code == 409
    job = wait_job(api, api.client.post(f"/api/library/documents/{identifier}/index"))
    assert job["state"] == "completed", job
    assert (api.root / "maintenance/fiche.pdf").read_bytes() == new
    response = api.client.get(
        f"/api/library/documents/{identifier}/file",
        params={"version": version, "page": 2},
    )
    assert response.content == old
    assert "no-store" in response.headers["cache-control"]
    assert "inline" in response.headers["content-disposition"]
    download = api.client.get(
        f"/api/library/documents/{identifier}/file",
        params={"version": version, "download": True},
    )
    assert "attachment" in download.headers["content-disposition"]


@pytest.mark.parametrize("failure", ["embedding", "manifest", "lost_manifest_reply"])
def test_failed_replacement_keeps_previous_file_and_retries(api, failure):
    identifier = add_indexed(api)
    old = (api.root / "maintenance/fiche.pdf").read_bytes()
    old_hash = hashlib.sha256(old).hexdigest()
    assert (
        upload(api, pdf_bytes("New version"), replace_id=identifier).status_code == 201
    )
    api.ollama.fail = failure == "embedding"
    api.qdrant.fail_manifest = failure == "manifest"
    api.qdrant.manifest_reply_lost = failure == "lost_manifest_reply"
    job = wait_job(api, api.client.post(f"/api/library/documents/{identifier}/index"))
    assert job["state"] == "failed"
    assert (api.root / "maintenance/fiche.pdf").read_bytes() == old
    assert (
        api.client.get(
            f"/api/library/documents/{identifier}/file?version={old_hash}&page=2"
        ).content
        == old
    )
    assert (
        api.client.get("/api/library/documents").json()["items"][0]["state"] == "error"
    )
    api.ollama.fail = api.qdrant.fail_manifest = api.qdrant.manifest_reply_lost = False
    retried = wait_job(
        api, api.client.post(f"/api/library/document-jobs/{job['id']}/retry")
    )
    assert retried["state"] == "completed", retried
    assert (api.root / "maintenance/fiche.pdf").read_bytes() != old


def test_double_launch_blocks_index_upload_removal_and_legacy_ingestion(api):
    identifier = add_indexed(api)
    job = api.manager.submit("index", api.kb, identifier, launch=False)
    assert (
        api.client.post(f"/api/library/documents/{identifier}/index").status_code == 409
    )
    assert (
        api.client.post(f"/api/library/documents/{identifier}/remove").status_code
        == 409
    )
    assert api.client.post("/api/ingest").status_code == 409
    assert upload(api, name="second.pdf").status_code == 409
    api.manager.execute(job, api.token)
    assert api.manager.state.jobs()[0]["state"] == "completed"


def test_interrupted_task_recovered_and_retryable(api):
    identifier = add_indexed(api)
    queued = api.manager.submit("index", api.kb, identifier, launch=False)
    queued["state"] = "running"
    api.manager.state.save_job(queued)
    api.manager.close()
    manager = DocumentManager(api.config, api.factory)
    manager.start()
    api.app.state.library = api.manager = manager
    try:
        listing = api.client.get("/api/library/documents").json()
        assert listing["jobs"][0]["state"] == "interrupted"
        assert listing["items"][0]["state"] == "error"
        job = wait_job(
            api, api.client.post(f"/api/library/document-jobs/{queued['id']}/retry")
        )
        assert job["state"] == "completed"
    finally:
        manager.close()


def test_second_worker_cannot_mark_live_work_interrupted(api):
    manager = DocumentManager(api.config, api.factory)
    try:
        with pytest.raises(RuntimeError, match="un seul processus"):
            manager.start()
    finally:
        manager.close()


def test_removal_archive_then_sync_never_resurrects(api):
    identifier = add_indexed(api)
    original = (api.root / "maintenance/fiche.pdf").read_bytes()
    removed = wait_job(
        api, api.client.post(f"/api/library/documents/{identifier}/remove")
    )
    assert removed["state"] == "completed"
    assert not (api.root / "maintenance/fiche.pdf").exists()
    assert (
        list((api.manager.state.root / "archive").rglob("*.pdf"))[0].read_bytes()
        == original
    )
    # Even an external accidental copy back to the old path cannot revive it.
    (api.root / "maintenance/fiche.pdf").write_bytes(original)
    assert (
        wait_job(api, api.client.post("/api/library/documents/sync"))["state"]
        == "completed"
    )
    assert not api.client.post(
        "/api/ask", json={"question": "Quelle procedure?"}
    ).json()["grounded"]
    assert api.client.get("/api/library/documents").json()["total"] == 0
    assert indexed_texts(api, active=True) == []


def test_partial_removal_hides_source_and_retry_completes_cleanup(api, monkeypatch):
    identifier = add_indexed(api)
    original_delete = api.qdrant.delete

    def fail_delete(*args, **kwargs):
        raise RuntimeError("private database error")

    monkeypatch.setattr(api.qdrant, "delete", fail_delete)
    failed = wait_job(
        api, api.client.post(f"/api/library/documents/{identifier}/remove")
    )
    assert failed["state"] == "failed" and "private" not in failed["error"]
    assert not api.client.post(
        "/api/ask", json={"question": "Quelle procedure?"}
    ).json()["sources"]
    row = api.client.get("/api/library/documents").json()["items"][0]
    assert row["retired"] and row["state"] == "error"
    monkeypatch.setattr(api.qdrant, "delete", original_delete)
    assert (
        wait_job(
            api, api.client.post(f"/api/library/document-jobs/{failed['id']}/retry")
        )["state"]
        == "completed"
    )


def test_source_identity_version_and_page_checked(api):
    identifier = add_indexed(api)
    answer = api.client.post("/api/ask", json={"question": "Machine DEMO-42?"}).json()
    source = next(s for s in answer["sources"] if s["page"] == 2)
    assert source["document_id"] == identifier
    response = api.client.get(
        f"/api/library/documents/{identifier}/source",
        params={"version": source["version"], "page": 2},
    )
    assert response.status_code == 200 and response.json()["page"] == 2
    # Same basename and even same content cannot substitute a different document ID.
    other_id = upload(api, folder="formation").json()["document_id"]
    assert (
        api.client.get(
            f"/api/library/documents/{other_id}/file?version={source['version']}"
        ).status_code
        == 410
    )
    assert (
        api.client.get(
            f"/api/library/documents/{identifier}/file?version={'a' * 64}"
        ).status_code
        == 410
    )
    assert (
        api.client.get(
            f"/api/library/documents/{identifier}/file?version={source['version']}&page=9"
        ).status_code
        == 400
    )
    assert (
        api.client.get(
            f"/api/library/documents/{identifier}/file?version=../../secret"
        ).status_code
        == 404
    )
    snapshot = api.manager.state.version_path(identifier, source["version"])
    snapshot.unlink()
    assert (
        api.client.get(
            f"/api/library/documents/{identifier}/source?version={source['version']}"
        ).status_code
        == 410
    )


def test_external_modification_detection_and_filters_pagination(api):
    add_indexed(api)
    (api.root / "maintenance/fiche.pdf").write_bytes(pdf_bytes("Changed outside UI"))
    row = api.client.get("/api/library/documents?status=modified&q=FICHE").json()[
        "items"
    ][0]
    assert row["state"] == "modified" and row["file_hash"] != row["version"]
    assert upload(api, name="autre.pdf").status_code == 201
    listing = api.client.get("/api/library/documents?page_size=1&page=2").json()
    assert listing["total"] == 2 and len(listing["items"]) == 1
    assert (
        api.client.get("/api/library/documents?status=available").json()["total"] == 0
    )


def test_retirement_during_generation_invalidates_answer(api):
    identifier = add_indexed(api)

    async def scenario():
        original = api.ollama

        def retire_on_chat(request):
            if request.url.path == "/api/chat":
                api.manager.state.record("maintenance/fiche.pdf", removed=1)
            return original(request)

        kb = KnowledgeBase(
            config=api.config,
            qdrant=api.qdrant,
            http=httpx.AsyncClient(transport=httpx.MockTransport(retire_on_chat)),
        )
        try:
            answer = await kb.ask("Quelle procedure?")
            assert answer["grounded"] is False and not answer["sources"]
        finally:
            await kb.close()

    asyncio.run(scenario())
    assert document_id("maintenance/fiche.pdf") == identifier


def test_cross_origin_writes_are_rejected(api):
    assert (
        api.client.post(
            "/api/library/documents/sync",
            headers={"Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )


def test_state_archive_cannot_be_inside_indexed_tree(api):
    config = api.config.model_copy(
        update={"document_state_path": str(api.root / "state")}
    )
    with pytest.raises(ValueError, match="outside"):
        DocumentManager(config, api.factory)


def test_interrupted_http_upload_never_replaces_previous_document(api):
    from starlette.requests import ClientDisconnect, Request

    identifier = add_indexed(api)
    original = (api.root / "maintenance/fiche.pdf").read_bytes()

    async def scenario():
        incoming = iter(
            [
                {
                    "type": "http.request",
                    "body": b"%PDF-1.7 partial upload",
                    "more_body": True,
                },
                {"type": "http.disconnect"},
            ]
        )

        async def receive():
            return next(incoming)

        request = Request(
            {
                "type": "http",
                "method": "PUT",
                "path": "/api/library/documents",
                "app": api.app,
                "headers": [(b"content-type", b"application/pdf")],
            },
            receive,
        )
        with pytest.raises(ClientDisconnect):
            await api.app.state.library_upload(
                request, name="fiche.pdf", folder="maintenance", replace_id=identifier
            )

    asyncio.run(scenario())
    assert (api.root / "maintenance/fiche.pdf").read_bytes() == original
    assert not list((api.manager.state.root / "staging").iterdir())
    assert api.manager.state.records()["maintenance/fiche.pdf"]["pending"] is None


def test_retirement_cleans_lexical_index_and_retries_partial_failure(api, monkeypatch):
    from app.lexical import LexicalIndex

    identifier = add_indexed(api)
    with api.kb.lexical.connect() as db:
        assert db.execute("SELECT count(*) FROM passages").fetchone()[0] > 0
    original = LexicalIndex.collect_garbage

    def fail_cleanup(self, manifests):
        raise OSError("private lexical failure")

    monkeypatch.setattr(LexicalIndex, "collect_garbage", fail_cleanup)
    job = wait_job(api, api.client.post(f"/api/library/documents/{identifier}/remove"))
    assert job["state"] == "failed"
    assert api.manager.state.records()["maintenance/fiche.pdf"]["removed"] == 1
    answer = api.client.post("/api/ask", json={"question": "DEMO-42 12 unites?"}).json()
    assert not answer["grounded"] and answer["sources"] == [] and answer["claims"] == []
    monkeypatch.setattr(LexicalIndex, "collect_garbage", original)
    retried = wait_job(
        api, api.client.post(f"/api/library/document-jobs/{job['id']}/retry")
    )
    assert retried["state"] == "completed"
    with api.kb.lexical.connect() as db:
        assert db.execute("SELECT count(*) FROM passages").fetchone()[0] == 0


def test_verified_claims_keep_exact_document_version(api):
    identifier = add_indexed(api)
    answer = api.client.post("/api/ask", json={"question": "DEMO-42 12 unites?"}).json()
    assert answer["grounded"] and answer["claims"] and answer["safety_notice"]
    cited = {
        source_id for claim in answer["claims"] for source_id in claim["source_ids"]
    }
    assert cited == {source["source_id"] for source in answer["sources"]}
    for source in answer["sources"]:
        assert source["document_id"] == identifier
        assert source["revision"] and source["fingerprint"] and source["passage_id"]
        response = api.client.get(
            f"/api/library/documents/{identifier}/file",
            params={"version": source["version"], "page": source["page"]},
        )
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == source["version"]


def test_retirement_repairs_missing_lexical_sidecar(api):
    identifier = add_indexed(api)
    api.kb.lexical.path.unlink()
    job = wait_job(api, api.client.post(f"/api/library/documents/{identifier}/remove"))
    assert job["state"] == "completed"
    with api.kb.lexical.connect() as db:
        assert db.execute("SELECT count(*) FROM passages").fetchone()[0] == 0
