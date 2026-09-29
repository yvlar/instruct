"""Optional real Ollama/Qdrant evaluation; never downloads a model or starts Docker."""

import argparse
import asyncio
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from app.config import Settings
from app.schemas import Answer
from app.services import KnowledgeBase

from .build_fixtures import ROOT
from .checks import REFUSAL, check_answer, load_cases


async def evaluate(args):
    cases = load_cases()
    report = {
        "chat_model": args.chat_model,
        "embedding_model": args.embedding_model,
        "limits": "Contrôles sur cinq questions fictives; aucune garantie de sécurité "
        "ou d'exactitude. Les cas de panne et sorties hostiles sont testés en CI.",
        "questions": [],
        "cleanup_errors": [],
    }
    with tempfile.TemporaryDirectory(prefix="instruct-eval-") as temp:
        for corpus in dict.fromkeys(case["corpus"] for case in cases):
            documents = Path(temp) / corpus
            shutil.copytree(ROOT / corpus, documents)
            # No caller-provided collection or documents directory: cleanup can only
            # target this run's synthetic corpus and unique disposable collections.
            collection = f"instruct_eval_{uuid.uuid4().hex}"
            config = Settings(
                _env_file=None,
                documents_path=str(documents),
                index_lock_path=str(Path(temp) / "locks"),
                qdrant_url=args.qdrant_url,
                ollama_url=args.ollama_url,
                ollama_model=args.chat_model,
                embedding_model=args.embedding_model,
                qdrant_collection=collection,
                chunk_size=220,
                chunk_overlap=35,
                embedding_batch_size=2,
                top_k=8,
                min_score=args.min_score,
            )
            kb = KnowledgeBase(config=config)
            try:
                ingestion_error = None
                try:
                    result = await kb.ingest()
                    if result["failed"]:
                        ingestion_error = "Ingestion incomplète"
                except Exception as exc:
                    ingestion_error = f"Ingestion impossible ({type(exc).__name__})"
                for case in (c for c in cases if c["corpus"] == corpus):
                    started = time.perf_counter()
                    entry = {
                        "id": case["id"],
                        "question": case["question"],
                        "expected_refusal": case["expected_refusal"],
                        "allow_refusal": case.get("allow_refusal", False),
                        "answer": None,
                        "sources": [],
                        "grounded": None,
                        "refused": None,
                    }
                    failures = [ingestion_error] if ingestion_error else []
                    if not ingestion_error:
                        try:
                            answer = Answer(
                                **await kb.ask(case["question"])
                            ).model_dump()
                            entry.update(answer)
                            entry["refused"] = (
                                answer["answer"] == REFUSAL
                                and answer["grounded"] is False
                                and answer["sources"] == []
                            )
                            failures = check_answer(case, answer, documents)
                        except Exception as exc:
                            failures = [f"Question impossible ({type(exc).__name__})"]
                    entry["duration_seconds"] = round(time.perf_counter() - started, 3)
                    entry["checks_passed"] = not failures
                    entry["failures"] = failures
                    report["questions"].append(entry)
            finally:
                for name in (collection + "__manifest", collection):
                    try:
                        if kb.qdrant.collection_exists(name):
                            kb.qdrant.delete_collection(name)
                    except Exception as exc:
                        report["cleanup_errors"].append(
                            {"collection": name, "error": type(exc).__name__}
                        )
                await kb.close()
    report["passed"] = (
        all(q["checks_passed"] for q in report["questions"])
        and not report["cleanup_errors"]
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--chat-model", default="qwen3:8b")
    parser.add_argument("--embedding-model", default="nomic-embed-text")
    parser.add_argument("--min-score", type=float, default=0.20)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    report = asyncio.run(evaluate(args))
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
