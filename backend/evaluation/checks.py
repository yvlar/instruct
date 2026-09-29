"""Observable checks, shared by deterministic CI and real-service evaluation.

These checks compare citations with PDF pages, not with model assertions. They
cannot establish semantic relevance or detect every prompt injection.
"""

import json
import re
from pathlib import Path

import fitz

ROOT = Path(__file__).parent
REFUSAL = "Information non trouvée dans les instructions disponibles."


def load_cases():
    return json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def check_answer(case: dict, answer: dict, documents: Path) -> list[str]:
    """Return explicit failures; an empty list means only these checks passed."""
    failures = []
    refused = (
        answer.get("grounded") is False
        and answer.get("answer") == REFUSAL
        and answer.get("sources") == []
    )
    if case["expected_refusal"] or (case.get("allow_refusal") and refused):
        return [] if refused else ["Refus explicite sans citation attendu"]
    if answer.get("grounded") is not True or not answer.get("sources"):
        return ["Réponse documentée attendue"]
    sources = answer["sources"]
    actual = {(s["document"], s["page"]) for s in sources}
    if actual != {tuple(source) for source in case["expected_sources"]}:
        failures.append("Document ou page inattendu")
    ids = [s.get("passage_id") for s in sources]
    if not all(ids) or len(set(ids)) != len(ids):
        failures.append("Identifiants de passage absents ou dupliqués")
    for number, source in enumerate(sources, 1):
        path = (documents / source["document"]).resolve()
        if not path.is_relative_to(documents.resolve()) or not path.is_file():
            failures.append("Document cité inexistant")
            continue
        try:
            with fitz.open(path) as pdf:
                page = source["page"]
                if not isinstance(page, int) or not 1 <= page <= len(pdf):
                    failures.append("Page citée inexistante")
                    continue
                text = normalize(pdf[page - 1].get_text())
            excerpt = source["excerpt"]
            if not excerpt or excerpt not in text:
                failures.append("Extrait absent de la page citée")
            marker = f"[{number}] {source['document']}, p. {page}"
            if marker not in answer["answer"] or excerpt not in answer["answer"]:
                failures.append("Citation sans passage correspondant dans la réponse")
        except (ValueError, RuntimeError):
            failures.append("PDF cité illisible")
    for text in case["required_text"]:
        if text not in answer["answer"]:
            failures.append(f"Texte attendu absent : {text}")
    for text in case["forbidden_text"]:
        if text in answer["answer"]:
            failures.append(f"Texte interdit présent : {text}")
    return failures
