# Installation locale et réseau interne

Cette version vise une petite installation sur **une seule machine**. Aucun compte
cloud, serveur Redis, service d’identité externe ou second modèle n’est nécessaire.
SQLite conserve les comptes, sessions révocables, groupes, autorisations, catalogue
et audit. Qdrant et Ollama restent les moteurs existants.

## Installation et premier administrateur

Utiliser Docker Compose 2.24.4 ou ultérieur pour les fichiers d’extension.

```bash
cp .env.example .env
# Autoriser temporairement Ollama à télécharger les modèles, si nécessaires.
docker compose -f docker-compose.yml -f docker-compose.models.yml up -d qdrant ollama
docker compose exec ollama ollama pull qwen3:8b
docker compose exec ollama ollama pull nomic-embed-text
# Revenir au réseau de traitement isolé, sans l’extension models.
docker compose up -d --build --force-recreate
docker compose exec backend python -m app.admin create-admin yves
```

Le terminal demande et confirme un mot de passe de 12 à 256 caractères, sans
l’afficher ni le passer dans la ligne de commande. Aucun compte n’est créé par
le démarrage de l’application. Ouvrir `http://localhost:3000` **sur le serveur**.
Les cookies et l’origine sont liés à ce nom : ne pas remplacer `localhost` par
`127.0.0.1` dans le navigateur sans modifier `APP_ORIGIN`.

Le GPU reste facultatif; ajouter `-f docker-compose.gpu.yml` aux commandes Compose
si configuré. Les imports documentaires peuvent être faits après le retour au
réseau isolé. Les images Docker et modèles peuvent aussi être transférés hors
ligne; l’exécution courante ne requiert pas Internet.

### Récupérer l’administration

Avec un accès local au serveur Docker :

```bash
docker compose exec backend python -m app.admin recover-admin yves
```

Cette commande réactive le compte existant, lui attribue le rôle administrateur,
change son mot de passe, révoque ses sessions et remet à zéro les compteurs de
connexion. Elle est auditée avec un acteur local non identifié (`null`).
`create-admin` refuse de créer un autre compte tant qu’un administrateur actif
existe; les suivants sont créés dans l’interface. La désactivation ou rétrogradation
du dernier administrateur actif est refusée dans une transaction SQLite.

## Comptes, groupes et migration des documents

1. Se connecter comme administrateur, ouvrir **Administration**, créer les groupes
   (maintenance, production…) et les comptes. Un compte peut avoir plusieurs groupes.
2. Pour les PDF déjà présents dans `documents/`, ouvrir **Documents → Synchroniser
   le dossier local**. Aucun groupe n’est attribué automatiquement.
3. Dans **Autorisations** de chaque document, sélectionner les groupes autorisés.
4. Vérifier avec un compte lecteur : seuls ses documents apparaissent.

L’administrateur voit tous les documents du catalogue, y compris les documents
sans groupe; les autres utilisateurs sont refusés par défaut. Un index Qdrant
existant n’est pas implicitement public : seules les révisions également publiées
dans le catalogue SQLite et autorisées peuvent être recherchées. Conserver
`app_state` en plus des anciens volumes `qdrant_data` et `ollama_data`.

| Rôle | Lecture / questions | Documents | Administration |
|---|---|---|---|
| Lecteur | Groupes du compte | Aucun changement | Aucun accès |
| Gestionnaire documentaire | Groupes du compte | Ajout dans ses groupes, remplacement/retrait/indexation dans son périmètre | Aucun accès |
| Administrateur | Tous les documents actifs | Tous, synchronisation globale, attribution des droits | Comptes, groupes, conservation de l’audit |

Un gestionnaire ne peut pas modifier les autorisations d’un document existant.
Un remplacement conserve ses groupes. Un nouveau document téléversé exige au
moins un groupe explicitement sélectionné. Les PDF importés par le système de
fichiers restent réservés à l’administrateur jusqu’à attribution explicite.

Un remplacement doit être indexé avant de redevenir recherchable. Les versions
PDF sont conservées par SHA-256, sans conserver les anciens vecteurs devenus
inutiles. Les anciennes citations ouvrent la version exacte archivée, après un
nouveau contrôle des droits actuels. **Retirer** bloque aussi l’ouverture des
anciennes versions. Réimporter un document retiré est réservé à l’administrateur.
Les éditions directes sur disque demandent une nouvelle synchronisation.

