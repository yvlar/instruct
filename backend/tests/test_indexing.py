import asyncio
import hashlib
import json
import warnings
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app.config import Settings
from app.indexing import IndexErrorBase, PdfSource, active_filter, index_lock
from app.services import KnowledgeBase
from qdrant_client import QdrantClient, models


def run(coroutine):
    return asyncio.run(coroutine)


class FakeFiles:
    def __init__(self):
        self.files = {"instructions/start.pdf": ["Ancienne procédure."]}
        self.extract_calls = []
        self.unavailable = False
        self.fail_extraction = False
        self.fail_read = False

    def inventory(self):
        if self.unavailable:
            raise IndexErrorBase("DOCUMENTS_UNAVAILABLE")
        return {name: tuple(pages) for name, pages in self.files.items()}

    def fingerprint(self, document):
        if self.fail_read:
            raise PermissionError("secret read error")
        return hashlib.sha256(json.dumps(self.files[document]).encode()).hexdigest()

    def passages(self, document, expected_hash):
        self.extract_calls.append(document)
        for number, text in enumerate(self.files[document], 1):
            if self.fail_extraction:
                raise ValueError("private PDF content")
            yield number, text


class FakeOllama:
    def __init__(self):
        self.inputs = []
        self.digest = "model-digest-1"
        self.fail = False
        self.bad_count = False
        self.on_embed = None
        self.chat_requests = []

    def __call__(self, request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "nomic-embed-text:latest", "digest": self.digest},
                    ]
                },
            )
        body = json.loads(request.content)
        if request.url.path == "/api/chat":
            self.chat_requests.append(body)
            context = (
                body["messages"][1]["content"]
                .split("CONTEXTE:\n", 1)[1]
                .split("\n\nQUESTION:", 1)[0]
            )
            passages = [json.loads(part) for part in context.split("\n\n")]
            content = {
                "answer": "Réponse sourcée.",
                "citations": [
                    {"passage_id": p["id"], "quote": p["text"]} for p in passages
                ],
            }
            return httpx.Response(
                200, json={"message": {"content": json.dumps(content)}}
            )
        assert request.url.path == "/api/embed"
        assert isinstance(body["input"], list)
        assert body["truncate"] is False
        self.inputs.append(body["input"])
        if self.on_embed:
            self.on_embed()
        if self.fail:
            return httpx.Response(500, json={"error": "secret provider error"})
        vectors = [[1.0, 0.5, 0.25] for _ in body["input"]]
        return httpx.Response(
            200, json={"embeddings": [] if self.bad_count else vectors}
        )


class Interrupted(BaseException):
    """Simulate process death: ordinary per-document handlers cannot catch it."""


class FaultyQdrant:
    """Server double backed by Qdrant's local filter/query implementation."""

    def __init__(self):
        self.client = QdrantClient(":memory:")
        self.data_upserts = []
        self.fail_data = None
        self.fail_manifest = False
        self.fail_cleanup = False
        self.interrupt_after_manifest = False
        self.interrupt_after_batch = False
        self.manifest_reply_lost = False

    def __getattr__(self, name):
        return getattr(self.client, name)

    def create_payload_index(self, *args, **kwargs):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            return self.client.create_payload_index(*args, **kwargs)

    def upsert(self, collection_name, points, **kwargs):
        manifest = collection_name.endswith("__manifest")
        is_document = manifest and points[0].payload.get("kind") == "document"
        if is_document and self.fail_manifest:
            raise RuntimeError("private write error")
        if not manifest:
            self.data_upserts.append(deepcopy(points))
            if self.fail_data == "before":
                raise RuntimeError("private write error")
        result = self.client.upsert(collection_name, points=points, **kwargs)
        if not manifest and self.fail_data == "after":
            raise RuntimeError("lost reply after data persisted")
        if not manifest and self.interrupt_after_batch:
            raise Interrupted()
        if is_document and self.interrupt_after_manifest:
            raise Interrupted()
        if is_document and self.manifest_reply_lost:
            raise RuntimeError("lost reply after manifest persisted")
        return result

    def delete(self, collection_name, **kwargs):
        if not collection_name.endswith("__manifest") and self.fail_cleanup:
            raise RuntimeError("cleanup unavailable")
        return self.client.delete(collection_name, **kwargs)


