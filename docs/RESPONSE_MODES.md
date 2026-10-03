# Modes de réponse locaux

Cette évolution repose sur le socle comptes/droits de la PR #11. Elle doit être
intégrée après ce socle. Aucun service cloud ni modèle supplémentaire n’est requis.

## Utilisation

Le sélecteur, au-dessous de la question, propose :

- **Rapide** (défaut) : une génération directe; `think: false` sur un Qwen3 compatible.
- **Réflexion** : une génération avec `think: true`, pour examiner plusieurs
  passages. Le texte final reste **extractif**, avec les mêmes validations strictes
  de provenance que Rapide; aucune reformulation libre, valeur calculée ou étape
  ajoutée. Les citations ne garantissent pas à elles seules la pertinence.
- **Recherche seulement** : recherche hybride et extraits complets, document,
  page et ouverture de la version exacte. Aucun appel `/api/chat`, aucune
  vérification du modèle génératif dans ce parcours; l’embedding reste possible.
  Ces résultats ne constituent pas une réponse validée (`grounded: false`).

Après Rapide, **Approfondir** réutilise la question enregistrée lors de la première
requête, même si le champ a depuis été modifié. Il effectue une nouvelle recherche
avec les droits et révisions actuels, puis au maximum une génération Réflexion.
La première réponse reste visible pendant l’attente et en cas d’erreur; la seconde
apparaît dans une carte distincte. Les clics répétés pendant une requête sont
ignorés. Aucun approfondissement automatique ni chaîne de messages assistant.
Une révocation/session expirée peut effacer les résultats affichés; elle prime
sur leur conservation pendant l’attente.

## API

Session, Origin et jeton CSRF existants requis, y compris pour Recherche seulement.

```json
{"question":"Quel réglage pour DEMO-42?","mode":"reflection"}
```

`POST /api/ask` accepte `fast`, `reflection`, `search`; mode omis = `fast`.
Toute autre valeur, y compris `null`, est rejetée (422). Les champs de réponse
existants sont conservés avec `mode` et `kind: "answer" | "search"`. En recherche,
`answer` est vide, `claims` vide et `sources` contient les passages, pas des preuves
suffisantes d’une réponse. Les liens sont construits par le serveur.

`GET /api/response-modes` retourne `default` et, pour chaque mode, `available` et
`reason`. Les options indisponibles restent visibles et désactivées, avec une
explication. Il n’existe aucun remplacement automatique d’un mode par un autre. Si Ollama
refuse effectivement un mode (HTTP 400), celui-ci est désactivé dans le cache des
capacités et l’interface actualise ses options après l’erreur; aucune génération
supplémentaire n’est lancée.

Erreurs explicites : `MODE_UNAVAILABLE` ou `MODEL_REJECTED_MODE` (422),
`QUEUE_FULL` (429), `QUEUE_TIMEOUT` (503), `REQUEST_TIMEOUT` (504),
`INCOMPLETE_GENERATION` ou `INVALID_GENERATION` (502). Une génération arrêtée par
une limite n’est jamais présentée comme une procédure complète. Un JSON final
invalide ou des citations non vérifiables conduisent au refus documentaire existant.

## Compatibilité Ollama

L’adaptateur lit `/api/version` et `/api/show` **une seule fois par instance backend**,
y compris en cas d’échec. Deux sondes bornées à cinq secondes, aucune génération
de test. Les appels concurrents partagent cette détection. La première consultation
des modes (ou la première question générative) la déclenche.

Le projet fixe **Ollama 0.12.3**. En mode `auto`, les deux modes génératifs sont
activés pour une version stable >= 0.12.3, famille `qwen3`/`qwen3moe`, et un template
qui conditionne réellement le raisonnement avec `.Think`. Les capacités déclarées,
si présentes, doivent confirmer `completion` et `thinking`. Leur absence est
tolérée si version, famille et template suffisent. Une capacité `completion` sans
`thinking` active seulement Rapide, sans envoyer un champ `think` inutile.
Les modèles à niveaux (par exemple GPT-OSS) ne sont pas convertis en faux niveaux
faible/moyen/élevé. Un modèle distant déclaré ou portant `:cloud` est refusé.

Si la détection est impossible, l’administrateur peut configurer :

- `OLLAMA_THINK_SUPPORT=boolean` **uniquement après avoir confirmé** que la version
  installée et le modèle/template acceptent réellement `think: false` ET `true`;
- `OLLAMA_THINK_SUPPORT=none` pour un modèle effectivement sans raisonnement.
  Rapide omet alors `think`; Réflexion reste désactivé.