La synchronisation globale conserve la protection contre un dossier accidentellement
vide. L’administrateur peut confirmer une suppression globale via l’API avec
`POST /api/ingest?allow_empty=true`, une session, l’origine et le jeton CSRF habituels.
Le gestionnaire n’a jamais accès à cette route globale. Les compteurs et erreurs
d’une indexation individuelle ne portent que sur le document concerné.

## Sessions et contrôles d’accès

- Argon2id via `argon2-cffi`, paramètres recommandés par la bibliothèque (~64 Mio
  de mémoire temporaire par vérification); aucun chiffrement maison.
- `SessionMiddleware` Starlette et `itsdangerous` signent le cookie; il contient
  seulement un identifiant aléatoire de session et un jeton CSRF. Le serveur ne
  conserve que l’empreinte de l’identifiant et sa date d’expiration dans SQLite.
- Cookie HttpOnly, SameSite=Strict, Secure obligatoire en HTTPS; expiration absolue
  de 8 h par défaut. Mot de passe changé/réinitialisé, compte désactivé, rôle ou
  groupes du compte modifiés : toutes ses sessions sont immédiatement invalidées.
- Les écritures exigent **l’origine exacte** `APP_ORIGIN` et `X-CSRF-Token`. Le jeton
  se récupère via `GET /api/auth/session` et change à chaque connexion. Même la
  connexion est protégée. Un client API doit conserver le cookie reçu.
- Cinq tentatives de connexion par identifiant sur 15 minutes par défaut; plafond
  de 50 par adresse directement observée. Les succès comptent aussi. Les compteurs
  SQLite sont partagés entre workers. Le proxy n’est pas considéré comme une
  source fiable d’identifiants utilisateur; les en-têtes de groupes sont ignorés.
- Les filtres Qdrant portent sur les seules révisions autorisées **avant** le top-k.
  Ils sont recalculés après les embeddings, avant le contexte Ollama et après la
  génération. Une révocation ou nouvelle révision retire la réponse entière.
- Les réponses API/PDF portent `Cache-Control: no-store, private`. Pas de cache RAG
  partagé ni d’historique de questions. Le navigateur garde seulement l’écran
  courant; il l’efface lors d’un changement de session/droits détecté par son
  contrôle périodique (15 s). Le serveur revérifie immédiatement chaque demande.

Une copie déjà lue, téléchargée, photographiée ou enregistrée par l’utilisateur
ne peut pas être rappelée à distance. Une requête déjà envoyée à Ollama avant une
révocation ne peut pas être « désenvoyée »; sa réponse n’est pas servie si les droits
ont changé. Les données autorisées reçues précédemment par une personne restent
évidemment connues de cette personne.

## Journal d’audit

**Administration → Journal d’audit**, pagination de 25 événements dans l’interface
(100 maximum par demande API). Heure UTC, ID de l’acteur lorsqu’il est connu,
action, ressource et résultat. Sont tracés connexions réussies/échouées/limitées,
déconnexions, comptes, mots de passe changés (sans valeur), rôles, groupes, droits,
import/remplacement/retrait, début/fin de synchronisation, sauvegarde/restauration.

Aucun mot de passe, cookie, jeton, PDF complet, question ou réponse complète n’est
écrit dans le journal. La conservation vaut 90 jours par défaut, configurable de
1 à 3650 jours dans l’interface; la purge intervient à chaque nouvel événement.
`AUDIT_RETENTION_DAYS` initialise la valeur uniquement lors de la création du stockage.
Les archives anciennes peuvent contenir des événements désormais purgés du serveur;
leur durée de conservation doit être gérée séparément.

**Limite** : une personne ayant accès à Docker, aux fichiers SQLite ou à la machine
peut lire les PDF, modifier les droits et altérer/effacer le journal. Ce journal
local n’est ni inviolable, ni signé par un tiers, ni une preuve indépendante.
Limiter les comptes système, protéger les sauvegardes et le disque de la machine.

## Sauvegarder

Ne pas modifier directement les PDF pendant l’opération. La commande prend un
verrou exclusif de maintenance et d’indexation; une requête en cours fait échouer
la prise du verrou plutôt que produire une archive partielle. Réessayer à un moment
calme, ou arrêter temporairement backend/frontend. Les nouvelles requêtes reçoivent
503 pendant le verrou. Tous les processus doivent utiliser le même `STATE_PATH`
et `INDEX_LOCK_PATH`; aucun accès direct non coordonné à Qdrant n’est pris en charge.

