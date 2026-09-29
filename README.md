# Instruct IA

[![CI](https://github.com/yvlar/instruct/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/yvlar/instruct/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg)](https://www.python.org/)

Assistant RAG local pour interroger des instructions de travail au format PDF. Instruct IA extrait le texte, recherche les passages pertinents dans Qdrant, puis demande à Qwen de produire une réponse accompagnée du document, de la page et de l'extrait source.

> [!WARNING]
> Ce projet aide à retrouver de l'information. Il ne remplace jamais une procédure officielle à jour, une formation, une analyse de risques, une consignation ou le jugement d'une personne qualifiée. Ne prenez aucune décision de sécurité uniquement à partir d'une réponse générée.

## Fonctionnalités

- fonctionnement local, sans API d'IA externe;
- synchronisation incrémentale des PDF texte : ajout, modification et suppression;
- fichiers inchangés ignorés, embeddings par lots et reprise après interruption;
- embeddings locaux avec `nomic-embed-text`;
- recherche vectorielle avec Qdrant;
- génération avec Qwen via Ollama;
- réponses extractives avec citations par élément, document, page et passage vérifiés;
- refus prudent si les passages sont jugés insuffisants, ambigus ou si les citations sont invalides;
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

Chaque synchronisation compare le contenu SHA-256 et les paramètres d'indexation.
Un PDF inchangé ne subit aucune extraction, aucun embedding ni aucun nouvel upsert.
Les sources affichent le chemin relatif, par exemple `maintenance/fiche.pdf`.
Un déplacement de fichier est traité comme un ajout puis une suppression.

Exemple de résultat :

```json
{
  "documents": 3, "chunks": 12,
  "added": 1, "modified": 1, "unchanged": 1, "deleted": 1,
  "failed": 0, "errors": [], "cleanup_pending": false
}
```

`documents` reste le nombre de PDF trouvés. `chunks` compte les passages publiés
pendant cet appel; il vaut **0** si tout est inchangé. Les anciens champs restent
présents. Les erreurs par document sont retournées dans `errors` sous la forme
`{"document":"maintenance/fiche.pdf","code":"EXTRACTION_FAILED"}` sans contenu
interne de l'exception. Une réponse HTTP 200 peut être partielle : vérifiez
`failed == 0` et `cleanup_pending == false`. Les erreurs globales (dossier absent,
index incompatible, service indisponible, index occupé) retournent HTTP 503.

Une nouvelle version ne devient visible qu'après l'écriture complète de ses
passages. En cas d'échec, l'ancienne version reste stockée. Les passages incomplets
ne sont jamais utilisés par `/api/ask`. Relancez simplement la même commande après
une interruption. `cleanup_pending: true` signale un nettoyage physique à reprendre;
les anciennes versions sont déjà exclues des réponses.

### Supprimer des PDF

Retirez les fichiers voulus de `documents/`, puis relancez `/api/ingest`. Le retrait
de leurs passages est effectué pendant cette synchronisation. Un dossier absent,
illisible, ou vide alors que des documents sont indexés bloque les suppressions.
Les liens symboliques vers des PDF ou des sous-dossiers sont refusés.

Pour supprimer **volontairement tous les PDF de l'index**, retirez d'abord les PDF
du dossier, conservez le dossier présent et lisible, puis utilisez explicitement :

```bash
curl -X POST 'http://localhost:8000/api/ingest?allow_empty=true'
```

Cette option autorise uniquement le cas vide; elle ne contourne ni une erreur de
lecture, ni une migration, ni une indexation concurrente. Si des PDF restent dans
le dossier, ils sont synchronisés normalement. Aucun appel Ollama n'est nécessaire
pour vider un index dont le dossier source est vide.

### Migrer un index existant ou changer de modèle d'embeddings

Les anciens points ne contiennent que le nom du fichier; il est impossible de
retrouver sans ambiguïté les sous-dossiers. Un index non vide sans manifeste v2
produit `LEGACY_INDEX`; il n'est ni supprimé ni interrogé par la nouvelle version.
La reconstruction dans une **nouvelle collection** est obligatoire :

1. Conservez une sauvegarde des PDF et de Qdrant; arrêtez le backend avec
   `docker compose stop backend`.
2. Dans `.env`, remplacez `QDRANT_COLLECTION=work_instructions` par un nom encore
   inutilisé, par exemple `QDRANT_COLLECTION=work_instructions_v2`.
3. Exécutez `docker compose up -d --build backend` puis l'appel `/api/ingest`.
4. Vérifiez `failed: 0`, `cleanup_pending: false`, puis une question avec ses sources.
   Relancez l'ingestion et vérifiez `unchanged` et `chunks: 0`.

L'ancienne collection reste intacte jusqu'à une suppression manuelle décidée après
validation. Pour revenir à l'ancien format, restaurez aussi l'ancienne version du
backend; changer seulement le nom de collection ne suffit pas. Pendant la
reconstruction, les questions peuvent répondre `INDEX_BUSY`.

La même procédure s'applique à un changement d'`EMBEDDING_MODEL` ou de son digest
Ollama (même nom, nouveaux poids). Le refus `EMBEDDING_MODEL_CHANGED` évite de
mélanger des espaces vectoriels. Modifier `CHUNK_SIZE`, `CHUNK_OVERLAP` ou la version
d'extraction entraîne une réindexation lors de la prochaine synchronisation.
Modifier la taille des lots ou le modèle de conversation ne la déclenche pas.

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
| `POST` | `/api/ingest` | Synchronise les PDF; `?allow_empty=true` autorise un dossier volontairement vidé |
| `POST` | `/api/ask` | Retourne des extraits avec citations vérifiées, ou un refus explicite |

`answer` reste une chaîne utilisable par les clients existants. `claims` relie
chaque extrait à ses `source_ids`; `sources` contient uniquement les passages
utilisés et leur `source_id`. `safety_notice` fournit le rappel officiel, même
en cas de refus. L'interface affiche les liens de citation par élément.

**`grounded` valide la provenance des extraits, pas leur vérité ni leur pertinence
sémantique.** Un passage authentique peut être incomplet ou mal interprété.
Consultez [le contrat, les limites et le banc de régression](docs/GROUNDING.md).

## Configuration

Les réglages se trouvent dans `.env`. Ne publiez jamais ce fichier.

| Variable | Valeur par défaut | Rôle |
|---|---|---|
| `OLLAMA_MODEL` | `qwen3:8b` | Modèle de génération |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Modèle d'embeddings |
| `QDRANT_COLLECTION` | `work_instructions` | Collection de passages; manifeste associé dans `<nom>__manifest` |
| `EMBEDDING_BATCH_SIZE` | `16` | Passages par appel Ollama et écriture Qdrant, de 1 à 128 |
| `CHUNK_SIZE` | `1400` | Taille cible en caractères, de 100 à 16000 |
| `CHUNK_OVERLAP` | `250` | Chevauchement, inférieur à `CHUNK_SIZE` |
| `INDEX_LOCK_PATH` | `/tmp/instruct-locks` | Verrous locaux; Compose impose le volume partagé `/state/locks` |
| `MIN_SCORE` | `0.35` | Seuil de similarité vectorielle, sans valeur de probabilité de vérité |
| `TOP_K` | `6` | Nombre maximal de passages récupérés |
| `MAX_CONTEXT_CHARS` | `3200` | Plafond du texte fourni au modèle; budget UTF-8 conservateur supplémentaire |
| `MAX_PASSAGE_CHARS` | `1400` | Passage entier maximum; les passages trop longs sont omis |
| `MAX_ANSWER_CHARS` | `1600` | Longueur cumulée maximale des extraits sélectionnés |
| `MAX_RESPONSE_CHARS` | `6000` | Taille maximale du JSON accepté |
| `OLLAMA_NUM_CTX` | `4096` | Fenêtre de contexte en tokens |
| `OLLAMA_NUM_PREDICT` | `768` | Tokens de sortie maximum, JSON compris |

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
- suppression répercutée au prochain appel manuel à `/api/ingest`;
- lecture du contenu des PDF pour calculer les empreintes, même sans changement;
- une synchronisation à la fois; `/api/ask` renvoie `INDEX_BUSY` pendant celle-ci;
- déploiement local sur un seul hôte : tous les processus doivent partager les
  mêmes verrous; plusieurs hôtes indépendants ne sont pas pris en charge;
- une copie temporaire sur disque par PDF modifié; mémoire d'extraction limitée
  principalement à une page, plus un lot de vecteurs;
- index et filtres de recherche gardent les métadonnées des documents en mémoire;
  cette approche vise un corpus local, pas des millions de documents;
- aucun contrôle d'accès;
- pas conçu ni certifié comme système de sécurité industrielle.

## Tests

```bash
pip install -r backend/requirements-dev.txt
(cd backend && python -m pytest -q)
(cd frontend && npm ci && npm run build)
cp .env.example .env  # uniquement si vous n'avez pas déjà de configuration
docker compose config --quiet
```

Les tests isolés simulent Ollama, les fichiers et les erreurs de Qdrant; ils utilisent
également le moteur Qdrant embarqué pour vérifier les vrais filtres. Un test avec
PDF réel et Qdrant persistant vérifie la reprise après redémarrage. Aucun modèle
n'est téléchargé. Pour le test HTTP facultatif, démarrez vos services locaux avec
un modèle d'embeddings **déjà installé**, puis :

```bash
cd backend
INSTRUCT_TEST_QDRANT_URL=http://localhost:6333 \
INSTRUCT_TEST_OLLAMA_URL=http://localhost:11434 \
INSTRUCT_TEST_EMBEDDING_MODEL=nomic-embed-text \
python -m pytest -q tests/test_integration.py
```

Le test HTTP utilise des PDF synthétiques et des collections temporaires dédiées,
qu'il supprime ensuite. Il ne lance pas le modèle de conversation.

## Contribuer

Les contributions sont bienvenues. Consultez [`CONTRIBUTING.md`](CONTRIBUTING.md), le [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) et les issues existantes avant de commencer.

## Licence

Le code source est distribué sous licence [MIT](LICENSE), Copyright © 2026 Yves Larivière. Cette licence ne s'étend pas aux documents que les utilisateurs chargent dans l'application.
