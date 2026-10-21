# Contribuer à Instruct IA

Merci de contribuer au projet.

## Avant de commencer

1. Consultez les issues existantes.
2. Pour un changement important, ouvrez d'abord une issue afin de discuter de l'approche.
3. N'ajoutez jamais de document de travail réel, de secret, de donnée personnelle ou d'information appartenant à un employeur.
4. Toute modification touchant les réponses, les citations ou le filtrage doit préserver les garde-fous décrits dans `docs/SAFETY.md`.

## Développement local

```bash
git clone https://github.com/yvlar/instruct.git
cd instruct
cp .env.example .env
docker compose up -d qdrant ollama
docker compose up -d --build
```

### Backend

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements-dev.txt
python -m pip check
cd backend
pytest -q
```

### Mises à jour Python

`qdrant-client==1.19.1` exige `portalocker>=2.7.0,<4.0`; conserver
`portalocker==3.2.0` tant que cette contrainte s'applique. Dependabot ignore
Portalocker 4 et les versions suivantes et regroupe séparément Qdrant et
Portalocker. Les autres mises à jour mineures/correctives restent groupées;
les versions majeures sont proposées séparément pour faciliter leur validation.
Ne retirer la règle `ignore` qu'après vérification des métadonnées d'une nouvelle
version de Qdrant et passage des quatre jobs CI (backend, frontend, browser,
Docker). La résolution pip, `pip check` et les parcours navigateur doivent rester
valides; ne pas désactiver les tests pour accepter une mise à jour.

### Frontend

```bash
cd frontend
npm ci
npm run build
```

## Soumettre une contribution

- créez une branche courte et descriptive;
- ajoutez ou mettez à jour les tests pertinents;
- vérifiez qu'aucun document ou secret n'est inclus;
- utilisez des commits clairs;
- remplissez le modèle de pull request;
- acceptez la licence MIT pour votre contribution.

En soumettant une contribution, vous acceptez qu'elle soit distribuée sous la licence MIT du projet.
