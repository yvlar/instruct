# Instruct IA

[![CI](https://github.com/yvlar/instruct/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/yvlar/instruct/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg)](https://www.python.org/)

Assistant RAG local pour interroger des instructions de travail au format PDF. Instruct IA extrait le texte, recherche les passages pertinents dans Qdrant, puis demande à Qwen de sélectionner ceux qui répondent à la question. Le serveur affiche ces extraits avec leurs références vérifiées.

> [!WARNING]
> Ce projet aide à retrouver de l'information. Il ne remplace jamais une procédure officielle à jour, une formation, une analyse de risques, une consignation ou le jugement d'une personne qualifiée. Ne prenez aucune décision de sécurité uniquement à partir d'une réponse générée.

## Fonctionnalités

- fonctionnement local, sans API d'IA externe;
- écran **Documents** : ajout par bouton ou glisser-déposer, recherche, filtres et pagination;
- tâches locales suivies dans l’interface, remplacement validé avant installation et retrait avec archive;
- ouverture des sources à leur page et dans leur version exacte, avec téléchargement;
- synchronisation incrémentale des PDF texte : ajout, modification et suppression;
- fichiers inchangés ignorés, embeddings par lots et reprise après interruption;
- embeddings locaux avec `nomic-embed-text`;
- recherche hybride locale : Qdrant sémantique et SQLite FTS5 pour les termes exacts;
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

Les [tests de régression RAG et l'évaluation locale facultative](docs/RAG_REGRESSION.md)
couvrent les citations, refus, valeurs exactes, PDF invalides et synchronisations.
`grounded: true` atteste la provenance des extraits affichés, pas leur pertinence
ni l'exactitude métier du document.

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

## Gérer les documents sans commandes curl

Après le démarrage, ouvrez <http://localhost:3000> puis **Documents** :

1. Cliquez sur **Ajouter un PDF** ou glissez un fichier dans la zone d’ajout.
2. Vérifiez son nom et son dossier relatif (par exemple `maintenance`). Cliquez sur
   **Enregistrer le PDF**, puis **Indexer** dans sa ligne.
3. Suivez les étapes connues et le nombre de documents traités. L’interface reste
   accessible pendant le travail. Un seul traitement modifie les documents à la fois.
4. Quand le document est **Disponible**, revenez à **Questions**. Dans les sources,
   cliquez sur **Ouvrir la source · p. N**. Le numéro reste affiché, avec des liens
   d’ouverture et de téléchargement si le lecteur PDF ignore `#page=N` sur mobile.
5. **Remplacer** prépare un nouveau PDF au même chemin. L’ancien fichier reste en
   place pendant la validation et l’indexation. En cas d’échec, utilisez **Réessayer**.
   Les copies des versions déjà indexées sont conservées pour les anciennes sources.
6. **Retirer**, puis la confirmation, archive le fichier hors du dossier indexé et
   retire ses passages actifs. Attendez **Terminé** : une erreur partielle nécessite
   une nouvelle tentative. La synchronisation ne réactive jamais ce chemin retiré.

**Synchroniser les documents** traite aussi les ajouts, modifications et suppressions
faits directement sur le disque. **Réindexer** vérifie un document précis et conserve
l’optimisation incrémentale : aucun nouvel embedding pour un fichier inchangé.

Les états sont **À indexer**, **En cours**, **Disponible**, **Modification détectée**
et **Erreur**. Les détails affichent les empreintes SHA-256 du fichier et de la
version indexée : ce sont des identifiants techniques, **jamais une révision officielle
ni une approbation métier**. Les pages et dates inconnues sont indiquées comme telles.

Les PDF doivent être valides, non chiffrés, non endommagés, et ne pas dépasser
`MAX_PDF_BYTES` (50 Mio par défaut). Un nom existant nécessite un remplacement
explicite ou un autre nom. Les chemins absolus, traversées, composants cachés et liens
symboliques sont refusés. Les fichiers PDF scannés sans texte nécessitent un OCR en amont.

Les documents sont exclus de Git. Les sous-dossiers suggérés sont `instructions`,
`securite`, `maintenance` et `formation`. L’API historique reste utilisable :

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

### Suppressions faites directement sur le disque

Privilégiez **Retirer** dans l’interface pour conserver une archive. Pour une suppression
externe, retirez les fichiers voulus de `documents/`, puis relancez `/api/ingest`. Le retrait
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
retrouver sans ambiguïté les sous-dossiers. Un index non vide sans manifeste compatible
produit `LEGACY_INDEX`; il n'est ni supprimé ni interrogé par la nouvelle version.
La reconstruction dans une **nouvelle collection** est obligatoire :

1. Conservez une sauvegarde des PDF et de Qdrant; arrêtez le backend avec
   `docker compose stop backend`.
2. Dans `.env`, remplacez `QDRANT_COLLECTION=work_instructions` par un nom encore
   inutilisé, par exemple `QDRANT_COLLECTION=work_instructions_hybrid_v3`.
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
| `POST` | `/api/ask` | Extraits vérifiés, claims et sources avec identifiant, version et page |
| `GET` | `/api/documents` | Liste : `q`, `status`, `page`, `page_size`, tâches récentes |
| `PUT` | `/api/documents` | Corps PDF brut, `Content-Type: application/pdf`; `name`, `folder`, `replace_id` explicite |
| `POST` | `/api/documents/sync` | Lance une synchronisation; réponse 202 avec la tâche |
| `POST` | `/api/documents/{id}/index` | Lance l’indexation incrémentale d’un document |
| `POST` | `/api/documents/{id}/remove` | Lance l’archivage et le retrait |
| `POST` | `/api/document-jobs/{id}/retry` | Relance une tâche échouée ou interrompue |
| `GET` | `/api/documents/{id}/source` | Vérifie la `version` et la `page` avant ouverture |
| `GET` | `/api/documents/{id}/file` | PDF exact, paramètres `version`, `page`, `download=true` facultatif |

## Persistance, permissions et reprise

| Emplacement | Contenu | Persistance |
|---|---|---|
| `./documents` → `/documents` | PDF courants, montage désormais en lecture-écriture | Dossier de l’hôte |
| `document_state` → `/state/documents` | Registre SQLite, tâches, remplacements préparés, versions PDF et archives | Volume Docker nommé |
| `index_locks` → `/state/locks` | Verrous de mutation de l’index | Volume Docker nommé |
| `lexical_data` → `/state/lexical` | Index SQLite FTS5, reconstructible depuis Qdrant | Volume Docker nommé |
| `qdrant_data` | Passages et manifestes Qdrant | Volume Docker nommé |
| `ollama_data` | Modèles locaux | Volume Docker nommé |

Sauvegardez ensemble les PDF, `document_state` et Qdrant. `docker compose down` conserve
les volumes; **`down -v` les détruit**, y compris les archives et versions citées.
Ne placez jamais `DOCUMENT_STATE_PATH` à l’intérieur de `DOCUMENTS_PATH`.
Le système de fichiers de l’image backend est en lecture seule; seuls les montages
ci-dessus et `/tmp` sont inscriptibles. Avec les
permissions hôte usuelles, le conteneur écrit en tant que root : les nouveaux fichiers
peuvent appartenir à root sur Linux. Donnez au dossier documentaire les droits adaptés
à votre déploiement; aucun `chmod 777` n’est nécessaire.

Un **unique processus backend** possède le registre. N’utilisez pas `uvicorn --workers`
ni plusieurs répliques : le second processus est refusé. Les tâches s’exécutent dans
un fil de travail unique, sans Redis, Celery ni second modèle. Deux envois HTTP au
maximum sont acceptés simultanément. Au redémarrage, les tâches non terminées passent
à **Traitement interrompu** et peuvent être relancées; les fichiers préparés restent
conservés. `/api/ingest` partage les verrous et refuse les conflits avec les tâches UI.

Un retrait écrit d’abord une exclusion durable. Même si Qdrant échoue ensuite, ce
chemin ne peut plus alimenter la recherche. **Réessayer** termine l’archivage et le
nettoyage; un fichier recopié au même chemin reste exclu. Pour un nouvel ajout après
retrait, choisissez explicitement un autre nom. La restauration d’archive n’a pas
encore d’interface. Les PDF archivés se trouvent sous `archive/<identifiant>/` dans
`document_state`; le registre conserve leur chemin documentaire d’origine.

Les sources utilisent `document_id` et `version`; aucune route de lecture n’accepte
un chemin arbitraire. La copie exacte est servie ou une erreur **410** explique son
indisponibilité. Les anciennes réponses sans version doivent être régénérées après
synchronisation. Aucun cache de réponses n’est introduit : réponses HTTP `no-store`,
sources revérifiées après génération, et réponse affichée effacée après une mutation UI.

`answer` reste une chaîne utilisable par les clients existants. `claims` relie
chaque extrait à ses `source_ids`; `sources` contient uniquement les passages
utilisés et leur `source_id`. `safety_notice` fournit le rappel officiel, même
en cas de refus. L'interface affiche les liens de citation par élément.

**`grounded` valide la provenance des extraits, pas leur vérité ni leur pertinence
sémantique.** Un passage authentique peut être incomplet ou mal interprété.
Consultez [le contrat, les limites et le banc de régression](docs/GROUNDING.md).

## Recherche hybride et migration

La recherche combine les embeddings Qdrant et SQLite FTS5 pour retrouver aussi
les codes exacts, nombres et unités. Les passages conservent les titres, étapes
et pages; le contexte est borné. Les références et extraits cités sont validés
avant de retourner les sources utilisées. `grounded` indique une provenance
validée, pas une garantie d'exactitude. Le score de recherche n'est pas une
probabilité.

**Le schéma 3 exige de reconstruire les index v2 dans une nouvelle collection.**
Les anciens index sont conservés et refusés explicitement jusqu'à cette migration.
Les modalités de synchronisation et de suppression volontaire restent identiques.

Voir [recherche hybride](docs/HYBRID_RETRIEVAL.md) pour les réglages, la migration,
les limites, le benchmark reproductible et les mesures réellement obtenues.

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
| `DOCUMENT_STATE_PATH` | `.instruct-state` | Registre et versions hors du dossier documentaire; Compose impose `/state/documents` |
| `MAX_PDF_BYTES` | `52428800` | Limite serveur par PDF, vérifiée aussi en réception progressive |
| `MIN_SCORE` | `0.35` | Seuil de similarité vectorielle, sans valeur de probabilité de vérité |
| `TOP_K` | `4` | Nombre maximal de passages retenus |
| `RETRIEVAL_CANDIDATES` | `24` | Candidats par méthode avant fusion |
| `CONTEXT_MAX_CHARS` | `8000` | Budget de présélection sérialisée, avant les limites de génération |
| `LEXICAL_INDEX_PATH` | `./data/lexical` | Index SQLite local; Compose utilise `/state/lexical` |
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
- indexation déclenchée depuis l’interface ou l’API, sans surveillance automatique du dossier;
- aucune purge automatique des snapshots, archives ou tâches : surveillez l’espace disque;
- les citations valident la provenance des extraits, sans garantir leur pertinence pour la situation réelle;
- lecture du contenu des PDF pour calculer les empreintes, même sans changement;
- une synchronisation à la fois; `/api/ask` renvoie `INDEX_BUSY` pendant celle-ci;
- déploiement Docker Linux sur un seul hôte et un seul processus backend; les opérations de fichiers utilisent `openat`/`O_NOFOLLOW`; pour Windows et macOS, utilisez Docker;
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

Le parcours navigateur utilise les mêmes API, de vrais PDF synthétiques et Qdrant
embarqué, avec Ollama contrôlé. Il vérifie ordinateur et mobile, ajout → indexation
→ question → source/version/page → téléchargement → retrait → synchronisation :

```bash
(cd frontend && npx playwright install chromium && npm run test:e2e)
```

Le Python contenant les dépendances backend doit être présent dans `PATH`. Aucun
service de production n’est contacté; `backend/tests/browser_app.py` crée un dossier
temporaire et n’est jamais inclus dans l’image backend. Les ports 3000 et 8000 doivent
être libres. Les captures sont dans [`docs/screenshots`](docs/screenshots).

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
