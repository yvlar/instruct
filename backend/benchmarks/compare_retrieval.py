"""Run from backend: python -m benchmarks.compare_retrieval --output DIR.

Default: controlled toy embeddings, real PDF/SQLite/embedded Qdrant operations.
--ollama-url http://localhost:11434: real, already installed embeddings; no pulls,
no chat call, no mutation of the application's collections. No quality claim can
be inferred from the deliberately coarse toy embeddings.
"""

import argparse
import asyncio
import json
import platform
import re
import sqlite3
import statistics
import time
import warnings
from pathlib import Path

import fitz
import httpx
from app.config import Settings
from app.indexing import PdfSource
from app.lexical import normalize
from app.retrieval import select_context
from app.services import KnowledgeBase
from qdrant_client import QdrantClient

CORPUS = {
    "orion.pdf": [
        [
            "CONSIGNES ORION",
            "1. Actionner le sectionneur avant toute maintenance.\nAttendre que le voyant soit eteint.",
            "2. En cas d'arret d'urgence, presser le bouton rouge.",
        ],
        [
            "MAINTENANCE ORION",
            "1. Installer le joint AB-204/X dans la bride.",
            "2. Regler la pression a 12,5 bar.\nControler le manometre.",
        ],
    ],
    "neptune.pdf": [
        [
            "MACHINE NEPTUNE",
            "1. Installer le joint AB-205/X dans la bride.",
            "2. Regler le capteur a 12,5 kPa.",
            "3. Regler le circuit secondaire a 12,6 bar.",
        ]
    ],
}


def write_corpus(root):
    root.mkdir(parents=True)
    for name, pages in CORPUS.items():
        with fitz.open() as pdf:
            for blocks in pages:
                page = pdf.new_page()
                for index, text in enumerate(blocks):
                    page.insert_text(
                        (72, 72 + 70 * index), text, fontsize=14 if index == 0 else 11
                    )
            pdf.save(root / name)


class LegacyPdfSource(PdfSource):
    """Frozen ee5cb73 page.get_text + whitespace-flattening chunker baseline."""

    def page_passages(self, page):
        clean = re.sub(r"\s+", " ", page.get_text()).strip()
        start = 0
        while start < len(clean):
            end = min(start + self.size, len(clean))
            if end < len(clean):
                boundary = clean.rfind(". ", start, end)
                if boundary > start + self.size // 2:
                    end = boundary + 1
            yield clean[start:end]
            if end == len(clean):
                break
            start = max(start + 1, end - self.overlap)


class NoLexicalIndex:
    """Baseline measures no lexical writes, verification or cleanup."""

    def repair(self, *args):
        pass

    def upsert(self, *args):
        pass

    def require_ready(self, *args):
        pass

    def collect_garbage(self, *args):
        pass


class BaselineKnowledgeBase(KnowledgeBase):
    @property
    def lexical(self):
        return NoLexicalIndex()


def toy_vector(text):
    text = normalize(text)
    # Explicit, coarse concepts: intentional collisions of codes and nearby values.
    # This exercises ranking plumbing; it is not a stand-in for nomic accuracy.
    concepts = [
        ("sectionneur", "alimentation", "electrique", "entretien", "maintenance"),
        ("joint", "installer", "bride"),
        ("bar", "pression", "kpa", "capteur", "regler"),
        ("urgence", "rouge"),
        ("neptune",),
        ("orion",),
    ]
    vector = [float(any(word in text for word in group)) for group in concepts]
    return vector + [0.0 if any(vector) else 1.0]


def toy_ollama(request):
    if request.url.path == "/api/tags":
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "nomic-embed-text:latest", "digest": "toy-v1-not-a-model"}
                ]
            },
        )
    assert request.url.path == "/api/embed", "Benchmark must never call chat or pull"
    body = json.loads(request.content)
    return httpx.Response(
        200, json={"embeddings": [toy_vector(t) for t in body["input"]]}
    )


def size_bytes(root):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


