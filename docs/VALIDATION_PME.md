# Validation du socle PME

Base examinée : `ee5cb73`, PR #5 fusionnée (synchronisation incrémentale).
La branche ne suppose pas les chantiers citations, recherche lexicale ou interface
documentaire déjà fusionnés. Aucun document réel ni compte de production utilisé.

## Exécuté dans l’environnement de développement

- `cd backend && ../.venv/bin/python -m pytest -q --tb=short` : **53 réussis,
  1 ignoré**. Le test ignoré demande des services HTTP Qdrant/Ollama réels;
  aucune variable `INSTRUCT_TEST_*` n’était fournie.
- `ruff check --isolated --select E4,E7,E9,F backend` : réussi.
- `ruff format --isolated --check backend` : réussi.
- `cd frontend && npm ci && npm run build` : réussi.
- `python -m pip check` : réussi.
- `PYTHONPATH=backend python -m app.admin --help` : réussi.
- `git diff --check` : réussi.
- Démonstration de serveur synthétique **restauré** démarrée avec
  `PYTHONPATH=.:tests python tests/browser_demo.py`; startup Uvicorn réussi.

Les nouveaux tests passent par l’API HTTP réelle de l’application (TestClient),
Argon2/Starlette, SQLite, de vrais PDF et les filtres du moteur Qdrant local.
La restauration est exécutée vers de nouveaux répertoires et une nouvelle collection,
puis Qdrant persistant est fermé/réouvert avant les assertions : connexion, droits,
recherche, citations, PDF à la page attendue, anciennes versions, ancien cookie
refusé. Les refus d’archive altérée/incompatible/path traversal, de destination
existante, de sauvegarde incohérente et la reprise après restauration interrompue
sont aussi testés. Ollama est un double déterministe : ces tests ne mesurent pas
la qualité des réponses d’un vrai modèle.

## Parcours navigateur / CI

`frontend/tests/browser.mjs` vérifie sur l’installation synthétique restaurée :
connexion lecteur → recherche dans le bon groupe → clic sur la source et réponse
PDF → URL directe d’un autre groupe refusée → déconnexion → ancienne URL refusée.
Il vérifie également l’écran d’administration et l’absence d’erreurs JavaScript.
Le job CI `browser` installe Chromium et lance ce parcours avec `npm run test:browser`.

La tentative locale `agent-browser` a échoué au démarrage de son daemon
(`Failed to bind socket: Operation not permitted`). Les téléchargements Playwright
ont échoué (archives vides/tronquées). Un binaire Chromium alternatif a ensuite
quitté au lancement. **Aucun succès navigateur local n’est revendiqué**; consulter
le résultat du job CI associé au commit de la PR, qui fait foi pour ce parcours.

Docker n’est pas installé dans cet environnement. Les validations Compose standard,
LAN et téléchargement des modèles, et la construction des images, sont confiées
au job CI `docker`. Ne pas les confondre avec les tests locaux ci-dessus.

## À vérifier sur le serveur cible

- Chaîne TLS interne reconnue sur deux postes, nom DNS, pare-feu et adresse privée;
  aucune redirection publique et aucun port Qdrant/Ollama/backend publié.
- Modèles Ollama réels déjà présents : qualité d’une question connue, citation,
  absence de document d’un autre groupe dans le contexte, performances sur le GPU.
- Sauvegarde représentative du volume réel : durée de maintenance, espace disque,
  droits du support chiffré, restauration périodique isolée.
- Versions des certificats/modèles et `.env` conservées séparément de l’archive;
  anciennes sessions invalidées après redémarrage du backend restauré.

Les droits ne remplacent pas la validation sémantique des réponses. La validation
structurée des citations et la recherche lexicale restent des chantiers distincts.
