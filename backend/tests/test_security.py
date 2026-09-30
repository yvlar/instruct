"""Exercise real HTTP auth, SQLite, PDF extraction and Qdrant filtering, no GPU."""

import asyncio
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import fitz
import httpx
import pytest
from app.backup import backup, restore
from app.config import Settings
from app.indexing import document_id
from app.main import create_app
from app.security import maintenance_lock
from app.services import KnowledgeBase
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from test_indexing import FakeOllama, FaultyQdrant

PASSWORD = "Synthetic-password-123"


def pdf_bytes(text):
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), text)
        return pdf.tobytes()


def sign_in(app, name="admin", password=PASSWORD):
    client = TestClient(app, base_url=app.state.security.config.app_origin)
    client.headers["origin"] = app.state.security.config.app_origin
    client.headers["x-csrf-token"] = client.get("/api/auth/session").json()["csrf"]
    response = client.post(
        "/api/auth/login", json={"username": name, "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf"]
    return client


@pytest.fixture
def secured(tmp_path):
    config = Settings(
        _env_file=None,
        state_path=str(tmp_path / "state"),
        documents_path=str(tmp_path / "documents"),
        index_lock_path=str(tmp_path / "locks"),
    )
    Path(config.documents_path).mkdir()
    ollama = FakeOllama()
    http = httpx.AsyncClient(transport=httpx.MockTransport(ollama))
    kb = KnowledgeBase(config=config, qdrant=FaultyQdrant(), http=http)
    app = create_app(config, kb=kb)
    store = app.state.security
    admin_user = store.create_user("admin", PASSWORD, "admin", bootstrap=True)
    admin = sign_in(app)
    ga = admin.post("/api/admin/groups", json={"name": "Maintenance"}).json()["id"]
    gb = admin.post("/api/admin/groups", json={"name": "Production"}).json()["id"]
    alice = admin.post(
        "/api/admin/users",
        json={"username": "alice", "password": PASSWORD, "groups": [ga]},
    ).json()
    bob = admin.post(
        "/api/admin/users",
        json={"username": "bobby", "password": PASSWORD, "groups": [gb]},
    ).json()
    manager = admin.post(
        "/api/admin/users",
        json={
            "username": "manager",
            "password": PASSWORD,
            "role": "manager",
            "groups": [ga],
        },
    ).json()
    for path, group, text in (
        ("a/guide.pdf", ga, "ALPHA pressure 42 kPa."),
        ("b/guide.pdf", gb, "BETA confidential valve 99 kPa."),
    ):
        response = admin.post(
            "/api/documents",
            data={"path": path, "groups": str(group)},
            files={"file": ("guide.pdf", pdf_bytes(text), "application/pdf")},
        )
        assert response.status_code == 201, response.text
    assert admin.post("/api/ingest").json()["added"] == 2
    clients = [admin]

    def login(name):
        client = sign_in(app, name)
        clients.append(client)
        return client

    yield SimpleNamespace(
        app=app,
        config=config,
        kb=kb,
        ollama=ollama,
        store=store,
        admin=admin,
        admin_user=admin_user,
        alice=alice,
        bob=bob,
        manager=manager,
        ga=ga,
        gb=gb,
        login=login,
        a=document_id("a/guide.pdf"),
        b=document_id("b/guide.pdf"),
        tmp=tmp_path,
    )
    for client in clients:
        client.close()
    asyncio.run(kb.close())


def test_two_users_are_isolated_before_generation_and_at_file_routes(secured):
    e = secured
    for name, own, other, marker, forbidden in (
        ("alice", e.a, e.b, "ALPHA", "BETA"),
        ("bobby", e.b, e.a, "BETA", "ALPHA"),
    ):
        client = e.login(name)
        docs = client.get("/api/documents").json()
        assert [d["id"] for d in docs] == [own]
        # Browser-supplied group IDs are not an authority.
        response = client.post(
            "/api/ask?groups=999",
            json={"question": "Quelle pression?", "groups": [e.ga, e.gb]},
        )
        assert response.status_code == 200
        source = response.json()["sources"][0]
        assert source["document_id"] == own and source["page"] == 1
        assert marker in source["excerpt"] and forbidden not in response.text
        prompt = e.ollama.chat_requests[-1]["messages"][1]["content"]
        assert marker in prompt and forbidden not in prompt
        response = client.get(source["url"])
        assert (
            response.content.startswith(b"%PDF")
            and "no-store" in response.headers["cache-control"]
        )
        for suffix in ("file", "file?download=true", "versions", "file?version=known"):
            assert client.get(f"/api/documents/{other}/{suffix}").status_code == 404
        assert client.get("/api/admin/audit").status_code == 403


def test_default_deny_for_imported_pdf_and_fresh_reader(secured):
    e = secured
    (Path(e.config.documents_path) / "unassigned.pdf").write_bytes(
        pdf_bytes("UNASSIGNED PRIVATE")
    )
    assert e.admin.post("/api/ingest").status_code == 200
    client = e.login("alice")
    assert "unassigned" not in client.get("/api/documents").text
    assert (
        client.get(f"/api/documents/{document_id('unassigned.pdf')}/file").status_code
        == 404
    )
    e.admin.post("/api/admin/users", json={"username": "nogroup", "password": PASSWORD})
    empty = e.login("nogroup")
    assert empty.get("/api/documents").json() == []
    before = len(e.ollama.chat_requests)
    assert (
        empty.post("/api/ask", json={"question": "Une question?"}).json()["grounded"]
        is False
    )
    assert len(e.ollama.chat_requests) == before


def test_expiration_logout_disable_reset_and_password_change_revoke_sessions(secured):
    e = secured
    alice = e.login("alice")
    old_cookie = alice.cookies.get("instruct_session")
    assert alice.post("/api/auth/logout").status_code == 200
    alice.cookies.set("instruct_session", old_cookie)
    assert alice.get("/api/documents").status_code == 401
    alice = e.login("alice")
    with e.store.transaction() as db:
        db.execute("UPDATE sessions SET expires=0 WHERE user_id=?", (e.alice["id"],))
    assert alice.get("/api/documents").status_code == 401
    alice = e.login("alice")
    assert (
        e.admin.put(
            f"/api/admin/users/{e.alice['id']}",
            json={"role": "reader", "active": False, "groups": [e.ga]},
        ).status_code
        == 200
    )
    assert alice.get("/api/documents").status_code == 401
    assert (
        alice.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        ).status_code
        == 401
    )
    bob = e.login("bobby")
    assert (
        e.admin.post(
            f"/api/admin/users/{e.bob['id']}/password",
            json={"password": PASSWORD + "new"},
        ).status_code
        == 200
    )
    assert bob.get("/api/documents").status_code == 401
    manager = e.login("manager")
    assert (
        manager.post(
            "/api/auth/password",
            json={"current": PASSWORD, "password": PASSWORD + "new"},
        ).status_code
        == 200
    )
    assert manager.get("/api/documents").status_code == 401


