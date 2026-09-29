# Architecture

## Composants

| Composant | Rôle |
|---|---|
| React + TypeScript | Question et affichage du chemin relatif, de la page et de l'extrait |
| FastAPI / `services.py` | Synchronisation, embeddings par lots et réponses sourcées |
| `indexing.py` | Inventaire strict, empreintes, verrous, versions et manifeste durable |
| PyMuPDF | Extraction du texte d'une copie temporaire du PDF, page par page |
| Ollama / `nomic-embed-text` | Embeddings des passages et questions |
| Qdrant | Passages vectoriels et manifeste sans vecteurs |
| SQLite FTS5 | Recherche lexicale locale des mêmes révisions |
| Ollama / Qwen + `answering.py` | Sélection structurée de passages et rendu extractif côté serveur |

## Identité et compatibilité

`document` est le chemin POSIX relatif à `documents/`, par exemple
`maintenance/fiche.pdf`. Il figure dans les métadonnées, le contexte du LLM et
les sources. Deux sous-dossiers peuvent donc contenir le même nom de fichier.

L'empreinte combine le SHA-256 du fichier et une signature de pipeline : version
de l'algorithme, version PyMuPDF, taille/chevauchement des passages, nom et digest
Ollama du modèle d'embeddings, absence de troncature implicite. La taille du lot,
le modèle de conversation et les paramètres de recherche n'y participent pas.
Le digest est obtenu par `/api/tags`; aucun embedding n'est produit pour un
passage inchangé. Le contenu du PDF est toutefois relu pour son SHA-256.

La révision dépend du chemin et de l'empreinte. L'UUID de chaque passage dépend
de la révision et de son rang. Rejouer une écriture écrase donc le même point.
Les modifications de découpage réindexent les fichiers. Un changement du modèle
ou de son digest exige une nouvelle collection pour éviter des vecteurs
incompatibles, même lorsque leur dimension est identique.

## Stockage Qdrant

- `<QDRANT_COLLECTION>` : points `schema`, `document`, `revision`, `fingerprint`,
  `page`, `text`, avec vecteur. Index de payload sur `schema` et `revision`.
- `<QDRANT_COLLECTION>__manifest` : collection sans vecteurs, un point de contrôle
  (schéma, modèle/digest, dimension) et un point par document (révision active,
  empreinte, nombre de passages). L'identifiant du manifeste dépend du chemin.

Le manifeste est lu avec une pagination de 128 points, sans vecteurs. Il ne
contient pas le texte des PDF. Il reste la seule source de vérité après un
redémarrage; aucun état d'indexation n'est conservé uniquement en mémoire Python.
Il faut sauvegarder/restaurer les deux collections ensemble, backend arrêté.
La perte du manifeste avec des passages présents bloque l'index plutôt que de
considérer arbitrairement les anciennes données comme actuelles.

## Synchronisation et publication

1. Prendre un verrou exclusif non bloquant, partagé entre les workers locaux.
2. Parcourir entièrement les dossiers; toute erreur de parcours ou lien symbolique
   de dossier/PDF fait échouer la synchronisation avant les mutations.
3. Refuser un dossier vide si le manifeste contient des documents, sauf option
   explicite `allow_empty=true`. Cette option ne permet pas un dossier absent.
4. Lire le manifeste et contrôler le schéma. Pour une collection neuve, un unique
   embedding d'initialisation fixe la dimension, puis le point de contrôle est écrit.
5. Traiter les PDF un par un. Si l'empreinte correspond au manifeste, compter
   `unchanged` et sauter extraction, embeddings et upserts du document.
6. Pour un ajout/modification, copier le PDF sur disque temporaire et vérifier son
   SHA-256. Extraire page par page, appeler `/api/embed` avec `input: [textes]` et
   `truncate: false`, puis écrire chaque lot dans Qdrant avec `wait=True`.
   La taille configurable vaut 16 par défaut. Vérifier nombre, dimension, valeurs
   finies et vecteurs non nuls. Un PDF sans texte échoue avec `NO_TEXT`.
7. Recontrôler le contenu source et le digest du modèle. Après l'écriture complète
   de tous les passages, remplacer **un seul point manifeste** avec `wait=True`.
   Cette écriture publie atomiquement la nouvelle révision du document. Toute
   écriture doit retourner un statut `completed` avant de poursuivre.
8. Vérifier de nouveau l'inventaire. Si l'arborescence a changé, annuler les
   suppressions et demander une relance. Une erreur de document annule aussi
   les suppressions de fichiers manquants pour cet appel.
9. Pour chaque PDF absent d'un inventaire stable, supprimer son manifeste; il
   devient immédiatement invisible aux prochaines recherches.
10. Sans erreur de document, supprimer les points v3 dont la révision ne figure
    dans aucun manifeste actif. Cette étape retire les anciennes versions et les
    passages abandonnés lors d'une interruption. Elle n'efface pas les points
    d'un autre schéma. En cas d'échec, retourner `cleanup_pending: true`.