@pytest.fixture
def env(tmp_path):
    config = Settings(
        _env_file=None,
        documents_path=str(tmp_path / "documents"),
        index_lock_path=str(tmp_path / "locks"),
        lexical_index_path=str(tmp_path / "lexical"),
        embedding_batch_size=2,
    )
    files = FakeFiles()
    ollama = FakeOllama()
    qdrant = FaultyQdrant()
    http = httpx.AsyncClient(transport=httpx.MockTransport(ollama))
    kb = KnowledgeBase(config=config, qdrant=qdrant, http=http, source=files)
    yield SimpleNamespace(
        kb=kb, files=files, ollama=ollama, qdrant=qdrant, config=config
    )
    run(http.aclose())
    qdrant.close()


def indexed_texts(env, *, active=False):
    _, manifests = env.kb.store.read()
    if active and not manifests:
        return []
    points, _ = env.qdrant.scroll(
        env.config.qdrant_collection,
        limit=1000,
        scroll_filter=active_filter(manifests) if active else None,
    )
    return sorted(point.payload["text"] for point in points)


def test_first_add_and_unchanged_skip_extraction_embeddings_and_upserts(env):
    result = run(env.kb.ingest())
    assert result == dict(
        documents=1,
        chunks=1,
        added=1,
        modified=0,
        unchanged=0,
        deleted=0,
        failed=0,
        errors=[],
        cleanup_pending=False,
    )
    inputs = deepcopy(env.ollama.inputs)
    writes = len(env.qdrant.data_upserts)
    result = run(env.kb.ingest())
    assert result["unchanged"] == 1 and result["chunks"] == 0
    assert env.files.extract_calls == ["instructions/start.pdf"]
    assert env.ollama.inputs == inputs
    assert len(env.qdrant.data_upserts) == writes
    assert indexed_texts(env) == ["Ancienne procédure."]


def test_modification_replaces_old_passages_with_fewer_chunks(env):
    env.files.files["instructions/start.pdf"] = [
        "Ancien un.",
        "Ancien deux.",
        "Ancien trois.",
    ]
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Nouvelle procédure."]
    result = run(env.kb.ingest())
    assert result["modified"] == 1
    assert indexed_texts(env) == ["Nouvelle procédure."]
    answer = run(env.kb.ask("Quelle procédure?"))
    assert answer["sources"][0]["document"] == "instructions/start.pdf"
    assert (
        "Nouvelle procédure." in env.ollama.chat_requests[0]["messages"][1]["content"]
    )


def test_delete_one_document_and_same_basename_in_subdirectories(env):
    env.files.files = {"a/fiche.pdf": ["Procédure A"], "b/fiche.pdf": ["Procédure B"]}
    assert run(env.kb.ingest())["added"] == 2
    answer = run(env.kb.ask("Procédures disponibles?"))
    assert {s["document"] for s in answer["sources"]} == {"a/fiche.pdf", "b/fiche.pdf"}
    del env.files.files["a/fiche.pdf"]
    result = run(env.kb.ingest())
    assert result["deleted"] == 1 and result["unchanged"] == 1
    assert indexed_texts(env) == ["Procédure B"]


@pytest.mark.parametrize(
    "failure,code",
    [
        ("extract", "EXTRACTION_FAILED"),
        ("embed", "EMBEDDING_FAILED"),
        ("bad_count", "EMBEDDING_FAILED"),
        ("write", "QDRANT_WRITE_FAILED"),
        ("write_reply", "QDRANT_WRITE_FAILED"),
        ("manifest", "MANIFEST_WRITE_FAILED"),
        ("read", "READ_FAILED"),
    ],
)
def test_failures_preserve_old_revision_and_do_not_expose_internal_errors(
    env, failure, code
):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Version incomplète."] * 3
    if failure == "extract":
        env.files.fail_extraction = True
    elif failure == "embed":
        env.ollama.fail = True
    elif failure == "bad_count":
        env.ollama.bad_count = True
    elif failure == "write":
        env.qdrant.fail_data = "before"
    elif failure == "write_reply":
        env.qdrant.fail_data = "after"
    elif failure == "manifest":
        env.qdrant.fail_manifest = True
    elif failure == "read":
        env.files.fail_read = True
    result = run(env.kb.ingest())
    assert result["failed"] == 1
    assert result["errors"] == [{"document": "instructions/start.pdf", "code": code}]
    assert indexed_texts(env, active=True) == ["Ancienne procédure."]
    assert "Ancienne procédure." in indexed_texts(env)