Cette déclaration ne contourne pas une version connue trop ancienne, un modèle
absent, des capacités explicitement incompatibles ou un modèle distant.
**Redémarrer le backend après toute modification du modèle, du template, de la
version d’Ollama ou de cette configuration**, puis recharger la page. Une erreur
lors de la détection nécessite aussi ce redémarrage; pas de boucle de sondage.

### Vérification dans les sources de la version 0.12.3

- [Types de requête/réponse et ShowResponse](https://github.com/ollama/ollama/blob/v0.12.3/api/types.go) :
  `think`, `message.thinking`, `message.content`, `capabilities`, métriques.
- [Route chat](https://github.com/ollama/ollama/blob/v0.12.3/server/routes.go) :
  vérification de `thinking`, exécution du modèle puis séparation du raisonnement.
- [Boucle du runner](https://github.com/ollama/ollama/blob/v0.12.3/runner/ollamarunner/runner.go) :
  le compteur `numPredicted` avance pour **tous** les tokens avant cette séparation;
  `num_predict` est donc le budget combiné raisonnement + réponse finale.

La grammaire `format` est appliquée avant la séparation du raisonnement dans cette
version. En Réflexion, l’adaptateur l’omet pour ne pas empêcher la production de
`</think>`; le prompt exige le même JSON et le serveur valide toujours le schéma,
les identifiants et les extraits exacts. Cela peut augmenter les refus si le modèle
ne produit pas un JSON final valide. Rapide conserve le décodage contraint.
Le champ de raisonnement brut est retiré dans l’adaptateur : ni UI, ni source,
ni historique, ni journal. L’interface affiche seulement « Réflexion en cours… ».

## Budgets et consommation

Valeurs de départ prudentes pour 32 Go de RAM / RTX 4060 8 Go; elles ne sont pas
une promesse de latence ou d’utilisation VRAM sans mesure sur la machine cible.

| Réglage | Défaut | Effet |
|---|---:|---|
| `OLLAMA_NUM_CTX` | 4096 | Contexte total |
| `OLLAMA_NUM_PREDICT` | 768 | Budget Rapide, JSON final compris (nom existant conservé) |
| `OLLAMA_REFLECTION_NUM_PREDICT` | 1536 | Budget Réflexion : raisonnement + JSON final |
| `MAX_CONTEXT_CHARS` / `MAX_PASSAGE_CHARS` | 3200 / 1400 | Passages entiers; budget UTF-8 conservateur supplémentaire |
| `ASK_TIMEOUT_SECONDS` | 180 | File + métadonnées + recherche + génération; 240 maximum, sous le proxy 300 s |
| `ASK_CONCURRENCY` | 1 | Questions actives par processus backend |
| `ASK_QUEUE_SIZE` | 2 | Questions supplémentaires admises |
| `ASK_QUEUE_TIMEOUT_SECONDS` | 15 | Attente maximale d’une place |
| `OLLAMA_KEEP_ALIVE_SECONDS` | 120 | Maintien après embeddings/génération, 0 pour décharger |
| `OLLAMA_NUM_PARALLEL` | 1 | Parallélisme du service Ollama |
| `OLLAMA_MAX_LOADED_MODELS` | 1 | Modèles chargés simultanément côté Ollama |
| `OLLAMA_MAX_QUEUE` | 2 | File du service Ollama |

Les deux budgets de génération doivent rester sous la moitié du contexte. La
préparation réserve le budget du mode avant de choisir les passages; elle ne coupe
jamais une phrase pour la faire rentrer. Réflexion peut ainsi recevoir moins de
passages qu’un budget Rapide sur le même contexte. Les passages de recherche ne
consomment aucun budget de génération. Une limite de contexte conserve le refus
conservateur si aucun passage entier ne tient.

Un seul processus backend est supporté : multiplier les workers multiplierait les
limites applicatives. Les tâches d’indexation conservent leur verrou existant;
Ollama sérialise aussi leurs embeddings avec les générations. Le délai HTTP Ollama
existant (120 s sans données reçues) et les délais des dépendances peuvent échouer
avant le délai global. Les opérations synchrones locales Qdrant/fichiers ne sont
pas préemptibles par asyncio; le délai global n’est pas un mécanisme temps réel.

Le plafond d’un modèle chargé réduit le pic mémoire, mais peut décharger Qwen lors
de l’embedding suivant. Passer à deux uniquement après mesure mémoire sur la machine
cible. Une fermeture HTTP sur délai/cancellation demande l’arrêt au serveur; les
limites du service Ollama restent nécessaires durant la libération effective.

## Autorisations et absence de cache documentaire

Tous les modes réutilisent la recherche hybride filtrée par documents/révisions,
revérifient les droits avant exposition et ouvrent les PDF via les routes protégées.
Approfondir n’accepte aucun ancien passage ou réponse du navigateur comme source.
Aucun cache de réponse ou de recherche n’existe : deux questions identiques,
quel que soit le mode ou le compte, sont recalculées. Le seul cache ajouté contient
les capacités techniques du modèle, jamais des données documentaires. Les réponses
HTTP restent `Cache-Control: no-store, private` et les fetch utilisent `no-store`.

Si un cache documentaire est ajouté ultérieurement, sa clé devra inclure question,
mode, modèle/digest, paramètres, révision documentaire et périmètre d’accès; il faudra
revérifier les autorisations à chaque lecture. Les tests actuels vérifient l’absence
de réutilisation entre utilisateurs/modes et après révocation.

## Vérification et mesures locales

```bash
(cd backend && python -m pytest -q)
(cd frontend && npm ci && npm run build && npm run test:e2e)
```

La suite utilise des PDF fictifs, Qdrant embarqué et Ollama contrôlé : transmissions
`think`, une génération par question/approfondissement, zéro en recherche, nouveaux
embeddings à chaque question, citations/refus, sessions/droits révoqués, délais,
file pleine et annulation. Le navigateur vérifie aussi les radios au clavier,
le maintien de la première réponse, les liens de sources et la largeur 390 px.
Les captures sont publiées dans `docs/screenshots/modes-*.png` et comme artefacts CI.

**Aucune mesure matérielle n’a été obtenue dans l’environnement de développement** :
ni Ollama/Qdrant réseau, ni GPU disponibles. Les tests simulés ne mesurent pas la
qualité ou la vitesse réelle du modèle. Sur l’installation cible, choisir quelques
questions identiques (réponse simple, comparaison multi-PDF, information absente),
relever durée totale côté API et `eval_count`, `load_duration`, `total_duration`
côté Ollama, puis contrôler les citations. Ne pas journaliser le texte de
`message.thinking` ni le contenu des documents.

Séparer les essais modèle préchargé et les essais avec chargement; `load_duration`
nul ou faible ne suffit pas à prouver que le modèle était entièrement en VRAM.
Confirmer l’état avec `ollama ps` avant chaque essai. Avec un seul modèle chargé,
l’embedding peut provoquer un nouveau chargement de Qwen : l’indiquer au lieu de
classer automatiquement la deuxième question comme « chaude ».

### Benchmark reproductible des trois modes

Le benchmark utilise **les services locaux Ollama et Qdrant existants**, sans
installation de modèle, et une instance API ASGI isolée avec les véritables routes,
session, CSRF, ACL, recherche et validation des citations. Il crée deux PDF fictifs,
un compte éphémère et une collection Qdrant au nom unique; celle-ci est supprimée
à la fin, y compris en cas d'erreur. Il n'interroge pas vos documents ni vos comptes.
Les fichiers temporaires sont supprimés. Un arrêt forcé peut laisser une collection
`response_benchmark_*` à retirer manuellement. Aucun changement fonctionnel dans
les modes de réponse; aucune nouvelle route de métriques en production.

Sur la pile Compose (depuis la racine du dépôt), après installation des modèles
locaux habituels et démarrage d'Ollama/Qdrant :

```bash
mkdir -p benchmark-results
docker compose run --rm --no-deps \
  -v "$PWD/backend:/benchmark:ro" \
  -v "$PWD/benchmark-results:/results" \
  -w /benchmark --entrypoint python backend \
  -m benchmarks.response_modes --live \
  --ollama-url http://ollama:11434 --qdrant-url http://qdrant:6333 \
  --model qwen3:8b --embedding-model nomic-embed-text \
  --repeat 2 --output /results/modes-4060.json
```

Le backend doit avoir été construit (`docker compose build backend`). Le montage
inclut le code de benchmark, absent de l'image de production. La commande n'a pas
besoin du frontend. Exécuter à un moment sans autre question ni indexation : le
benchmark **décharge les modèles Ollama résidents** avant chaque essai froid, mais
ne change ni les modèles installés, ni les paramètres persistants du service.
Cela peut ralentir une autre application utilisant le même Ollama.

Hors Docker, avec les services accessibles sur localhost :

```bash
cd backend
python -m pip install -r requirements.txt
python -m benchmarks.response_modes --live --repeat 2 \
  --output modes-4060.json
```

Les paramètres sont reproductibles : contexte 4096, Rapide 768, Réflexion 1536,
température 0, trois questions (simple, deux PDF, information absente), mêmes PDF.
Les valeurs par défaut de `Settings` sont utilisées indépendamment du `.env` et
les chemins de stockage sont tous temporaires. Conserver le modèle, sa version,
Ollama 0.12.3 et les limites Compose pour comparer deux exécutions. Sur une RTX
4060 8 Go / 32 Go RAM, conserver **un seul modèle chargé** comme prévu; ne pas
augmenter les budgets pour ce premier relevé. Aucun résultat matériel n'est fourni
par les tests hors ligne.

Le JSON contient 27 essais avec `--repeat 2` : une requête froide suivie de deux
répétitions pour chaque question/mode. Codes de sortie : **0** toutes les
vérifications réussies, **1** au moins un échec de qualité ou une erreur API (le
rapport est conservé), **2** échec de préparation/service/nettoyage. Le chemin de
sortie doit être nouveau. Un mode indisponible reste une erreur API; aucun repli.

Champs de comparaison :

- `api_seconds` : durée de `POST /api/ask` à travers l'API authentifiée **en processus**,
  sans réseau frontend/proxy; comprend les sondes de résidence. `probe_seconds`
  isole leur coût pour l'estimation `api_seconds - probe_seconds`. Cela n'est pas
  une mesure de latence navigateur ni une instrumentation sans coût.
- `ollama` : pour chaque embedding/chat, durée HTTP, statut et métriques numériques
  autorisées (`eval_count`, `prompt_eval_count`, `load_duration`, `total_duration`,
  `eval_duration`, `prompt_eval_duration`). Durées Ollama en **nanosecondes**;
  valeurs absentes ou invalides = `null`; aucune métrique générative inventée
  pour Recherche seulement. Les tokens Réflexion incluent le raisonnement.
- `resident_before`, `resident_after` et `ollama[].before` : état `/api/ps`,
  équivalent structuré de `ollama ps`, avec taille totale et VRAM en **octets**.
  `cold_verified: true` signifie que la liste des modèles était vide après
  déchargement; `false` signifie que ce contrôle n'a pas pu être confirmé.
  `warm-repeat` signifie répétition sans déchargement explicite, **pas** garantie
  de modèle génératif chaud : consulter l'état juste avant `/api/chat` et la durée
  de chargement. L'embedding peut remplacer Qwen à chaque question.
- `system_before/after` : RAM Linux via `/proc/meminfo` en KiB et mémoire GPU
  via `nvidia-smi` en MiB, si disponibles; sinon `null`. Ce sont des instantanés,
  pas des pics ni une attribution au seul benchmark. Dans le conteneur backend,
  `nvidia-smi` peut être absent même si Ollama utilise le GPU; lancer aussi
  `nvidia-smi` et `ollama ps` sur l'hôte si nécessaire. Aucun outil système requis.
- `quality` : résultat des contrôles, citations présentes dans les pages PDF,
  `grounded`, nombre de sources/affirmations et d'appels chat. Rapide/Réflexion
  doivent restituer les extraits attendus avec citations, ou refuser l'information
  absente sans source/affirmation. Recherche doit rester sans réponse, sans
  affirmation, `grounded: false` et zéro chat; les passages attendus doivent être
  retrouvés. Une recherche sur une information absente peut retourner des passages
  proches, ce qui est normal. Ces contrôles ne prouvent pas une qualité générale
  du modèle sur vos documents.

Seuls les identifiants de cas, statistiques, états techniques et booléens sont
écrits. Aucun prompt, réponse, extrait, document, contenu `message.thinking`, mot de
passe, cookie, jeton CSRF ou corps d'erreur fournisseur n'est exporté. Les erreurs
fatales affichent un message générique. Le benchmark reçoit les corps en mémoire
pour la validation, puis les élimine; ne pas activer de logs HTTP détaillés autour
de cette commande. Les URLs sont limitées à localhost et aux services Compose,
sans identifiants, query ou fragment. Aucun modèle `:cloud` n'est accepté.

Tests hors ligne (API authentifiée, PDF réels, Qdrant embarqué, Ollama simulé,
contrôle de non-divulgation, froid/chaud, nettoyage, outils absents, citations
corrompues et `grounded` incorrect) :

```bash
(cd backend && python -m pytest -q tests/test_response_benchmark.py)
```