La publication est atomique **par document**, pas pour tout le corpus. Un échec
sur un fichier n'annule pas les documents déjà publiés pendant cet appel.
L'extraction, les vecteurs et le texte complet du corpus ne sont jamais accumulés
ensemble : un fichier temporaire, une page extraite et un lot de vecteurs sont
traités à la fois. L'inventaire et les petits manifestes restent en mémoire, donc
leur coût et la taille du filtre croissent avec le nombre de documents.

## Échecs et reprise

| Moment de l'échec | État et relance |
|---|---|
| Lecture, extraction ou embedding | Ancien manifeste inchangé; les éventuels passages préparés sont invisibles |
| Écriture d'un lot | Aucun nouveau manifeste publié; ancienne version préservée |
| Avant publication du manifeste | Relance de la préparation avec les mêmes UUID, sans doublons |
| Réponse perdue lors de la publication | Les deux versions restent stockées; Qdrant peut avoir validé la nouvelle. La relance relit le manifeste durable |
| Après publication, avant nettoyage | Nouvelle version seule utilisée par l'API; une relance termine le nettoyage sans nouvel embedding |
| Retrait du manifeste, avant nettoyage | Document déjà invisible; une relance supprime ses passages physiques |
| Dossier absent, illisible ou vide non autorisé | Arrêt explicite, aucune suppression |

Les échecs d'écriture ambigus n'entraînent **aucun nettoyage** pendant l'appel.
La nouvelle version peut déjà être active si son manifeste a été écrit avant la
perte de la réponse, mais elle contient alors tous ses passages. L'ancienne
version n'est supprimée qu'à une relance réussie. Ne modifiez pas le modèle Ollama
ou les PDF pendant une synchronisation; les vérifications détectent les changements
observables, sans fournir de verrou distribué sur ces ressources externes.

## Question-réponse et concurrence

La recherche interroge les révisions actives dans Qdrant et SQLite, puis fusionne
les candidats par priorité exacte et RRF. Les UUID, versions et pages restent
attachés aux passages. Le contexte garde au maximum quatre passages entiers sous
un budget configurable. Qwen retourne uniquement des `passage_ids`; le serveur
valide tous les identifiants contre les seuls passages envoyés, puis restitue
leurs extraits complets avec citations. Aucun texte libre du modèle n'est accepté.
Les documents différents restent séparés avec l'avertissement de version existant.
Après génération, les révisions sont revérifiées sous verrou : une révision
retirée ou remplacée provoque un refus. Le rang n'est jamais une preuve de vérité.

Le [guide hybride](HYBRID_RETRIEVAL.md) décrit l'algorithme, le schéma 3, les limites
et les mesures. Lors de l'ingestion décrite ci-dessus, chaque lot Qdrant est aussi
écrit dans SQLite **avant** publication du manifeste. Le nettoyage concerne les
deux index. La perte de SQLite se répare depuis Qdrant sans nouvel embedding.
Un ancien manifeste v2 impose une nouvelle collection; aucune migration silencieuse.

Pendant une ingestion, une autre ingestion ou recherche échoue rapidement avec
HTTP 503 / `INDEX_BUSY`; il n'y a pas d'attente bloquant l'event loop sur un verrou.
Si une synchronisation modifie ou retire une révision sélectionnée pendant la
génération, la réponse est refusée; une ingestion encore active produit `INDEX_BUSY`.

`portalocker` libère le verrou à la fermeture ou à la mort du processus. Compose
partage `/state/locks` entre conteneurs du même projet, et refuse de créer
silencieusement un dossier hôte `documents/` manquant. Hors Compose, tous les
workers doivent utiliser le même `INDEX_LOCK_PATH` et la même URL Qdrant.
Plusieurs hôtes avec des dossiers de verrous indépendants ne sont pas supportés.
Une requête Qdrant directe sans le filtre applicatif n'offre pas ces garanties.

## Migration et suppression complète

Un index historique non vide sans manifeste compatible retourne `LEGACY_INDEX` sans
modification. Aucun rattachement par nom de fichier n'est tenté. Choisir une
nouvelle `QDRANT_COLLECTION`, reconstruire depuis les PDF, valider les sources et
les compteurs, puis conserver l'ancienne collection jusqu'à décision explicite de
la retirer. Le README donne les commandes et la procédure de retour arrière.

La suppression volontaire totale demande un dossier présent, lisible et sans PDF,
puis `POST /api/ingest?allow_empty=true`. Elle retire les manifestes de documents
et leurs passages, tout en conservant le point de contrôle. Elle ne migre pas un
ancien index et ne change pas le modèle associé à la collection.

## Frontières de confiance

- Les PDF sont des entrées non fiables et peuvent contenir des instructions trompeuses.
- Une similarité vectorielle ne prouve ni l'exactitude ni l'actualité d'un document.
- Le LLM peut encore interpréter incorrectement un passage.
- Les erreurs retournées ne contiennent ni texte de PDF ni exception fournisseur brute.
- Les ports sont liés à `127.0.0.1` par défaut; l'application n'offre aucune authentification.
- Les volumes Docker conservent localement modèles, manifeste et vecteurs.