def test_empty_pdf_preserves_old_version(env):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = []
    assert run(env.kb.ingest())["errors"][0]["code"] == "NO_TEXT"
    assert indexed_texts(env, active=True) == ["Ancienne procédure."]


@pytest.mark.parametrize("unavailable", [False, True])
def test_missing_or_accidentally_empty_tree_never_deletes(env, unavailable):
    run(env.kb.ingest())
    env.files.files.clear()
    env.files.unavailable = unavailable
    with pytest.raises(IndexErrorBase, match="DOCUMENTS_UNAVAILABLE|EMPTY_DOCUMENTS"):
        run(env.kb.ingest())
    assert indexed_texts(env) == ["Ancienne procédure."]
    if unavailable:
        with pytest.raises(IndexErrorBase, match="DOCUMENTS_UNAVAILABLE"):
            run(env.kb.ingest(allow_empty=True))


def test_explicit_full_deletion_is_idempotent_and_does_not_need_ollama(env):
    run(env.kb.ingest())
    env.files.files.clear()
    env.ollama.fail = True
    result = run(env.kb.ingest(allow_empty=True))
    assert result["deleted"] == 1
    assert indexed_texts(env) == []
    assert run(env.kb.ingest(allow_empty=True))["deleted"] == 0
    assert run(env.kb.ask("Une question?"))["grounded"] is False


@pytest.mark.parametrize("after_manifest", [False, True])
def test_restart_after_interruption_repairs_and_avoids_duplicates(env, after_manifest):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Nouveau 1", "Nouveau 2", "Nouveau 3"]
    env.qdrant.interrupt_after_manifest = after_manifest
    env.qdrant.interrupt_after_batch = not after_manifest
    with pytest.raises(Interrupted):
        run(env.kb.ingest())
    assert "Ancienne procédure." in indexed_texts(env)
    expected = (
        ["Nouveau 1", "Nouveau 2", "Nouveau 3"]
        if after_manifest
        else ["Ancienne procédure."]
    )
    assert indexed_texts(env, active=True) == expected
    env.qdrant.interrupt_after_manifest = env.qdrant.interrupt_after_batch = False
    env.kb = KnowledgeBase(
        config=env.config, qdrant=env.qdrant, http=env.kb.http, source=env.files
    )
    result = run(env.kb.ingest())
    assert result["unchanged" if after_manifest else "modified"] == 1
    assert indexed_texts(env) == ["Nouveau 1", "Nouveau 2", "Nouveau 3"]
    writes = len(env.qdrant.data_upserts)
    run(env.kb.ingest())
    assert len(env.qdrant.data_upserts) == writes


def test_lost_manifest_reply_preserves_both_versions_until_retry(env):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Nouvelle version"]
    env.qdrant.manifest_reply_lost = True
    result = run(env.kb.ingest())
    assert result["failed"] == 1
    assert indexed_texts(env) == ["Ancienne procédure.", "Nouvelle version"]
    assert indexed_texts(env, active=True) == ["Nouvelle version"]
    env.qdrant.manifest_reply_lost = False
    assert run(env.kb.ingest())["unchanged"] == 1
    assert indexed_texts(env) == ["Nouvelle version"]


def test_cleanup_failure_never_exposes_old_version_and_is_retried(env):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Nouvelle version"]
    env.qdrant.fail_cleanup = True
    result = run(env.kb.ingest())
    assert result["modified"] == 1 and result["cleanup_pending"]
    assert indexed_texts(env, active=True) == ["Nouvelle version"]
    assert len(indexed_texts(env)) == 2
    env.qdrant.fail_cleanup = False
    result = run(env.kb.ingest())
    assert result["unchanged"] == 1 and not result["cleanup_pending"]
    assert indexed_texts(env) == ["Nouvelle version"]