def test_csrf_cookies_login_limit_and_no_passwords_in_audit(secured):
    e = secured
    client = TestClient(e.app)
    response = client.get("/api/auth/session")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    creds = {"username": "alice", "password": "never-log-this-password"}
    assert client.post("/api/auth/login", json=creds).status_code == 403
    client.headers.update(
        {"origin": "https://evil.example", "x-csrf-token": response.json()["csrf"]}
    )
    assert client.post("/api/auth/login", json=creds).status_code == 403
    client.headers["origin"] = e.config.app_origin
    for _ in range(e.config.login_attempts):
        assert client.post("/api/auth/login", json=creds).status_code == 401
    assert client.post("/api/auth/login", json=creds).status_code == 429
    audit = e.admin.get("/api/admin/audit?limit=100").json()
    assert "never-log-this-password" not in json.dumps(
        audit
    ) and PASSWORD not in json.dumps(audit)
    assert {i["result"] for i in audit["items"] if i["action"] == "login"} >= {
        "success",
        "failure",
        "rate_limited",
    }
    assert all("+00:00" in i["at"] for i in audit["items"])
    client.close()


def test_roles_manager_scope_and_last_admin(secured):
    e = secured
    reader = e.login("alice")
    manager = e.login("manager")
    assert reader.post(f"/api/documents/{e.a}/sync").status_code == 403
    assert reader.delete(f"/api/documents/{e.a}").status_code == 403
    assert manager.post("/api/ingest").status_code == 403
    assert manager.post(f"/api/documents/{e.b}/sync").status_code == 404
    assert manager.post(f"/api/documents/{e.a}/sync").json()["documents"] == 1
    assert (
        manager.post("/api/admin/groups", json={"name": "Forbidden"}).status_code == 403
    )
    assert (
        e.admin.put(
            f"/api/admin/users/{e.admin_user['id']}",
            json={"role": "reader", "active": False, "groups": []},
        ).status_code
        == 409
    )
    response = manager.post(
        "/api/documents",
        data={"path": "forbidden.pdf", "groups": str(e.gb)},
        files={"file": ("guide.pdf", pdf_bytes("SECRET"))},
    )
    assert response.status_code == 403
    assert not (Path(e.config.documents_path) / "forbidden.pdf").exists()
    for path in (
        "../escape.pdf",
        "/etc/escape.pdf",
        "a/../escape.pdf",
        "a\\escape.pdf",
    ):
        assert (
            e.admin.post(
                "/api/documents",
                data={"path": path, "groups": str(e.ga)},
                files={"file": ("guide.pdf", pdf_bytes("Text"))},
            ).status_code
            == 422
        )


