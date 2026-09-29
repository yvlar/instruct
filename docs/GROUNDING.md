# Réponses avec passages vérifiables

## Contrat et choix prudent

La recherche Qdrant retourne des candidats, pas des preuves de vérité. Le backend
ne met plus `grounded: true` dès qu'il existe un résultat. Le modèle sélectionne
une **réponse extractive** : une liste d'extraits exacts, chaque extrait étant relié
à un identifiant de passage. Il ne rédige pas de paraphrase libre. Ce choix réduit
le risque d'ajouter une valeur, une étape ou une référence absente du PDF.

Une génération Ollama `/api/chat` reçoit :

- un message système fixe; aucune donnée PDF n'y est interpolée;
- un message utilisateur JSON contenant `QUESTION` et `PASSAGES`, explicitement
  traités comme des données non fiables;
- pour chaque passage, un `source_id` stable et le texte; ni document ni page ne
  doivent être produits par le modèle;
- un schéma JSON strict : `status` (`answered`, `insufficient`, `ambiguous`) et
  `answer: [{source_id, quote}]`. Les identifiants admissibles sont aussi énumérés
  dans le schéma; le backend les vérifie indépendamment.

L'identifiant est `p_` suivi de 24 caractères du SHA-256 de l'identifiant Qdrant,
du chemin relatif, de la page et du texte normalisé. Il est indépendant du rang
vectoriel et change avec la révision du passage. Les numéros `[1]`, `[2]` servent
seulement à l'affichage dans une réponse; ils ne sont pas les identifiants stables.
Les citations seules n’imposent aucune réindexation supplémentaire; la migration
vers le schéma 3 de la recherche hybride reste nécessaire pour les anciens index.

## Validation avant affichage

Le backend exige une génération terminée (`done: true`, `done_reason: stop`),
un JSON sans doublon de clé ni champ supplémentaire, un statut `answered`, de
1 à 6 extraits non vides et au maximum les tailles configurées. Tout appel d'outil
émis par le modèle est refusé. Il n'existe aucun outil ni exécuteur dans ce parcours.

Chaque identifiant doit appartenir aux passages réellement **envoyés au modèle**,
pas seulement aux candidats Qdrant. Chaque citation doit correspondre exactement
à une sous-chaîne du passage, après normalisation des espaces uniquement. Le
backend recopie le texte depuis le passage et résout document/page lui-même.
Il refuse les fragments commençant au milieu d'une phrase, les fragments finissant
avant une ponctuation finale et les citations strictement dupliquées. Ces contrôles
conservent mieux négations, unités et conditions, sans analyser leur sens.

Si un élément échoue, toute la réponse est refusée; aucune réponse partielle ni
source inutilisée n'est renvoyée. Aucune deuxième génération de réparation ou de
vérification n'est lancée. Le refus HTTP 200 est toujours :

```json
{
  "answer": "Information non trouvée dans les instructions disponibles.",
  "grounded": false,
  "claims": [],
  "sources": [],
  "safety_notice": "Vérifiez toujours la version officielle de l’instruction avant d’exécuter une procédure. Cet assistant de recherche documentaire n’est pas une autorité en matière de sécurité industrielle."
}
```

Une indisponibilité réseau, un index incompatible ou une erreur de service restent
un HTTP 503 avec message assaini; ils ne sont pas présentés comme une absence
d'information documentaire.

## API et interface

Les champs historiques `answer`, `grounded`, `sources`, `document`, `page`,
`excerpt`, `score`, `passage_id`, `revision` et `fingerprint` sont conservés. S'ajoutent `claims: [{text, source_ids}]`,
`sources[].source_id` et `safety_notice`. `answer` est assemblé par le backend à
partir des extraits validés et des numéros de citation. Le frontend affiche
`claims`, avec un lien par élément vers le passage concerné. Plusieurs extraits
peuvent pointer vers le même passage; sa source est dédupliquée.

`excerpt` contient le passage entier fourni au modèle, borné par
`MAX_PASSAGE_CHARS`, pour pouvoir lire les conditions autour de la citation.
Tous les textes sont affichés comme texte React; aucun HTML, Markdown actif,
lien fourni par le modèle ou texte `thinking` n'est exécuté ou rendu comme contenu.
Les ancres sont construites avec les identifiants générés côté serveur.