def test_bounded_batches_and_deterministic_ids(env):
    env.files.files["instructions/start.pdf"] = [f"Texte {i}" for i in range(9)]
    run(env.kb.ingest())
    assert max(map(len, env.ollama.inputs)) == 2
    assert max(map(len, env.qdrant.data_upserts)) == 2
    first = {p.id for batch in env.qdrant.data_upserts for p in batch}
    files = deepcopy(env.files.files)
    env.files.files.clear()
    run(env.kb.ingest(allow_empty=True))
    env.qdrant.data_upserts.clear()
    env.files.files = files
    run(env.kb.ingest())
    assert {p.id for batch in env.qdrant.data_upserts for p in batch} == first


def test_changed_chunk_settings_reindex_but_batch_size_does_not(env):
    run(env.kb.ingest())
    env.config.embedding_batch_size = 1
    assert run(env.kb.ingest())["unchanged"] == 1
    env.config.chunk_overlap = 100
    assert run(env.kb.ingest())["modified"] == 1


def test_model_digest_change_requires_separate_collection(env):
    run(env.kb.ingest())
    env.ollama.digest = "new-incompatible-model"
    with pytest.raises(IndexErrorBase, match="EMBEDDING_MODEL_CHANGED"):
        run(env.kb.ingest())
    with pytest.raises(IndexErrorBase, match="EMBEDDING_MODEL_CHANGED"):
        run(env.kb.ask("Question?"))
    assert indexed_texts(env) == ["Ancienne procédure."]


def test_model_changed_during_embedding_does_not_publish(env):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Nouveau"]
    env.ollama.on_embed = lambda: setattr(env.ollama, "digest", "changed-during-ingest")
    assert run(env.kb.ingest())["errors"][0]["code"] == "EMBEDDING_MODEL_CHANGED"
    assert indexed_texts(env, active=True) == ["Ancienne procédure."]


def test_legacy_index_refused_for_ingest_and_ask_without_any_mutation(env):
    env.qdrant.create_collection(
        env.config.qdrant_collection,
        vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE),
    )
    env.qdrant.upsert(
        env.config.qdrant_collection,
        points=[
            models.PointStruct(
                id=1,
                vector=[1.0, 0.5, 0.25],
                payload={"document": "fiche.pdf", "page": 1, "text": "Legacy"},
            )
        ],
    )
    for operation in (
        env.kb.ingest,
        lambda: env.kb.ingest(allow_empty=True),
        lambda: env.kb.ask("Question?"),
    ):
        with pytest.raises(IndexErrorBase, match="LEGACY_INDEX"):
            run(operation())
    assert env.qdrant.count(env.config.qdrant_collection).count == 1
    assert not env.ollama.inputs


def test_concurrent_operations_fail_fast_instead_of_corrupting_index(env):
    with index_lock(env.config):
        with pytest.raises(IndexErrorBase, match="INDEX_BUSY"):
            run(env.kb.ingest())
        with pytest.raises(IndexErrorBase, match="INDEX_BUSY"):
            run(env.kb.ask("Question?"))
    assert run(env.kb.ingest())["added"] == 1


def test_changed_tree_aborts_deletions(env):
    env.files.files["deleted.pdf"] = ["À conserver"]
    run(env.kb.ingest())
    del env.files.files["deleted.pdf"]
    env.files.files["instructions/start.pdf"] = ["Nouvelle version"]
    env.ollama.on_embed = lambda: env.files.files.update(
        {"arrived.pdf": ["Arrivé entretemps"]}
    )
    with pytest.raises(IndexErrorBase, match="DOCUMENTS_CHANGED"):
        run(env.kb.ingest())
    assert "À conserver" in indexed_texts(env, active=True)


def test_read_failure_cancels_deletions(env):
    env.files.files["deleted.pdf"] = ["À conserver"]
    run(env.kb.ingest())
    del env.files.files["deleted.pdf"]
    env.files.fail_read = True
    assert run(env.kb.ingest())["deleted"] == 0
    assert "À conserver" in indexed_texts(env, active=True)