```bash
mkdir -p backups
chmod 700 backups
docker compose run --rm --no-deps \
  -v "$PWD/backups:/backups" backend \
  python -m app.admin backup /backups/instruct-2026-09-29.tar.gz
```

L’archive (permissions 0600) contient : PDF courants, versions PDF conservées,
SQLite (comptes/hachages Argon2, groupes, ACL, catalogue et audit), export JSONL par
lots des vecteurs et manifestes Qdrant, ainsi qu’un manifeste de schémas,
configuration d’indexation, tailles et SHA-256. Les sessions et compteurs de connexion
sont retirés de la copie SQLite. Les modèles Ollama, `.env`, la clé de session et
les certificats TLS ne sont **pas** inclus. Conserver séparément la configuration,
les certificats et les fichiers des modèles si une reprise totalement hors ligne
est nécessaire. Le digest du modèle d’embeddings doit rester identique.

L’export logique évite la copie incohérente des fichiers internes d’un Qdrant actif.
Sa durée dépend du corpus : la maintenance peut durer plusieurs minutes. Il n’y a
pas de durée maximale garantie. Prévoir l’espace du corpus, des anciennes versions,
d’une copie temporaire, des vecteurs JSONL (plus volumineux que leur stockage natif)
et de l’archive. L’archive n’est **pas chiffrée**; la stocker sur un support chiffré,
avec un accès restreint et une copie hors de la machine. Les SHA-256 détectent une
corruption, pas la falsification d’une archive par une personne capable de modifier
son manifeste : ne restaurer que des archives de confiance.

## Restaurer et vérifier

Utiliser la même version du code et les mêmes paramètres `EMBEDDING_MODEL`,
`OLLAMA_MODEL`, `CHUNK_SIZE`, `CHUNK_OVERLAP`. Le nom de collection Qdrant peut être
différent pour un exercice isolé. Les schémas incompatibles sont refusés; il n’y a
pas de migration silencieuse. Les chemins d’archive, liens, doublons, fichiers
spéciaux, tailles et contrôles d’intégrité sont vérifiés **avant** toute modification
de la destination. Limites : 100 000 fichiers, 2 Gio par fichier, 20 Gio décompressés;
prévoir une autre procédure pour un corpus plus grand.

Arrêter backend et frontend avant la restauration. Depuis un **projet Compose
isolé**, avec un autre nom de projet, un autre dossier `documents/` et des volumes
neufs (ou une machine de test), démarrer seulement Qdrant/Ollama :

```bash
docker compose up -d qdrant ollama
docker compose run --rm --no-deps \
  -v "$PWD/backups:/backups:ro" backend \
  python -m app.admin restore /backups/instruct-2026-09-29.tar.gz
docker compose up -d backend frontend
```

Une destination existante (SQLite, fichiers documents ou collections Qdrant) est
refusée. Les répertoires neufs doivent être vides, y compris les `.gitkeep` du dépôt.
Pour une restauration **volontairement destructive** après sauvegarde de la
configuration précédente, ajouter `--overwrite`. Cette option remplace les
collections configurées et les fichiers documents/versions de destination.
Ne jamais réutiliser les volumes de production dans un exercice isolé.

Les droits sont restaurés et les anciennes sessions sont effacées; une nouvelle
clé de session est créée. Si la restauration échoue après avoir commencé à écrire,
`RESTORE_INCOMPLETE` reste dans l’état et bloque l’API. Ne pas simplement le supprimer :
corriger la cause, puis rejouer une restauration avec `--overwrite`. Il ne s’agit
pas d’une transaction atomique distribuée; le marqueur empêche de servir un état
partiellement restauré. Redémarrer le backend à la fin pour charger la nouvelle clé.

Vérifier ensuite avec deux comptes : connexion, liste distincte, question avec
source connue, ouverture à la bonne page, refus de l’URL d’un document de l’autre
groupe, puis déconnexion et refus de l’ancienne URL. Répéter cet exercice après une
évolution de schéma. Les tests automatisés réalisent cette restauration avec de
vrais PDF, SQLite et Qdrant persistant; seul Ollama est simulé.

## HTTPS sur le réseau interne

1. Réserver une adresse IP privée au serveur; configurer un nom DNS interne, par
   exemple `instruct.example.internal`.
