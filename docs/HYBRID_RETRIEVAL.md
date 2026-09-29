# Recherche locale hybride

## Choix et contrats

Le point de départ est `main` au commit `ee5cb73`, après fusion de la PR #5.
L'indexation incrémentale, les manifestes, les verrous et les tests de reprise y
étaient présents. La validation des citations n'y était **pas** implémentée :
`grounded` dépendait seulement de l'existence d'un résultat Qdrant.
La branche intègre maintenant `main` au commit `ff9c22b` (PR #7) : sa sélection
structurée de passages, son rendu extractif et ses tests de régression sont
conservés. Le module de citations parallèle initial de cette PR a été supprimé.

Qdrant et `nomic-embed-text` sont conservés. SQLite **FTS5**, disponible dans le
Python de l'image backend, fournit la recherche lexicale sur disque, sans serveur,
dépendance Python supplémentaire, modèle ni VRAM supplémentaires. Le dépôt public
ne contient aucun PDF utilisateur : le volume privé est inconnu. Ce choix vise le
même corpus local modeste que le manifeste actuel, à confirmer sur les documents
réels. Référence technique : [SQLite FTS5 et BM25](https://www.sqlite.org/fts5.html).

La recherche suit ces étapes :

1. Lire les manifestes sous le verrou partagé; vérifier le modèle et la présence
   de tous les passages actifs dans SQLite.
2. Faire **un seul** embedding de la question; demander jusqu'à 24 candidats
   sémantiques Qdrant au-dessus de `MIN_SCORE`.
3. Rechercher jusqu'à 24 candidats FTS5, avec BM25. Les révisions actives sont
   filtrées **avant** la limite. Une première requête conjonctive réserve des
   candidats portant toutes les ancres exactes; une seconde recherche les mots.
4. Prioriser les passages contenant toutes les ancres exactes demandées, puis
   fusionner les rangs par RRF : somme de `1 / (60 + rang)` des deux listes.
   Une égalité est départagée par document, page et identifiant. Dédupliquer les
   UUID, puis les textes identiques sur la même page et la même révision.
5. Retenir au maximum `TOP_K` passages entiers sous `CONTEXT_MAX_CHARS`.
6. Effectuer un seul appel de génération structurée; valider les références et
   citations. Aucun second appel systématique au LLM, reranker ou modèle ajouté.

Les codes alphanumériques et composés (`AB-204/X`, `12ABC`), nombres signés,
décimales et associations valeur/unité ont des jetons exacts encodés. La virgule
et le point décimal sont équivalents; accents, casse, apostrophes typographiques et
variantes de tirets sont normalisés pour la recherche. Le **texte original reste
intact** dans les passages et les sources. `12,5 bar`, `12,6 bar` et `12,5 kPa`
restent distincts. « arrêt d'urgence » dispose aussi d'une ancre de locution.
Les unités composées reconnues incluent bar, kPa, MPa, psi, mm, cm, m, kg, g, l,
ml, Hz, V, A, rpm, tr/min, °C, %, N·m. Les autres unités restent recherchables
comme mots, mais n'ont pas toutes une ancre valeur/unité. Pas de conversion
d'unités, de compréhension de tableaux, ni d'équivalence numérique générale.

Les scores RRF servent au classement, **jamais à prouver la réponse**. `score`
reste dans l'API pour compatibilité, mais ne représente plus un cosinus ou un
pourcentage; le frontend n'affiche donc plus de pourcentage de pertinence.

## Extraction et contexte

PyMuPDF extrait les blocs texte triés de chaque page. Les frontières de blocs et
les retours à la ligne restent visibles. Chaque étape numérotée ou à puce démarre
un bloc distinct. Une heuristique conserve les titres courts et les répète avec
les étapes suivantes sur la même page. Seuls les paragraphes ou étapes trop longs
sont découpés avec chevauchement; aucune fusion entre pages.

Le découpage préserve davantage de structure mais peut produire **davantage de
petits vecteurs**. Il ne promet donc pas une réduction du temps de première
indexation. Les titres sont reconnus par heuristique, pas par analyse sémantique;
les mises en page multicolonnes et les tableaux complexes restent imparfaits.
Les PDF numérisés nécessitent toujours un OCR externe.

Le budget compte exactement les caractères du contexte sérialisé (identifiants,
métadonnées, texte et séparateurs compris), pas les tokens Ollama. Le prompt fixe
et la question, déjà limitée à 1 000 caractères par l'API, s'ajoutent à ce budget.
La conversion caractères/tokens dépend du modèle. Aucun passage n'est tronqué.
Si le meilleur passage ne tient pas, l'assistant refuse; il ne le remplace pas par
une valeur voisine plus courte. Augmenter le budget ou réduire `CHUNK_SIZE` puis
réindexer permet de traiter ce cas. Les passages après la première limite ne sont
pas envoyés, même s'ils sont plus courts.

## Révisions, publication et reprise

`<LEXICAL_INDEX_PATH>/<hash URL Qdrant + collection>.sqlite3` contient les passages
avec le même UUID, document, page, révision et empreinte que Qdrant, plus les jetons
FTS5. Le cache SQLite est limité à 4 Mio par connexion. Compose partage un volume
`lexical_data` entre les workers du même projet; le verrou existant protège les
écritures. Tous les workers doivent partager **les deux** dossiers de verrous et
SQLite. Plusieurs hôtes indépendants ne sont pas pris en charge.

Chaque lot est écrit dans Qdrant, puis dans une transaction SQLite, **avant** la
publication du manifeste Qdrant. Ce manifeste reste l'unique source de vérité.
Une interruption laisse des passages préparés invisibles dans les deux index.
Une réponse perdue lors de la publication est récupérée en relisant le manifeste;
la révision publiée dispose déjà de toutes ses entrées lexicales. Les mêmes UUID
sont réutilisés à la relance, donc pas de doublons.

Une modification publie une nouvelle révision; une suppression retire d'abord
le manifeste. Le filtre de recherche exclut immédiatement les anciennes versions,
même si leur nettoyage physique échoue (`cleanup_pending: true`). Une ingestion
réussie nettoie Qdrant et SQLite. Les suppressions SQLite libèrent des pages
réutilisables, mais ne réduisent pas nécessairement le fichier disque.

Avant de sauter un PDF inchangé, l'ingestion vérifie aussi les nombres de passages
lexicaux par révision. Une base SQLite perdue ou incomplète est réparée depuis les
passages Qdrant actifs par lots de 128, **sans extraction ni nouvel embedding**.
La recherche refuse explicitement avec `LEXICAL_INDEX_INCOMPLETE` tant que cette
réparation n'a pas été effectuée. Il n'y a pas de bascule silencieuse vers un seul
index. Une corruption physique SQLite nécessite de sauvegarder/écarter le fichier
concerné, backend arrêté, puis de relancer l'ingestion.

## Migration obligatoire vers le schéma 3

Le format/découpage a changé : un manifeste v2 renvoie `INDEX_INCOMPATIBLE`, un
ancien index sans manifeste renvoie `LEGACY_INDEX`. Aucune ancienne collection
n'est modifiée par une migration implicite.

1. Sauvegarder les PDF et les collections Qdrant, puis `docker compose stop backend`.
2. Choisir dans `.env` une collection **neuve**, par exemple
   `QDRANT_COLLECTION=work_instructions_hybrid_v3`.
3. Exécuter `docker compose up -d --build backend`, puis
   `curl -X POST http://localhost:8000/api/ingest`.
4. Vérifier `failed: 0` et `cleanup_pending: false`. Tester un code exact, une
   valeur avec unité, une paraphrase et une question absente avec les PDF réels.
5. Relancer `/api/ingest` : tous les fichiers doivent être `unchanged`, `chunks: 0`.
   Vérifier aussi une modification puis une suppression d'un PDF d'essai.

Conserver l'ancien code et les anciennes collections pour un retour arrière;
restaurer ensemble l'ancienne version du backend et son nom de collection.
N'effacer l'ancien index qu'après validation. Un changement ultérieur de taille
ou chevauchement déclenche la synchronisation des deux index par nouvelle révision,
sans mélanger deux versions d'un même document. Changer le modèle d'embeddings ou
son digest demande toujours une nouvelle collection.

## Réponses et citations

L'API conserve `answer`, `grounded`, `sources`, ainsi que document/page/extrait/score.
Chaque source conserve `passage_id` et ajoute `revision` et `fingerprint`.
Le protocole fusionné sur `main` reste inchangé : le modèle retourne uniquement
`status` et `answer: [{source_id, quote}]`. `grounding.py` valide les identifiants,
les extraits exacts et les limites contre les seuls passages effectivement envoyés.
Les métadonnées viennent du stockage; aucune prose libre n'est rendue.
Les sources complètes conservent les blocs originaux et les UUID Qdrant.
Les documents différents restent distincts, et le rappel de version est conservé
dans `safety_notice`. Voir [GROUNDING.md](GROUNDING.md) pour le contrat actuel.

Après génération, les révisions sélectionnées sont revérifiées sous verrou. Un
document retiré ou remplacé pendant cet appel entraîne un refus. Un verrou encore
occupé par une ingestion produit `INDEX_BUSY`. La recherche hybride ne réintroduit
pas de citation obsolète, y compris quand des anciens points physiques subsistent.

La vérification PDF des tests et de l'évaluation accepte les retours à la ligne
conservés et un titre répété avant une étape plus bas sur la page. Chaque bloc doit
exister dans l'ordre sur la page citée; seuls les espaces sont normalisés, jamais
les valeurs, unités ou signes. Les textes d'autres pages et valeurs falsifiées
continuent d'être refusés. Les bases SQLite de l'évaluation sont temporaires et
isolées, comme ses collections Qdrant.

Le modèle peut encore sélectionner un passage hors sujet. La provenance vérifiée
ne constitue donc pas une preuve de pertinence ou une autorisation d'exécuter une
procédure. Un rang élevé n'est jamais utilisé pour déclarer une réponse vraie.

## Réglages

| Variable | Défaut | Effet |
|---|---:|---|
| `LEXICAL_INDEX_PATH` | `./data/lexical` hors Compose | Dossier persistant; Compose impose `/state/lexical` |
| `RETRIEVAL_CANDIDATES` | 24 | Maximum par méthode, 1 à 100 |
| `TOP_K` | 4 | Maximum envoyé au modèle, 1 à 20 |
| `CONTEXT_MAX_CHARS` | 8000 | Budget du contexte sérialisé, 512 à 64000 caractères |
| `MIN_SCORE` | 0,35 | Seuil sémantique uniquement; ne filtre pas les résultats lexicaux |
| `CHUNK_SIZE` | 1400 | Maximum visé par passage, titre répété inclus |
| `CHUNK_OVERLAP` | 250 | Maximum demandé; limité au quart du fragment pour conserver une progression utile |
| `EMBEDDING_BATCH_SIZE` | 16 | Lot inchangé, réglable de 1 à 128 |

Pour le portable 32 Go / RTX 4060 8 Go, commencer par ces valeurs. SQLite n'utilise
pas le GPU; les modèles Ollama restent le principal coût mémoire. Aucun chiffre
de consommation GPU/RAM réelle n'a été mesuré dans cet environnement. Des index
plus volumineux et davantage de petits passages augmentent le disque et les
embeddings initiaux; l'incrémental évite ensuite de les recalculer sans changement.
Le contrôle des manifestes, des comptes SQLite et la table des révisions actives
ont un coût croissant avec le corpus. Pas de garantie de latence à grande échelle.

## Benchmark reproductible

Depuis `backend/`, avec les dépendances de développement installées :

```bash
python -m benchmarks.compare_retrieval --output ../benchmark-results/demo
# Sur la machine locale, avec le modèle déjà installé (aucun téléchargement) :
python -m benchmarks.compare_retrieval \
  --ollama-url http://localhost:11434 --embedding-model nomic-embed-text \
  --repeat 10 --output ../benchmark-results/real-local
```

Le répertoire de sortie doit être neuf. Le script produit deux PDF fictifs (trois
pages), les index **isolés** et `report.json`. Il ne touche jamais les collections
de l'application et ne fait aucun appel de génération. Les sept questions et
passages attendus sont dans `backend/benchmarks/questions.json`. La base comparative
reproduit l'extraction aplatie et la recherche sémantique de `ee5cb73`; les deux
variantes utilisent le même mécanisme incrémental et un maximum comparable de
quatre résultats (le défaut historique de `TOP_K` était six). La variante hybride
utilise le nouveau découpage. La comparaison mesure donc l'ensemble des changements,
pas uniquement l'apport isolé de FTS5.

Le mode par défaut utilise des embeddings **contrôlés de sept dimensions**,
délibérément incapables de distinguer certains codes/valeurs. Les opérations PDF,
SQLite et Qdrant embarqué sont réelles. Cela mesure le fonctionnement du pipeline
et ses frais locaux, **pas** la qualité de `nomic-embed-text`, la latence Ollama,
les besoins GPU ni la taille d'un serveur Qdrant. Le mode `--ollama-url` permet de
mesurer les embeddings réels; Qdrant demeure embarqué, ce qui reste une limite.
Les paramètres sont consignés dans le script; le digest et la dimension utilisés
figurent dans le rapport. Les appels `/api/tags` et l'embedding de question sont
inclus dans la durée de recherche. Les durées de génération ne sont pas mesurées.

### Mesures du 29 septembre 2026

Mode contrôlé, Python 3.12.14, PyMuPDF 1.26.4, SQLite 3.53.1, Linux x86_64.
Deux PDF fictifs, trois pages, dix recherches par question. Indexation mesurée
une seule fois, base sémantique exécutée en premier : bruit et effet de cache
possibles. Aucune conclusion de gain de vitesse ne découle de cette exécution.
Rapport brut : [`hybrid-benchmark-controlled.json`](hybrid-benchmark-controlled.json).

| Mesure | Base sémantique | Hybride |
|---|---:|---:|
| Indexation initiale (ms) | 14.06 | 12.51 |
| Synchronisation inchangée (ms) | 0.99 | 1.64 |
| Passages | 3 | 7 |
| Disque Qdrant embarqué, manifestes inclus (octets) | 25455 | 33625 |
| Disque SQLite lexical (octets) | 0 | 32768 |

| Question | Rang attendu base → hybride | Recherche médiane base (ms) | Hybride (ms) |
|---|---|---:|---:|
| paraphrase | 1 → 1 | 0.740 | 1.979 |
| part | 2 → 1 | 0.634 | 1.694 |
| pressure | 2 → 1 | 0.557 | 2.026 |
| unit | 1 → 1 | 0.615 | 1.864 |
| emergency | 1 → 1 | 0.527 | 1.775 |
| machine | 1 → 1 | 0.547 | 1.746 |
| absent | aucun résultat → aucun résultat | 0.500 | 1.374 |

Tous les passages attendus sont dans les quatre premiers résultats des deux
variantes. Les cas code et pression passent du rang 2 au rang 1 dans ce scénario
contrôlé; cela ne constitue pas une estimation du gain de qualité en production.
La synchronisation inchangée écrit zéro passage dans les deux variantes.

## Vérifications

La suite couvre les paraphrases, codes, nombres signés, unités voisines, titres,
étapes/pages, déduplication, budget, modifications/suppressions, interruptions,
perte/réparation de SQLite, migrations, refus et citations invalides. Ollama est
simulé; PDF, SQLite et filtres Qdrant embarqués sont réellement exécutés. Le test
HTTP facultatif nécessite des services locaux et reste désactivé sinon.

Après intégration de `main` (`ff9c22b`), la suite backend compte **105 tests réussis**
et un test HTTP facultatif ignoré. Elle conserve les régressions de citations,
refus, sorties hostiles et modifications pendant génération de la PR #7.
La compilation frontend et les configurations Compose standard/GPU ont été
vérifiées localement avec Compose 2.39.4. La construction Docker a été tentée
mais le socket du daemon est inaccessible dans cet environnement; le job Docker
de la CI construit les deux images applicatives. Les contrôles de CI portent
sur le commit de la PR et sont consultables directement sur GitHub.

## Intégration des citations par élément

Le parcours `/api/ask` utilise maintenant `grounding.py` et le protocole strict
`status` + `answer: [{source_id, quote}]`, décrit dans [GROUNDING.md](GROUNDING.md).
Il conserve le classement, l'UUID Qdrant `passage_id`, les révisions, les empreintes,
les blocs des extraits et le contrôle de fraîcheur après génération.
`claims` relie chaque extrait choisi à sa source; aucune prose libre n'est acceptée.
Les contradictions et injections évidentes sont refusées. Le budget de génération
s'ajoute à `CONTEXT_MAX_CHARS`; on ne saute jamais le premier passage trop long
pour présenter une valeur voisine plus courte. Le score affiché est un rang RRF,
pas une similarité vectorielle ou une probabilité de vérité.