def test_real_inventory_rejects_missing_unreadable_or_symlink_tree(
    tmp_path, monkeypatch
):
    source = PdfSource(str(tmp_path / "missing"), 1400, 250)
    with pytest.raises(IndexErrorBase, match="DOCUMENTS_UNAVAILABLE"):
        source.inventory()
    source.root = tmp_path
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "guide.PDF").write_bytes(b"test")
    assert set(source.inventory()) == {"a/guide.PDF"}
    (tmp_path / "link").symlink_to(tmp_path / "a", target_is_directory=True)
    with pytest.raises(IndexErrorBase, match="DOCUMENTS_UNAVAILABLE"):
        source.inventory()
    (tmp_path / "link").unlink()

    def unreadable(root, onerror):
        onerror(PermissionError("subdirectory cannot be read"))
        return iter(())

    monkeypatch.setattr("app.indexing.os.walk", unreadable)
    with pytest.raises(IndexErrorBase, match="DOCUMENTS_UNAVAILABLE"):
        source.inventory()


def test_api_response_retains_legacy_counts_and_supports_explicit_empty(
    env, monkeypatch
):
    from app import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "knowledge_base", env.kb)
    with TestClient(main.app) as client:
        result = client.post("/api/ingest").json()
        assert (
            result["documents"] == 1 and result["chunks"] == 1 and result["added"] == 1
        )
        env.files.files.clear()
        assert client.post("/api/ingest").status_code == 503
        assert client.post("/api/ingest?allow_empty=true").json()["deleted"] == 1


def test_configuration_rejects_unbounded_or_invalid_batches():
    for fields in (
        {"embedding_batch_size": 0},
        {"embedding_batch_size": 129},
        {"chunk_size": 100, "chunk_overlap": 100},
    ):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **fields)


def test_unconfirmed_qdrant_write_cannot_publish(env, monkeypatch):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Version non confirmée"]
    original = env.qdrant.upsert

    def acknowledge(collection_name, points, **kwargs):
        result = original(collection_name, points, **kwargs)
        if not collection_name.endswith("__manifest"):
            return models.UpdateResult(
                operation_id=1, status=models.UpdateStatus.ACKNOWLEDGED
            )
        return result

    monkeypatch.setattr(env.qdrant, "upsert", acknowledge)
    assert run(env.kb.ingest())["errors"][0]["code"] == "QDRANT_WRITE_FAILED"
    assert indexed_texts(env, active=True) == ["Ancienne procédure."]


def test_interrupted_first_add_is_not_mistaken_for_legacy_index(env):
    env.qdrant.interrupt_after_batch = True
    with pytest.raises(Interrupted):
        run(env.kb.ingest())
    assert indexed_texts(env, active=True) == []
    env.qdrant.interrupt_after_batch = False
    assert run(env.kb.ingest())["added"] == 1
    assert indexed_texts(env) == ["Ancienne procédure."]


def test_manifest_pagination_does_not_delete_documents_beyond_first_page(env):
    env.files.files = {f"dossier/{i}.pdf": [f"Texte {i}"] for i in range(130)}
    assert run(env.kb.ingest())["added"] == 130
    assert len(indexed_texts(env, active=True)) == 130
    result = run(env.kb.ingest())
    assert result["unchanged"] == 130 and result["deleted"] == 0
    assert len(indexed_texts(env)) == 130


def test_pdf_changed_during_embedding_keeps_old_revision(env):
    run(env.kb.ingest())
    env.files.files["instructions/start.pdf"] = ["Version intermédiaire"]
    env.ollama.on_embed = lambda: env.files.files.update(
        {"instructions/start.pdf": ["Version finale"]}
    )
    with pytest.raises(IndexErrorBase, match="DOCUMENTS_CHANGED"):
        run(env.kb.ingest())
    assert indexed_texts(env, active=True) == ["Ancienne procédure."]
    env.ollama.on_embed = None
    assert run(env.kb.ingest())["modified"] == 1
    assert indexed_texts(env) == ["Version finale"]