2. Obtenir un certificat couvrant ce nom auprès de votre autorité interne et
   installer sa chaîne de confiance sur chaque poste. Mettre la chaîne PEM dans
   `certs/fullchain.pem` et la clé privée dans `certs/privkey.pem` (0600, dossier 0700).
   Ne pas ignorer les alertes de certificat dans le navigateur.
3. Dans `.env`, définir `APP_ORIGIN=https://instruct.example.internal`,
   `COOKIE_SECURE=true` et `LAN_BIND_IP=192.168.1.20` (adapter à votre serveur).
4. Démarrer :

```bash
docker compose -f docker-compose.yml -f docker-compose.lan.yml up -d --build
```

Seul le port 443 sur cette adresse privée est publié. Aucun port backend, Qdrant
ou Ollama n’est publié. Le frontend et l’API ont la même origine; le navigateur
utilise des URL relatives `/api/...`, jamais le `localhost` d’un autre poste.
`APP_ORIGIN` refuse le HTTP hors loopback. Le reverse proxy Nginx utilise les
certificats fournis, sans ACME public ni publication automatique sur Internet.
Limiter le pare-feu aux sous-réseaux de l’entreprise et ne pas créer de redirection
sur le routeur. Retirer l’extension `models` avant tout accès documentaire réel.

Le contrôle par adresse des connexions est conservateur : derrière le proxy il
peut regrouper plusieurs utilisateurs sous l’adresse du proxy. Le contrôle par
identifiant reste indépendant. Aucun en-tête transmis par le navigateur n’est
utilisé comme identité ou groupe.

## API et développement

Toutes les routes `/api/*` sont protégées, sauf les amorces de connexion/session.
`/healthz` ne donne qu’un statut. La documentation OpenAPI publique est désactivée.
Principales routes : `/api/auth/*`, `/api/documents`, `/api/documents/{id}/file`,
`/versions`, `/sync`, `/api/ask`, `/api/ingest` (admin), `/api/admin/users`, `/groups`,
`/documents/{id}/groups`, `/audit`, `/configuration`.

Pour un développement hors Docker, configurer `STATE_PATH`, `DOCUMENTS_PATH`,
`INDEX_LOCK_PATH`, `QDRANT_URL`, `OLLAMA_URL` et `APP_ORIGIN=http://localhost:3000`.
Lancer `uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000` depuis
`backend/`, puis `npm run dev` depuis `frontend/`. Vite relaie `/api` vers le backend.
Les réglages d’exploitation restent dans `.env`, réservé à l’administrateur système;
la conservation de l’audit est modifiable dans l’interface.

## Validation et limites

Voir [VALIDATION_PME.md](VALIDATION_PME.md) pour les résultats exécutés et les étapes
encore manuelles. Une démonstration synthétique **restaurée** peut être lancée avec
`PYTHONPATH=.:tests python tests/browser_demo.py` depuis `backend/`, puis le frontend.
Les comptes de **ce test uniquement** sont `admin`, `alice`, `bobby`, avec le mot de
passe `Synthetic-password-123`; ils ne sont jamais créés par l’application normale.

Cette branche part de la synchronisation fusionnée dans la PR #5. La recherche
lexicale et la validation sémantique/structurée des citations des autres chantiers
ne sont pas présentes dans ce socle. Les sources sont des passages récupérés
vérifiables par document/page/version, mais le `grounded` historique n’est toujours
pas une preuve que chaque phrase générée découle du passage. Aucune recherche
lexicale non filtrée n’est ajoutée. Une future recherche lexicale devra recevoir
la même liste de révisions autorisées avant de sélectionner ses candidats.

Il n’y a ni annuaire LDAP/SSO, ni MFA, ni cluster multi-hôtes, ni antivirus/OCR,
ni ordonnanceur de sauvegardes, ni purge automatique des anciennes versions PDF.
La consommation principale reste celle des modèles; SQLite ne demande aucun
nouveau service. Les PDF sont limités à 50 Mio et 10 000 pages par défaut; un
PDF est lu en mémoire pour son archivage et son ouverture. Planifier l’espace des
versions et sauvegardes. Ne pas présenter l’application comme un système certifié
de sécurité industrielle.

Références techniques : [sessions Starlette](https://www.starlette.io/middleware/#sessionmiddleware),
[Argon2](https://argon2-cffi.readthedocs.io/en/stable/howto.html),
[correctif Starlette des limites de formulaires](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq).
