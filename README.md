# Instruct IA

Assistant RAG 100 % local pour interroger des instructions de travail PDF. Les réponses sont générées par Qwen via Ollama et fondées sur les passages retrouvés dans Qdrant, avec document, page et extrait.

## Prérequis

- Docker Desktop avec Docker Compose
- NVIDIA Container Toolkit pour l'accélération GPU (ou retirer la réservation GPU)
- Environ 8 Go de VRAM pour `qwen3:8b`

## Démarrage

```bash
cp .env.example .env
docker compose up -d qdrant ollama
docker compose exec ollama ollama pull qwen3:8b
docker compose exec ollama ollama pull nomic-embed-text
docker compose up -d --build
```

Déposer les PDF dans les sous-dossiers de `documents/`, puis lancer l'indexation :

```bash
curl -X POST http://localhost:8000/api/ingest
```

Ouvrir ensuite <http://localhost:3000>. La documentation API est disponible sur <http://localhost:8000/docs>.

## API

- `GET /healthz` : état de l'API
- `POST /api/ingest` : indexe ou actualise les PDF
- `POST /api/ask` avec `{"question":"..."}` : réponse sourcée

## Confidentialité et sécurité

Tout fonctionne sur la machine locale. Les PDF ne sont ni copiés dans l'image Docker ni envoyés à une API externe. Ce logiciel aide à retrouver une instruction; il ne remplace pas les procédures officielles, la formation, la consignation ou le jugement d'une personne qualifiée.

## Limites V1

- PDF texte seulement; ajouter un OCR pour les documents numérisés.
- L'indexation est manuelle et idempotente par contenu.
- Aucun système d'authentification; ne pas exposer les ports sur Internet.

