"""Opt-in local API benchmark. Never prints prompts, answers or service errors."""

import argparse
import asyncio
import json
import secrets
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import fitz
import httpx
from app.config import Settings
from app.main import create_app
from app.services import KnowledgeBase
from evaluation.checks import check_answer, excerpt_in_page
from evaluation.run_grounding import write_pdfs
from qdrant_client import QdrantClient

DOCUMENTS = {
    "bleues.pdf": ["Les cartes bleues se rangent dans le bac A."],
    "vertes.pdf": ["Les cartes vertes se rangent dans le bac B."],
}
CASES = [
    {
        "id": "simple",
        "question": "Où ranger les cartes bleues?",
        "expected_refusal": False,
        "expected_sources": [["bleues.pdf", 1]],
        "required_text": ["Les cartes bleues se rangent dans le bac A."],
        "forbidden_text": [],
    },
    {
        "id": "multi-pdf",
        "question": "Où ranger les cartes bleues et les cartes vertes?",
        "expected_refusal": False,
        "expected_sources": [["bleues.pdf", 1], ["vertes.pdf", 1]],
        "required_text": [
            "Les cartes bleues se rangent dans le bac A.",
            "Les cartes vertes se rangent dans le bac B.",
        ],
        "forbidden_text": [],
    },
    {
        "id": "absent",
        "question": "Quelle est la masse en grammes des cartes bleues?",
        "expected_refusal": True,
        "expected_sources": [],
        "required_text": [],
        "forbidden_text": [],
    },
]
METRICS = (
    "eval_count",
    "prompt_eval_count",
    "eval_duration",
    "prompt_eval_duration",
    "load_duration",
    "total_duration",
)


def numeric(value):
    return value if type(value) in (int, float) and value >= 0 else None


async def residency(http, url):
    """Structured equivalent of ollama ps; no raw command output."""
    try:
        response = await http.get(url + "/api/ps")
        response.raise_for_status()
        models = response.json()["models"]
        return [
            {
                "model": m["name"],
                "size": numeric(m.get("size")),
                "size_vram": numeric(m.get("size_vram")),
            }
            for m in models
        ]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


class MeasuredHTTP(httpx.AsyncClient):
    def __init__(self, url, **kwargs):
        super().__init__(timeout=120, **kwargs)
        self.url, self.calls = url, []
        self.probe_seconds = 0.0

    async def post(self, url, **kwargs):
        if urlsplit(str(url)).path not in {"/api/chat", "/api/embed"}:
            return await super().post(url, **kwargs)
        start = time.perf_counter()
        before = await residency(self, self.url)
        self.probe_seconds += time.perf_counter() - start
        record = {"endpoint": urlsplit(str(url)).path, "before": before}
        self.calls.append(record)
        start = time.perf_counter()
        try:
            response = await super().post(url, **kwargs)
            record["http_status"] = response.status_code
            try:
                body = response.json()
                if isinstance(body, dict):
                    record.update({key: numeric(body.get(key)) for key in METRICS})
            except ValueError:
                pass
            return response
        finally:
            record["http_seconds"] = time.perf_counter() - start


def system_snapshot():
    """Best effort Linux RAM + NVIDIA numbers, never command stderr/stdout dumps."""
    sample = {"ram_kib": None, "gpu_mib": None}
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(value.split()[0])
        sample["ram_kib"] = values
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        sample["gpu_mib"] = [
            dict(zip(("used", "total"), map(int, line.split(","))))
            for line in result.stdout.splitlines()
        ]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return sample


async def unload(http, url):
    models = await residency(http, url)
    if models is None:
        return False
    for model in models:
        response = await http.post(
            url + "/api/generate", json={"model": model["model"], "keep_alive": 0}
        )
        if response.is_error:
            return False
    return await residency(http, url) == []


