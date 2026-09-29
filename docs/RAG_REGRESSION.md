# Régression de l'assistant RAG

## Point de départ vérifié

La branche est basée sur `main` au commit `ee5cb73`, après fusion de la PR #5
(synchronisation incrémentale). À ce point, les citations vérifiables ne sont pas
implémentées : `grounded` signifie seulement qu'un résultat Qdrant existe, et
tous les résultats sont renvoyés comme sources. Aucune priorité métier entre deux
documents ou deux versions de procédure n'est définie. Les tests ne supposent
pas l'implémentation d'un autre prompt ou d'une PR non fusionnée.

## Tests rapides, sans services

Depuis la racine du dépôt, avec Python 3.12 :

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements-dev.txt
cd backend
python -m pytest -q
# Seulement la nouvelle suite RAG :
python -m pytest -q tests/test_rag_regression.py
```

Sous PowerShell, activer l'environnement avec `.venv\Scripts\Activate.ps1`.
Le job backend CI existant découvre automatiquement ces tests. Aucun modèle,
GPU, conteneur ni accès réseau n'est nécessaire. Un garde dans la nouvelle suite
interdit les connexions socket accidentelles. Le test HTTP historique reste ignoré
tant que ses variables `INSTRUCT_TEST_*` ne sont pas configurées.

Les cinq petits PDF sous `backend/evaluation/fixtures/` sont entièrement fictifs,
sans données confidentielles. Les pages et valeurs attendues sont explicites dans
`cases.json`. `python -m evaluation.build_fixtures` régénère les PDF avec PyMuPDF,
déjà dépendance du backend. Les PDF vierges, chiffrés et corrompus sont créés dans
les dossiers temporaires des tests; l'erreur de lecture est injectée à la frontière
du système de fichiers (les tests doivent aussi fonctionner en tant que root).

Les chemins de production passent par FastAPI, `PdfSource`, PyMuPDF, `chunk_text`,
les lots HTTP d'embeddings, les manifestes, le filtrage Qdrant et `/api/ask`.
Ollama est remplacé par `httpx.MockTransport`. Ses vecteurs lexicaux déterministes
permettent une vraie recherche cosinus en mémoire via le client Qdrant embarqué.
Ce dernier sert de double du serveur; les erreurs de nettoyage sont injectées
par une enveloppe existante. L'extraction, la recherche et la validation des
citations ne sont pas simulées.

Le faux modèle sélectionne des identifiants dans le contexte effectivement reçu.
Il ne fournit pas une réponse finale prédéfinie. Les assertions vérifient aussi
les citations contre les points indexés **et contre les pages des PDF**, ainsi
que les valeurs, les codes, les sources non sélectionnées et les anciennes révisions.
Des sorties malformées et hostiles doivent échouer même avec un identifiant valide.

| Scénario | Contrôle observable |
|---|---|
| Réponse présente | Bon chemin relatif, page 2, passage exact, identifiant stable et valeur `42.5 kPa` |
| Réponse absente malgré des résultats | Refus exact, `grounded: false`, aucune source |
| Aucun résultat ou index vide | Même refus, aucun appel de génération |
| Deux procédures contradictoires | Passages séparés à `18 kPa` et `42.5 kPa`, avertissement sans priorité inventée; refus aussi acceptable en évaluation locale |
| Code précis dans la question | `ZX-417` conservé; contenu libre substituant code, valeur ou unité rejeté |
| Injection dans le PDF | Texte dans les données utilisateur, aucun nouveau rôle système; sortie `PIRATE`/fausse source rejetée |
| PDF vierge, corrompu, chiffré ou illisible | Erreur maîtrisée, ancienne version encore citable, autres documents préservés |
| Génération invalide | JSON malformé, types incorrects, identifiant inventé, texte libre ou fausses métadonnées : refus |
| Passage réel mais non retrouvé | Référence rejetée, même si le point existe dans Qdrant |
| Sélection répétée/extrait long | Pas de citation dupliquée, passage complet non tronqué à 300 caractères |
| Réindexation/modification/suppression | Aucun ancien passage actif, même après échec du nettoyage physique |
| Synchronisation pendant la génération | Citation devenue obsolète rejetée avant de répondre |
| Erreur fournisseur/question invalide | HTTP 503 générique / HTTP 422 sans divulgation interne |

## Correction associée et contrat API

Les champs publics `answer`, `grounded`, `sources`, `document`, `page`, `excerpt`
et `score` restent présents. Chaque source reçoit aussi `passage_id` (UUID du point
Qdrant, stable tant que la révision et le découpage restent inchangés).

Ollama reçoit un schéma JSON et doit retourner `{"passage_ids": ["..."]}`.
Le serveur rejette toute sortie non conforme, tout champ supplémentaire ou toute
référence non retrouvée dans **cette** recherche. Une liste vide signifie un refus.
Les références sont ensuite recontrôlées contre le manifeste actif après génération.

La réponse devient **extractive** : le serveur affiche les passages complets,
séparés, avec leur document/page, au lieu d'afficher de la prose libre du modèle.
Cette correction empêche le modèle d'inventer des valeurs dans le texte affiché
ou d'accompagner une bonne référence d'une affirmation fabriquée. Les sources
renvoyées sont seulement celles sélectionnées et validées, et leur `excerpt`
contient le passage complet. Le frontend existant peut afficher ce contrat.

Quand plusieurs documents sont retrouvés, un avertissement indique qu'aucune
priorité de version n'existe. Le serveur ne déduit pas une autorité à partir du
nom du fichier, d'une date ou de l'ordre des résultats. Ce signalement est
conservateur : il peut apparaître même si les documents sont complémentaires.

`grounded: true` signifie ici **provenance vérifiée des passages affichés**.
Cela ne prouve ni leur pertinence pour la question, ni leur exactitude métier, ni
leur actualité. Un modèle peut sélectionner un passage authentique hors sujet ou
omettre une contradiction. Les tests déterministes vérifient les frontières de
confiance; ils ne prétendent pas mesurer la compréhension d'un vrai modèle.

## Évaluation facultative avec Ollama et Qdrant locaux

Démarrer ses services locaux et installer au préalable les deux modèles choisis.
La commande ne démarre aucun service et ne télécharge aucun modèle. Depuis
`backend/`, dans l'environnement Python ci-dessus :

```bash
python -m evaluation.run \
  --ollama-url http://127.0.0.1:11434 \
  --qdrant-url http://127.0.0.1:6333 \
  --chat-model qwen3:8b \
  --embedding-model nomic-embed-text \
  --output ../evaluation-results/rag-local.json