async def benchmark(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = output / "documents"
    write_corpus(root)
    questions = json.loads(Path(__file__).with_name("questions.json").read_text())
    report = {
        "mode": "real-ollama-embedded-qdrant"
        if args.ollama_url
        else "controlled-toy-embeddings",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "pymupdf": fitz.VersionBind,
        "repeat": args.repeat,
        "documents": len(CORPUS),
        "pages": 3,
        "baseline_commit": "ee5cb73",
        "chat_calls": 0,
        "limitations": "Embedded Qdrant sizes/times, not Qdrant server. Toy results do not measure real semantic quality or GPU latency.",
        "runs": {},
    }
    for mode in ("semantic_baseline", "hybrid"):
        location = output / mode
        location.mkdir()
        config = Settings(
            _env_file=None,
            state_path=str(location / "state"),
            documents_path=str(root),
            index_lock_path=str(location / "locks"),
            lexical_index_path=str(location / "lexical"),
            qdrant_collection=mode,
            ollama_url=args.ollama_url or "http://toy.local",
            embedding_model=args.embedding_model,
            top_k=4,
        )
        # No environment override silently changes the controlled benchmark.
        config.chunk_size, config.chunk_overlap = 1400, 250
        config.min_score, config.retrieval_candidates, config.context_max_chars = (
            0.35,
            24,
            8000,
        )
        config.embedding_batch_size = 16
        kb_class = (
            BaselineKnowledgeBase if mode == "semantic_baseline" else KnowledgeBase
        )
        http = httpx.AsyncClient(
            timeout=120,
            transport=None if args.ollama_url else httpx.MockTransport(toy_ollama),
        )
        kb = kb_class(
            config=config,
            qdrant=QdrantClient(path=str(location / "qdrant")),
            http=http,
            source=LegacyPdfSource(str(root), 1400, 250)
            if mode == "semantic_baseline"
            else None,
        )
        try:
            start = time.perf_counter()
            result = await kb.ingest()
            duration = (time.perf_counter() - start) * 1000
            if result["failed"] or result["cleanup_pending"]:
                raise RuntimeError(f"Benchmark indexing failed: {result}")
            start = time.perf_counter()
            unchanged = await kb.ingest()
            unchanged_ms = (time.perf_counter() - start) * 1000
            control, _ = kb.store.read()
            run = {
                "index_ms": duration,
                "unchanged_ms": unchanged_ms,
                "chunks": result["chunks"],
                "unchanged_chunks_written": unchanged["chunks"],
                "embedding_identity": control["embedding"],
                "vector_size": control["vector_size"],
                "questions": [],
            }
            for question in questions:
                durations = []
                hits = []
                for _ in range(args.repeat):
                    start = time.perf_counter()
                    hits = await kb.retrieve(
                        question["question"], semantic_only=mode == "semantic_baseline"
                    )
                    if mode == "hybrid":
                        hits = select_context(
                            hits,
                            max_passages=config.top_k,
                            max_chars=config.context_max_chars,
                        )
                    durations.append((time.perf_counter() - start) * 1000)
                rank = next(
                    (
                        i
                        for i, p in enumerate(hits, 1)
                        if p.document == question["document"]
                        and p.page == question["page"]
                        and question["contains"] in p.text
                    ),
                    None,
                )
                run["questions"].append(
                    {
                        "id": question["id"],
                        "expected_rank": rank,
                        "no_candidates": not hits,
                        "median_search_ms": statistics.median(durations),
                        "top_results": [
                            {"id": p.id, "document": p.document, "page": p.page}
                            for p in hits
                        ],
                    }
                )
            report["runs"][mode] = run
        finally:
            await kb.close()
        run["qdrant_disk_bytes"] = size_bytes(location / "qdrant")
        run["lexical_disk_bytes"] = size_bytes(location / "lexical")
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        help="New disposable directory; existing paths are refused",
    )
    parser.add_argument("--ollama-url", help="Existing local Ollama, no model download")
    parser.add_argument("--embedding-model", default="nomic-embed-text")
    parser.add_argument("--repeat", type=int, default=10)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Payload indexes have no effect")
        asyncio.run(benchmark(args))