@pytest.mark.parametrize("mode", ["fast", "reflection"])
def test_revocation_invalidates_old_citations_and_drops_inflight_answer(secured, mode):
    e = secured
    client = e.login("alice")
    response = client.post(
        "/api/ask", json={"question": "Quelle pression?", "mode": mode}
    ).json()
    url = response["sources"][0]["url"]
    access_before = client.get("/api/auth/session").json()["access_version"]
    assert (
        e.admin.put(
            f"/api/admin/documents/{e.a}/groups", json={"groups": []}
        ).status_code
        == 200
    )
    assert client.get("/api/auth/session").json()["access_version"] > access_before
    assert client.get(url).status_code == 404
    assert client.get(f"/api/documents/{e.a}/versions").status_code == 404
    assert not client.post(
        "/api/ask", json={"question": "Quelle pression?", "mode": mode}
    ).json()["grounded"]
    assert (
        e.admin.put(
            f"/api/admin/documents/{e.a}/groups", json={"groups": [e.ga]}
        ).status_code
        == 200
    )
    original = e.ollama.__call__

    def revoke(request):
        response = original(request)
        if request.url.path == "/api/chat":
            with e.store.transaction() as db:
                db.execute("DELETE FROM document_groups WHERE document_id=?", (e.a,))
        return response

    e.kb._http = httpx.AsyncClient(transport=httpx.MockTransport(revoke))
    result = client.post(
        "/api/ask", json={"question": "Quelle pression?", "mode": mode}
    ).json()
    assert (
        not result["grounded"]
        and result["sources"] == []
        and "sourcée" not in result["answer"]
    )


@pytest.mark.parametrize("mode", ["fast", "reflection", "search"])
def test_revoke_during_embedding_never_sends_context(secured, mode):
    e = secured
    client = e.login("alice")

    def revoke():
        with e.store.transaction() as db:
            db.execute("DELETE FROM document_groups WHERE document_id=?", (e.a,))

    e.ollama.on_embed = revoke
    assert not client.post(
        "/api/ask", json={"question": "Quelle pression?", "mode": mode}
    ).json()["grounded"]
    assert not e.ollama.chat_requests


