# Instruct IA

[![CI](https://github.com/yvlar/instruct/actions/workflows/ci.yml/badge.svg)](https://github.com/yvlar/instruct/actions/workflows/ci.yml)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg)](https://www.python.org/)

Assistant RAG local pour interroger des instructions de travail au format PDF. Instruct IA extrait le texte, recherche les passages pertinents dans Qdrant, puis demande à Qwen de produire une réponse accompagnée du document, de la page et de l'extrait source.

> [!WARNING]
> Ce projet aide à retrouver de l'information. Il ne remplace jamais une procédure officielle à jour, une formation, une analyse de risques, une consignation ou le jugement d'une personne qualifiée. Ne prenez aucune décision de sécurité uniquement à partir d'une réponse générée.

## Fonctionnalités

- fonctionnement local, sans API d'IA externe;
- ingestion de PDF contenant du texte;
- embeddings locaux avec `nomic-embed-text`;
- recherche vectorielle avec Qdrant;
- génération avec Qwen via Ollama;
- réponses avec document, page, extrait et score de pertinence;
- refus explicite lorsque les documents ne contiennent pas la réponse;
- API FastAPI et interface React/TypeScript;
- déploiement conteneurisé avec Docker Compose.

## Architecture

```text
PDF -> PyMuPDF -> passages -> nomic-embed-text -> Qdrant
                                                    |
Question -> embedding -> recherche sémantique ------+
                                                    |
                                  contexte -> Qwen -> réponse sourcée
```

La description détaillée se trouve dans [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Prérequis

- Git;
- Docker Engine ou Docker Desktop;
- Docker Compose v2 (`docker compose`);
- environ 8 Go de mémoire disponible pour `qwen3:8b`;
- facultatif : GPU NVIDIA, pilote fonctionnel et NVIDIA Container Toolkit.

## Installation rapide

```bash
git clone https://github.com/yvlar/instruct.git
cd instruct
cp .env.example .env
docker compose up -d qdrant ollama
docker compose exec ollama ollama pull qwen3:8b
docker compose exec ollama ollama pull nomic-embed-text
docker compose up -d --build
```

Pour activer explicitement le GPU NVIDIA :

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Vérification :

```bash
curl http://localhost:8000/healthz
```

Ouvrez ensuite <http://localhost:3000>. La documentation interactive de l'API est disponible sur <http://localhost:8000/docs>.

## Ajouter des documents

Déposez uniquement des documents que vous êtes autorisé à traiter dans l'un des dossiers suivants :

```text
documents/instructions/
documents/securite/
documents/maintenance/
documents/formation/
```

Les documents sont exclus de Git par défaut. Lancez ensuite l'indexation :

```bash
curl -X POST http://localhost:8000/api/ingest
```

Exemple de question :

```bash
curl -X POST http://localhost:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"Quelle est la procédure de démarrage?"}'
```

## API

| Méthode | Route | Description |
|---|---|---|
| `GET` | `/healthz` | Vérifie que l'API répond |
| `POST` | `/api/ingest` | Indexe les PDF présents dans `documents/` |
| `POST` | `/api/ask` | Retourne une réponse fondée sur les passages retrouvés |

## Configuration

Les réglages se trouvent dans `.env`. Ne publiez jamais ce fichier.

| Variable | Valeur par défaut | Rôle |
|---|---|---|
| `OLLAMA_MODEL` | `qwen3:8b` | Modèle de génération |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Modèle d'embeddings |
| `QDRANT_COLLECTION` | `work_instructions` | Collection vectorielle |
| `MIN_SCORE` | `0.35` | Seuil minimal de pertinence |
| `TOP_K` | `6` | Nombre maximal de passages récupérés |

## Confidentialité et sécurité

En configuration par défaut, les traitements restent sur la machine locale. Les ports sont liés à `127.0.0.1` et ne doivent pas être exposés directement sur Internet. L'application ne possède ni authentification ni gestion multiutilisateur.

Avant toute publication :

- vérifiez que les PDF, `.env`, clés et données Qdrant ne sont pas suivis par Git;
- retirez toute instruction confidentielle ou propriété d'un employeur;
- n'ajoutez aucun document dont vous ne détenez pas les droits de diffusion;
- consultez [`docs/SAFETY.md`](docs/SAFETY.md) et [`SECURITY.md`](SECURITY.md).

## Limites actuelles

- PDF texte seulement; les documents numérisés nécessitent un OCR;
- indexation manuelle;
- suppression d'un fichier source non répercutée automatiquement dans Qdrant;
- aucun contrôle d'accès;
- pas conçu ni certifié comme système de sécurité industrielle.

## Contribuer

Les contributions sont bienvenues. Consultez [`CONTRIBUTING.md`](CONTRIBUTING.md), le [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) et les issues existantes avant de commencer.

## Licence

Le code source est distribué sous licence [MIT](LICENSE), Copyright © 2026 Yves Larivière. Cette licence ne s'étend pas aux documents que les utilisateurs chargent dans l'application.
