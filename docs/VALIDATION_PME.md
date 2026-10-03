# Validation de la résolution des conflits — 29 septembre 2026

Main intégré : `2233379` (PR #7, #8, #9 et #10).

Vérifications locales exécutées après résolution :

- `cd backend && ../.venv/bin/python -m pytest -q --tb=short` : **212 réussis, 1 ignoré**.
- Régressions RAG, citations structurées, recherche hybride, gestion documentaire,
  authentification et restauration exécutées ensemble.
- Tests supplémentaires : recherche uniquement lexicale avec deux groupes distincts;
  refus des routes de gestion avancée aux lecteurs/gestionnaires; tâche différée
  interrompue après révocation, aucun jeton dans le journal de tâches.
- Restauration réelle de PDF, SQLite, Qdrant persistant et droits; reconstruction
  de FTS5 dans un nouveau répertoire avant recherche/citations/ouverture PDF.
- `ruff check --isolated --select E4,E7,E9,F backend`,
  `ruff format --isolated --check backend`, `pip check`, `git diff --check`.
- `cd frontend && npm ci && npm run build`.

La CI exécute en complément le parcours documentaire sur ordinateur/mobile,
le parcours à deux comptes après restauration et les constructions Docker avec
validation Compose standard/GPU/LAN/models. Consulter le résultat du commit de
résolution dans la PR; les succès de l’ancienne version ne prouvent pas cette
intégration. Docker et navigateur ne sont pas exécutables dans cet environnement
local; aucune validation locale de ces deux outils n’est revendiquée.

Le test Qdrant/Ollama réels reste facultatif et ignoré sans services configurés.
Les modèles sont simulés dans les tests automatiques. Certificats, pare-feu, accès
depuis deux postes et restauration à la taille réelle restent à valider sur site.

Le format d’archive est désormais 2. La gestion avancée est administrative;
les gestionnaires disposent des opérations unitaires limitées à leurs groupes.