def test_backup_restore_real_pdfs_sqlite_vectors_login_rights_and_citations(secured):
    e = secured
    client = e.login("alice")
    # Preserve two versions, then index the latest.
    assert (
        e.admin.post(
            "/api/documents",
            data={"path": "a/guide.pdf"},
            files={"file": ("guide.pdf", pdf_bytes("ALPHA pressure 43 kPa."))},
        ).status_code
        == 201
    )
    assert e.admin.post(f"/api/documents/{e.a}/sync").status_code == 200
    archive = e.tmp / "snapshot.tar.gz"
    backup(e.config, e.kb, archive)
    assert archive.stat().st_mode & 0o777 == 0o600
    target = e.config.model_copy(
        update={
            "state_path": str(e.tmp / "restored-state"),
            "document_state_path": str(e.tmp / "restored-state") + "-manager",
            "lexical_index_path": str(e.tmp / "restored-state") + "-lexical",
            "documents_path": str(e.tmp / "restored-documents"),
            "index_lock_path": str(e.tmp / "restored-locks"),
            "qdrant_collection": "restored",
        }
    )
    qpath = e.tmp / "restored-qdrant"
    restored_kb = KnowledgeBase(
        config=target,
        qdrant=QdrantClient(path=str(qpath), force_disable_check_same_thread=True),
        http=httpx.AsyncClient(transport=httpx.MockTransport(FakeOllama())),
    )
    restore(target, restored_kb, archive)
    asyncio.run(restored_kb.close())
    # Reopen on-disk Qdrant and SQLite to prove the restore survives a restart.
    restored_kb = KnowledgeBase(
        config=target,
        qdrant=QdrantClient(path=str(qpath), force_disable_check_same_thread=True),
        http=httpx.AsyncClient(transport=httpx.MockTransport(FakeOllama())),
    )
    restored_app = create_app(target, kb=restored_kb)
    stolen = TestClient(restored_app, base_url=target.app_origin)
    stolen.cookies.update(client.cookies)
    assert stolen.get("/api/documents").status_code == 401
    restored_client = sign_in(restored_app, "alice")
    assert [d["id"] for d in restored_client.get("/api/documents").json()] == [e.a]
    result = restored_client.post(
        "/api/ask", json={"question": "Quelle pression?"}
    ).json()
    assert result["grounded"] and "43 kPa" in result["sources"][0]["excerpt"]
    response = restored_client.get(result["sources"][0]["url"])
    with fitz.open(stream=response.content, filetype="pdf") as pdf:
        assert "43 kPa" in pdf[0].get_text()
    versions = restored_client.get(f"/api/documents/{e.a}/versions").json()
    assert len(versions) == 2
    assert (
        restored_client.get(
            f"/api/documents/{e.a}/file?version={versions[1]['hash']}"
        ).status_code
        == 200
    )
    assert restored_client.get(f"/api/documents/{e.b}/file").status_code == 404
    restored_admin = sign_in(restored_app)
    assert any(
        r["action"] == "restore.finish"
        for r in restored_admin.get("/api/admin/audit").json()["items"]
    )
    with pytest.raises(FileExistsError):
        restore(target, restored_kb, archive)
    for c in (stolen, restored_client, restored_admin):
        c.close()
    asyncio.run(restored_kb.close())


def test_tampered_archive_rejected_before_destination_writes_and_maintenance_blocks(
    secured,
):
    e = secured
    archive = e.tmp / "snapshot.tar.gz"
    backup(e.config, e.kb, archive)
    bad = e.tmp / "bad.tar.gz"
    with tarfile.open(archive) as source, tarfile.open(bad, "w:gz") as dest:
        for member in source:
            data = source.extractfile(member).read()
            if member.name.endswith(".pdf"):
                data = b"corrupted" + data[9:]
            dest.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="intégrité"):
        restore(e.config, e.kb, bad, overwrite=True)
    assert e.login("alice").get(f"/api/documents/{e.a}/file").status_code == 200
    with maintenance_lock(e.config, exclusive=True):
        assert e.admin.get("/api/documents").status_code == 503
    assert e.admin.get("/api/documents").status_code == 200


def test_retention_pagination_and_local_recovery(secured):
    e = secured
    with e.store.transaction() as db:
        db.execute(
            "INSERT INTO audit(at,action,resource,result) VALUES ('2000-01-01T00:00:00+00:00','old','old','success')"
        )
    assert (
        e.admin.put(
            "/api/admin/configuration", json={"audit_retention_days": 1}
        ).status_code
        == 200
    )
    page1 = e.admin.get("/api/admin/audit?limit=2").json()
    page2 = e.admin.get("/api/admin/audit?limit=2&offset=2").json()
    assert len(page1["items"]) == 2 and len(page2["items"]) == 2
    assert {a["id"] for a in page1["items"]}.isdisjoint(a["id"] for a in page2["items"])
    with e.store.connect() as db:
        assert not db.execute("SELECT 1 FROM audit WHERE action='old'").fetchone()
    e.store.reset_password(
        e.admin_user["id"], PASSWORD + "recovered", None, recover=True
    )
    assert e.admin.get("/api/admin/users").status_code == 401
    recovered = sign_in(e.app, password=PASSWORD + "recovered")
    assert recovered.get("/api/admin/users").status_code == 200
    recovered.close()