def quality(case, result, mode, root, calls):
    """Check original PDF evidence in memory; export booleans/counts only."""
    sources = result.get("sources", [])
    evidence_ok = True
    for source in sources:
        path = (root / source["document"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            evidence_ok = False
            continue
        with fitz.open(path) as pdf:
            page = source["page"]
            if not isinstance(page, int) or not 1 <= page <= len(pdf):
                evidence_ok = False
            elif not excerpt_in_page(source["excerpt"], pdf[page - 1].get_text()):
                evidence_ok = False
    chats = sum(c["endpoint"] == "/api/chat" for c in calls)
    if mode == "search":
        expected = {tuple(s) for s in case["expected_sources"]}
        actual = {(s["document"], s["page"]) for s in sources}
        passed = (
            result.get("grounded") is False
            and result.get("answer") == ""
            and result.get("claims") == []
            and chats == 0
            and expected <= actual
            and evidence_ok
        )
    else:
        passed = not check_answer(case, result, root) and evidence_ok and chats <= 1
        if case["expected_refusal"]:
            passed = passed and result.get("claims") == []
    return {
        "passed": passed,
        "citations_valid": evidence_ok,
        "grounded": result.get("grounded"),
        "source_count": len(sources),
        "claim_count": len(result.get("claims", [])),
        "chat_calls": chats,
    }


async def benchmark(args):
    rows = []
    collection = "response_benchmark_" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="instruct-modes-") as temporary:
        root = Path(temporary)
        write_pdfs(root / "pdf", DOCUMENTS)
        # Explicit defaults make the experiment independent of private .env files.
        defaults = {
            name: field.default for name, field in Settings.model_fields.items()
        }
        defaults.update(
            ollama_url=args.ollama_url,
            ollama_model=args.model,
            embedding_model=args.embedding_model,
            qdrant_collection=collection,
            documents_path=str(root / "pdf"),
            state_path=str(root / "state"),
            index_lock_path=str(root / "locks"),
            lexical_index_path=str(root / "lexical"),
            app_origin="http://localhost:3000",
            cookie_secure=False,
            ollama_num_ctx=4096,
            ollama_num_predict=768,
            ollama_reflection_num_predict=1536,
            document_state_path=str(root / "document-state"),
        )
        config = Settings(_env_file=None, **defaults)
        qdrant = QdrantClient(url=args.qdrant_url)
        http = MeasuredHTTP(args.ollama_url)
        kb = KnowledgeBase(config=config, qdrant=qdrant, http=http)
        app = create_app(config, kb=kb)
        password = secrets.token_urlsafe(32)
        app.state.security.create_user("benchmark", password, "admin", bootstrap=True)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=config.app_origin
            ) as api:
                api.headers["origin"] = config.app_origin
                api.headers["x-csrf-token"] = (
                    await api.get("/api/auth/session")
                ).json()["csrf"]
                login = await api.post(
                    "/api/auth/login",
                    json={"username": "benchmark", "password": password},
                )
                login.raise_for_status()
                api.headers["x-csrf-token"] = login.json()["csrf"]
                ingestion = await api.post("/api/ingest")
                ingestion.raise_for_status()
                if ingestion.json()["failed"]:
                    raise RuntimeError("benchmark_setup_failed")
                # Metadata detection is outside timings; no generation/preload.
                await api.get("/api/response-modes")
                for case in CASES:
                    for mode in ("fast", "reflection", "search"):
                        for iteration in range(args.repeat + 1):
                            cold = (
                                await unload(http, args.ollama_url)
                                if iteration == 0
                                else None
                            )
                            before = await residency(http, args.ollama_url)
                            system_before = system_snapshot()
                            http.calls.clear()
                            http.probe_seconds = 0
                            start = time.perf_counter()
                            response = await api.post(
                                "/api/ask",
                                json={"question": case["question"], "mode": mode},
                            )
                            elapsed = time.perf_counter() - start
                            result = (
                                response.json() if response.status_code == 200 else None
                            )
                            checks = (
                                quality(case, result, mode, root / "pdf", http.calls)
                                if result
                                else {"passed": False}
                            )
                            rows.append(
                                {
                                    "case": case["id"],
                                    "mode": mode,
                                    "phase": "cold"
                                    if iteration == 0
                                    else "warm-repeat",
                                    "iteration": iteration,
                                    "cold_verified": cold,
                                    "api_seconds": elapsed,
                                    "probe_seconds": http.probe_seconds,
                                    "http_status": response.status_code,
                                    "quality": checks,
                                    "ollama": list(http.calls),
                                    "resident_before": before,
                                    "resident_after": await residency(
                                        http, args.ollama_url
                                    ),
                                    "system_before": system_before,
                                    "system_after": system_snapshot(),
                                }
                            )
        finally:
            # Delete only the uniquely named benchmark collection, even on failure.
            try:
                for name in (collection, collection + "__manifest"):
                    if qdrant.collection_exists(name):
                        qdrant.delete_collection(name)
            finally:
                await kb.close()
    report = {
        "schema_version": 1,
        "corpus_version": 1,
        "model": args.model,
        "embedding_model": args.embedding_model,
        "budgets": {"context": 4096, "fast": 768, "reflection": 1536},
        "api_transport": "in-process ASGI; real auth/retrieval/Ollama/Qdrant",
        "runs": rows,
    }
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if all(row["quality"]["passed"] for row in rows) else 1


def local_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1", "ollama", "qdrant"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise argparse.ArgumentTypeError(
            "Use a local service origin without credentials"
        )
    return value.rstrip("/")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--ollama-url", type=local_url, default="http://localhost:11434"
    )
    parser.add_argument("--qdrant-url", type=local_url, default="http://localhost:6333")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--embedding-model", default="nomic-embed-text")
    parser.add_argument(
        "--repeat",
        type=int,
        default=2,
        help="Warm repeats after one cold run per case/mode",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if (
        not args.live
        or args.repeat < 1
        or Path(args.output).exists()
        or ":cloud" in args.model
        or ":cloud" in args.embedding_model
    ):
        parser.error(
            "Require --live, positive --repeat, local models and a new output path"
        )
    try:
        return asyncio.run(benchmark(args))
    except Exception:
        # Never expose HTTP bodies, prompts, auth state or exception strings.
        print("Benchmark failed; check local services. No service response logged.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
