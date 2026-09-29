"""Opt-in real-model evaluation on disposable PDFs and embedded Qdrant."""

import argparse
import asyncio
import json
import tempfile
import time
import warnings
from pathlib import Path

import fitz
import httpx
from app.config import Settings
from app.grounding import NOT_FOUND, normalize
from app.services import KnowledgeBase
from qdrant_client import QdrantClient

DATA_PATH = Path(__file__).with_name("grounding_cases.json")


def load_data():
    return json.loads(DATA_PATH.read_text())


def write_pdfs(directory: Path, documents: dict):
    directory.mkdir(parents=True, exist_ok=True)
    for name, pages in documents.items():
        with fitz.open() as pdf:
            for text in pages:
                page = pdf.new_page()
                remaining = page.insert_textbox(
                    fitz.Rect(60, 60, 535, 760), text, fontsize=12
                )
                if remaining < 0:
                    raise ValueError("Demo text does not fit its PDF page")
            pdf.save(directory / name)


def matches(case: dict, result: dict) -> bool:
    if result["grounded"] != case["grounded"]:
        return False
    if not case["grounded"]:
        return (
            result["answer"] == NOT_FOUND
            and not result["sources"]
            and not result["claims"]
        )
    sources = {s["source_id"]: s for s in result["sources"]}
    for expected in case["evidence"]:
        if not any(
            normalize(expected["contains"]) in claim["text"]
            and any(
                sources[source_id]["document"] == expected["document"]
                and sources[source_id]["page"] == expected["page"]
                for source_id in claim["source_ids"]
            )
            for claim in result["claims"]
        ):
            return False
    return True


class MeasuredHTTP(httpx.AsyncClient):
    def __init__(self):
        super().__init__(timeout=120)
        self.generations = []

    async def post(self, url, **kwargs):
        response = await super().post(url, **kwargs)
        if url.endswith("/api/chat"):
            body = response.json()
            self.generations.append(
                {
                    key: body.get(key)
                    for key in (
                        "total_duration",
                        "load_duration",
                        "prompt_eval_count",
                        "eval_count",
                    )
                }
            )
        return response


async def evaluate(args):
    data = load_data()
    failures = 0
    for case in data["cases"]:
        with tempfile.TemporaryDirectory(prefix="instruct-demo-") as temporary:
            root = Path(temporary)
            write_pdfs(
                root / "pdf",
                {name: data["documents"][name] for name in case["documents"]},
            )
            config = Settings(
                _env_file=None,
                state_path=str(root / "state"),
                documents_path=str(root / "pdf"),
                index_lock_path=str(root / "locks"),
                lexical_index_path=str(root / "lexical"),
                ollama_url=args.ollama_url,
                ollama_model=args.model,
                embedding_model=args.embedding_model,
            )
            http = MeasuredHTTP()
            kb = KnowledgeBase(
                config=config, qdrant=QdrantClient(":memory:"), http=http
            )
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    ingestion = await kb.ingest()
                if ingestion["failed"]:
                    raise RuntimeError(f"Demo ingestion failed: {ingestion['errors']}")
                start = time.perf_counter()
                result = await kb.ask(case["question"])
                elapsed = time.perf_counter() - start
                passed = matches(case, result)
                failures += not passed
                # Residency after the answer is a sample, NOT a peak-memory measurement.
                residency = None
                try:
                    response = await http.get(f"{args.ollama_url}/api/ps")
                    response.raise_for_status()
                    residency = [
                        {"model": m.get("name"), "size_vram": m.get("size_vram")}
                        for m in response.json()["models"]
                    ]
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
                print(
                    json.dumps(
                        {
                            "case": case["id"],
                            "pass": passed,
                            "elapsed_seconds": round(elapsed, 3),
                            "generation_calls": len(http.generations),
                            "ollama": http.generations,
                            "resident_models_after": residency,
                            "result": result,
                        },
                        ensure_ascii=False,
                    )
                )
            finally:
                await kb.close()
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use existing local Ollama models; never pull",
    )
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--embedding-model", default="nomic-embed-text")
    args = parser.parse_args()
    if not args.live:
        parser.error(
            "Use --live to explicitly enable local inference. CI uses pytest doubles."
        )
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