def test_network_requires_https_and_secure_cookies():
    with pytest.raises(ValueError):
        Settings(_env_file=None, app_origin="http://192.168.1.20")
    with pytest.raises(ValueError):
        Settings(
            _env_file=None, app_origin="https://instruct.internal", cookie_secure=False
        )


def test_direct_backend_requires_session_and_csrf_for_sensitive_routes(secured):
    e = secured
    anonymous = TestClient(e.app)
    for path in (
        "/api/documents",
        "/api/groups",
        "/api/admin/users",
        "/api/admin/audit",
        f"/api/documents/{e.a}/file",
    ):
        assert anonymous.get(path).status_code == 401
    assert anonymous.post("/api/documents", content=b"not parsed").status_code == 401
    assert anonymous.get("/healthz").json() == {"status": "ok"}
    assert anonymous.get("/docs").status_code == 404
    alice = e.login("alice")
    saved = alice.headers.pop("x-csrf-token")
    assert (
        alice.post("/api/ask", json={"question": "Question de test?"}).status_code
        == 403
    )
    alice.headers["x-csrf-token"] = saved
    assert (
        alice.post(
            "/api/ask",
            json={"question": "Question de test?"},
            headers={"content-length": "70000"},
        ).status_code
        == 413
    )
    anonymous.close()


def test_group_revocation_and_removed_document_block_all_versions(secured):
    e = secured
    alice = e.login("alice")
    assert e.admin.delete(f"/api/admin/groups/{e.ga}").status_code == 200
    assert alice.get("/api/documents").json() == []
    assert alice.get(f"/api/documents/{e.a}/versions").status_code == 404
    bob = e.login("bobby")
    version = bob.get(f"/api/documents/{e.b}/versions").json()[0]["hash"]
    assert e.admin.delete(f"/api/documents/{e.b}").status_code == 200
    assert bob.get(f"/api/documents/{e.b}/file?version={version}").status_code == 404
    assert not bob.post("/api/ask", json={"question": "Question de test?"}).json()[
        "grounded"
    ]


def test_secure_cookie_and_no_bootstrap_default_account(tmp_path):
    config = Settings(
        _env_file=None,
        state_path=str(tmp_path / "state"),
        documents_path=str(tmp_path / "documents"),
        app_origin="https://instruct.internal",
        cookie_secure=True,
    )
    app = create_app(config)
    with TestClient(app, base_url=config.app_origin) as client:
        response = client.get("/api/auth/session")
        assert response.json()["user"] is None
        assert "secure" in response.headers["set-cookie"].lower()
        with app.state.security.connect() as db:
            assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_failed_restore_is_fail_closed_and_explicit_retry_recovers(
    secured, monkeypatch
):
    e = secured
    archive = e.tmp / "backup.tar.gz"
    backup(e.config, e.kb, archive)
    target = e.config.model_copy(
        update={
            "state_path": str(e.tmp / "failed-state"),
            "document_state_path": str(e.tmp / "failed-state") + "-manager",
            "lexical_index_path": str(e.tmp / "failed-state") + "-lexical",
            "documents_path": str(e.tmp / "failed-docs"),
            "index_lock_path": str(e.tmp / "failed-locks"),
        }
    )
    qdrant = FaultyQdrant()
    kb = KnowledgeBase(
        config=target,
        qdrant=qdrant,
        http=httpx.AsyncClient(transport=httpx.MockTransport(FakeOllama())),
    )
    qdrant.fail_data = "before"
    with pytest.raises(RuntimeError):
        restore(target, kb, archive)
    assert (Path(target.state_path) / "RESTORE_INCOMPLETE").exists()
    app = create_app(target, kb=kb)
    blocked = TestClient(app)
    assert blocked.get("/api/auth/session").status_code == 503
    blocked.close()
    qdrant.fail_data = None
    restore(target, kb, archive, overwrite=True)
    app = create_app(target, kb=kb)
    client = sign_in(app, "alice")
    assert client.get(f"/api/documents/{e.a}/file").status_code == 200
    client.close()
    asyncio.run(kb.close())