Le score est présenté comme **classement hybride RRF**, sans pourcentage.
Il ne constitue ni une probabilité de vérité, ni une confiance calibrée.
Le rappel officiel provient d'une constante backend et reste visible même lors
d'un refus; il ne dépend pas de l'obéissance du modèle.

## PDF non fiables et limites restantes

Le prompt impose un refus face aux passages incomplets, proches du sujet mais
insuffisants, contradictoires ou ambigus. Un filtre conservateur repère également
certaines injections évidentes (ignorance des règles/sources, faux rôles système,
demandes d'exécution). Un candidat suspect fait refuser la question avant toute
génération, même si un autre candidat semblait utile. **Ce filtre n'est pas un
classificateur exhaustif** : une formulation obfusquée peut lui échapper et un
texte légitime discutant d'une attaque peut déclencher un faux positif.

`grounded: true` signifie ici : *le modèle a déclaré les passages suffisants et
chaque extrait affiché a passé les contrôles de provenance et de format*. Cela
**ne prouve pas** que la réponse est vraie, complète, pertinente ou sûre. Un
extrait authentique peut concerner la mauvaise machine, être obsolète, ignorer
une condition située dans une autre phrase ou être lui-même malveillant. Le
modèle peut sélectionner un extrait non pertinent malgré le prompt; aucune
vérification déterministe ne résout cette question sémantique générale. La
recherche peut manquer une contradiction située ailleurs dans le corpus.

Le refus est préféré à la correction automatique, aux réponses libres et aux
réponses partielles. Les bornes peuvent provoquer des faux refus, particulièrement
pour des procédures longues, des tableaux, des phrases dépassant 700 caractères,
un PDF mal extrait ou des questions composées. Les signes `. ! ? ;` sont des
heuristiques de frontière, pas une analyse grammaticale. L'application est une
aide de recherche documentaire, jamais une autorité de sécurité industrielle.

## Coût et réglages initiaux

| Réglage | Défaut | Effet |
|---|---:|---|
| `TOP_K` | 4 | Passages retenus après fusion, maximum configurable 20 |
| `MAX_CONTEXT_CHARS` | 3200 | Plafond cumulé du texte des passages |
| `MAX_PASSAGE_CHARS` | 1400 | Plafond d'un passage; un passage trop long est omis entier |
| `OLLAMA_NUM_CTX` | 4096 | Fenêtre de contexte Ollama en tokens |
| `OLLAMA_NUM_PREDICT` | 768 | Limite de génération en tokens, JSON compris |
| `MAX_ANSWER_CHARS` | 1600 | Somme maximale des extraits sélectionnés |
| `MAX_RESPONSE_CHARS` | 6000 | Taille maximale du contenu JSON accepté |

`think: false` et `temperature: 0` sont utilisés. Le code réserve la longueur UTF-8
des messages comme borne pessimiste de tokens pour Qwen3, plus 256 tokens de
marge de gabarit et le budget de sortie. Le schéma `format` contraint le décodage;
il n'est pas ajouté au texte des messages. Cette vérification peut réduire le
contexte effectif en dessous de 3200 caractères. Les passages sont conservés ou
omis **entiers**, jamais tronqués pour tenir dans le budget. Si aucun ne tient,
la réponse est refusée sans génération. Cette estimation vise Qwen3; elle n'est
pas une mesure exacte universelle pour tous les modèles et gabarits Ollama.

Valeurs initiales prudentes pour `qwen3:8b` sur 8 Go de VRAM; elles ne garantissent
pas la résidence intégrale sur GPU selon la quantification, les autres processus
et la coexistence du modèle d'embeddings. Commencer avec ces valeurs, puis mesurer
avant d'augmenter le contexte. Aucun nouveau modèle ni second modèle vérificateur
n'est requis.

| Mesure | Avant | Après |
|---|---|---|
| Générations par question avec contexte | 1 | 1, aucun appel de vérification |
| Texte candidat par défaut | Jusqu'à environ 6 × 1400 = 8400 caractères | ≤ 3200 caractères, budget UTF-8 souvent plus strict |
| Limite de sortie définie par l'application | Non | 768 tokens |
| Réflexion étendue explicitement désactivée | Non | Oui |
| Validation backend | Présence de candidats | JSON, identifiants, correspondance textuelle, bornes |

Aucune mesure avant/après de latence ou VRAM sur RTX 4060 n'est disponible dans
l'environnement de développement. Ne pas déduire un gain chiffré de ce tableau.
Le schéma et les citations ajoutent du texte; les plafonds et l'absence de réflexion
peuvent en économiser. Utiliser le banc local ci-dessous pour observer le résultat.

## Régression sur PDF de démonstration

`backend/evaluation/grounding_cases.json` contient uniquement des textes originaux,
synthétiques et non confidentiels : cartes de couleur et bacs de rangement.
Sept cas couvrent réponse présente simple/multiple, absente proche/hors sujet,
contradiction ambiguë, condition/négation et injection. Le générateur crée de
vrais PDF temporaires avec PyMuPDF, extraits par le même pipeline que les PDF
utilisateur. Aucun PDF réel ni binaire PDF n'est ajouté au dépôt.

Tests CI, sans serveur Ollama et sans téléchargement de modèle :

```bash
pip install -r backend/requirements-dev.txt
cd backend
python -m pytest -q
```

Qdrant est embarqué/simulé, Ollama est simulé via `httpx.MockTransport`. Ces tests
vérifient le contrat et les scénarios scriptés, **pas la compréhension du modèle**.

Évaluation réelle facultative : Ollama doit déjà tourner avec `qwen3:8b` et
`nomic-embed-text` installés. Aucun `pull` n'est exécuté :

```bash
cd backend
python -m evaluation.run_grounding --live \
  --ollama-url http://localhost:11434 \
  --model qwen3:8b --embedding-model nomic-embed-text
```

Chaque cas utilise ses PDF temporaires, ses verrous et un Qdrant embarqué en
mémoire. Il ne lit ni ne modifie les documents/collections de production.
Les réglages `MAX_*` et `OLLAMA_NUM_*` peuvent être fournis par variables
d'environnement. Les sorties JSONL donnent la réponse complète, les citations,
le résultat attendu, le nombre de générations, le temps de question-réponse,
les compteurs/durées Ollama et un échantillon de mémoire GPU résidente (`/api/ps`).
La mémoire résidente mesurée après la question n'est pas un pic de VRAM.
Le code de sortie est 1 si un scénario ne correspond pas à l'attendu; une erreur
réseau/modèle manquant interrompt l'exécution et n'est pas comptée comme un succès.

Pour comparer les versions sur votre machine, utiliser les mêmes PDF/questions,
modèles, quantifications et services. Séparer démarrage à froid et passages à
chaud, relever plusieurs exécutions et observer les pics avec `nvidia-smi`.
La sémantique de `grounded` avant ce changement n'est pas comparable : vérifier
manuellement chaque citation et consigner les faux refus/fausses réponses.

## Intégration avec la recherche hybride et la régression RAG

La recherche conserve les ancres exactes, FTS5, RRF et les passages structurés de
`main`. `CONTEXT_MAX_CHARS` borne la présélection sérialisée; `MAX_CONTEXT_CHARS`
et le budget UTF-8 bornent ensuite le texte effectivement envoyé. À la première
limite, la sélection s'arrête : un candidat exact trop long n'est pas remplacé
par un voisin plus court. `excerpt` conserve les sauts de ligne et blocs originaux.

`passage_id` reste l'UUID Qdrant utilisable par les clients; `source_id` est la
référence des citations. Les révisions sont recontrôlées après génération : un
document modifié ou retiré entre-temps fait refuser la réponse. L'avertissement
sur l'absence de priorité entre plusieurs documents est conservé dans
`safety_notice`. Les deux jeux de régression sont préservés sous des noms distincts :
`cases.json` pour les PDF ORION et `grounding_cases.json` pour les cartes/bacs.