```

La commande utilise le service `KnowledgeBase` et le schéma de réponse public,
avec les mêmes PDF, questions et contrôles que la CI. Il n'est pas nécessaire de
démarrer FastAPI. Elle n'utilise pas le dossier `documents/` ni la collection
habituelle. Chaque corpus utilise une collection `instruct_eval_<UUID>` et son
manifeste, supprimés en fin d'essai même en cas d'erreur normale. Une interruption
brutale peut laisser ces seules collections temporaires; une erreur de nettoyage
les nomme dans le rapport. Les paramètres de découpage sont `220/35`, les lots
de 2, `TOP_K=8`, `MIN_SCORE=0.20` (modifiable via `--min-score`). Ces réglages
servent au petit corpus et ne sont pas une recommandation de production.

Le JSON contient pour chaque question : question, réponse, sources/pages/extraits,
`grounded`, refus attendu ou facultatif, refus observé, durée en secondes,
`checks_passed` et la liste des échecs. La durée couvre la question, recherche et
génération incluses, mais pas l'ingestion préalable. Une panne d'ingestion produit
une entrée en échec pour chaque question concernée, sans prétendre les avoir posées.
Code de sortie 0 si les contrôles passent, 1 sinon (nettoyage compris).

Le cas contradictoire exige soit le refus exact, soit les **deux** documents cités
avec l'avertissement. Les cinq questions ne reproduisent pas les pannes ni toutes
les variantes hostiles de la CI. Cette évaluation n'est appelée par aucun job CI
obligatoire. Un bon résultat sur cinq questions ne constitue aucune garantie de
sécurité ou d'exactitude.

## Limites nécessitant un travail distinct

- Évaluer plusieurs vrais modèles, tailles de corpus, langues et formulations;
  mesurer aussi les faux refus et le rappel de recherche.
- Définir une politique métier de version/approuvé/obsolète et une détection des
  contradictions, y compris entre deux passages du même document.
- Ajouter une validation sémantique avant de réintroduire une synthèse libre.
  Le refus d'une question absente et la résistance sémantique aux injections
  restent à évaluer avec les vrais modèles; le protocole seul ne les garantit pas.
- Tester OCR, PDF scannés, tableaux, unités coupées par le découpage et documents
  volumineux. Le mode actuel traite uniquement les PDF contenant du texte.
- Vérifier sur un vrai serveur Qdrant les pannes réseau, redémarrages, délais et
  comportements de concurrence; le client embarqué ne simule pas tout le serveur.

Contrôles complémentaires habituels depuis la racine :

```bash
(cd frontend && npm ci && npm run build)
test -e .env || cp .env.example .env
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.gpu.yml config --quiet
docker compose build backend frontend
```