def test_backup_refuses_unsynchronized_external_file_changes(secured):
    e = secured
    (Path(e.config.documents_path) / "a/guide.pdf").write_bytes(
        pdf_bytes("Local update not yet synchronized")
    )
    archive = e.tmp / "inconsistent.tar.gz"
    with pytest.raises(ValueError, match="incohérents"):
        backup(e.config, e.kb, archive)
    assert not archive.exists()


def test_restore_rejects_traversal_and_incompatible_schema_before_mutation(secured):
    e = secured
    bad = e.tmp / "traversal.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        member = tarfile.TarInfo("../outside")
        member.size = 3
        tar.addfile(member, io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="interdite"):
        restore(e.config, e.kb, bad, overwrite=True)
    good = e.tmp / "snapshot.tar.gz"
    backup(e.config, e.kb, good)
    incompatible = e.tmp / "incompatible.tar.gz"
    with tarfile.open(good) as source, tarfile.open(incompatible, "w:gz") as dest:
        for member in source:
            data = source.extractfile(member).read()
            if member.name == "manifest.json":
                manifest = json.loads(data)
                manifest["backup_schema"] = 999
                data = json.dumps(manifest).encode()
                member.size = len(data)
            dest.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="incompatible"):
        restore(e.config, e.kb, incompatible, overwrite=True)
    assert e.login("alice").get(f"/api/documents/{e.a}/file").status_code == 200


def test_failed_archive_write_never_logs_backup_success(secured, monkeypatch):
    e = secured

    def unavailable(*args, **kwargs):
        raise OSError("Synthetic full disk")

    monkeypatch.setattr("app.backup.tarfile.open", unavailable)
    archive = e.tmp / "unwritten.tar.gz"
    with pytest.raises(OSError):
        backup(e.config, e.kb, archive)
    assert not archive.exists()
    with e.store.connect() as db:
        assert [
            r[0]
            for r in db.execute("SELECT result FROM audit WHERE action='backup.finish'")
        ] == ["failure"]


def test_lexical_only_retrieval_excludes_other_group(secured, monkeypatch):
    e = secured
    from types import SimpleNamespace

    monkeypatch.setattr(
        e.kb.qdrant, "query_points", lambda *a, **kw: SimpleNamespace(points=[])
    )
    alice = e.login("alice")
    answer = alice.post("/api/ask", json={"question": "ALPHA pressure 42 kPa"}).json()
    assert answer["grounded"] and all(
        s["document"] == "a/guide.pdf" for s in answer["sources"]
    )
    before = len(e.ollama.chat_requests)
    forbidden = alice.post(
        "/api/ask", json={"question": "BETA confidential valve"}
    ).json()
    assert not forbidden["grounded"] and forbidden["sources"] == []
    assert len(e.ollama.chat_requests) == before
    assert e.admin.get("/api/library/documents").status_code == 200
    for name in ("alice", "manager"):
        client = e.login(name)
        for method, path in (
            ("get", "/api/library/documents"),
            ("post", "/api/library/documents/sync"),
            ("post", f"/api/library/documents/{e.b}/index"),
            ("get", f"/api/library/documents/{e.b}/file?version=unknown"),
        ):
            assert getattr(client, method)(path).status_code == 403


def test_advanced_worker_rechecks_revoked_session_and_never_persists_token(secured):
    e = secured
    manager = e.app.state.library
    import base64

    from itsdangerous import TimestampSigner

    raw = TimestampSigner(e.store.secret).unsign(
        e.admin.cookies.get("instruct_session")
    )
    token = json.loads(base64.b64decode(raw))["token"]
    manager.actor.set((e.admin_user["id"], token))
    job = manager.submit("index", e.kb, e.a, launch=False)
    assert token not in json.dumps(manager.state.jobs())
    e.admin.post("/api/auth/logout")
    manager.factory = lambda: e.kb
    manager.execute(job, token)
    stored = next(j for j in manager.state.jobs() if j["id"] == job["id"])
    assert stored["state"] == "failed"
