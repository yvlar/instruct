# Architecture

## Composants

| Composant | Rôle |
|---|---|
| React + TypeScript | Interface de question et affichage des sources |
| FastAPI | API d'ingestion et de question |
| PyMuPDF | Extraction du texte et des numéros de page |
| Ollama / `nomic-embed-text` | Embeddings des passages et questions |
| Qdrant | Stockage et recherche vectorielle |
| Ollama / Qwen | Génération de réponses à partir du contexte retrouvé |

## Ingestion

1. Le backend recherche les PDF sous `documents/`.
2. PyMuPDF extrait le texte page par page.
3. Le texte est normalisé et découpé avec chevauchement.
4. Ollama transforme chaque passage en vecteur.
5. Qdrant enregistre le vecteur et les métadonnées `document`, `page` et `text`.
6. Un UUID déterministe rend la réindexation d'un passage identique idempotente.

## Question-réponse

1. La question est transformée en vecteur avec le même modèle d'embeddings.
2. Qdrant retourne jusqu'à `TOP_K` passages dépassant `MIN_SCORE`.
3. Sans résultat, l'API indique que l'information n'a pas été trouvée.
4. Sinon, les passages et leurs références sont transmis à Qwen.
5. Le prompt interdit d'ajouter une procédure ou une valeur absente du contexte.
6. L'API retourne la réponse et les sources retrouvées.

## Frontières de confiance

- Les PDF sont des entrées non fiables et peuvent contenir des instructions trompeuses.
- Une similarité vectorielle ne prouve ni l'exactitude ni l'actualité d'un document.
- Le LLM peut encore interpréter incorrectement un passage.
- L'accès réseau est limité à `127.0.0.1` par défaut, mais l'application n'offre aucune authentification.
- Les volumes Docker conservent localement modèles et vecteurs après l'arrêt des conteneurs.

