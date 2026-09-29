# Sécurité opérationnelle

Instruct IA est un outil de recherche documentaire, pas un système de contrôle ni une autorité procédurale.

## Principes obligatoires

- Répondre uniquement avec les passages fournis par le moteur de recherche.
- Ne jamais inventer une étape, une valeur, un EPI, un réglage ou une consigne.
- Indiquer clairement lorsqu'une information n'est pas retrouvée.
- Afficher le document et la page utilisés.
- Faire vérifier la version officielle avant l'exécution d'une procédure.
- Ne jamais relier directement une réponse générée à une commande de machine.

## Déploiement

Les comptes, groupes, audit et sauvegardes sont décrits dans [PME.md](PME.md).
Avant un usage réel, validez leur configuration et prévoyez au minimum :

- authentification et autorisation par rôle;
- versionnement et statut d'approbation des documents;
- retrait automatique des versions obsolètes;
- journal d'audit;
- tests de non-régression avec questions critiques;
- procédure de validation humaine;
- sauvegarde et restauration de Qdrant;
- analyse de risques adaptée au milieu de travail.

## Documents

Les documents chargés restent sous la responsabilité de l'utilisateur. Ne publiez pas de contenu confidentiel, de donnée personnelle, de secret commercial ou de document dont vous ne détenez pas les droits.


## Citations et contenu non fiable

`grounded: true` indique une sélection d'extraits dont les références et le texte
ont été validés, pas une certification de vérité, de pertinence ou de sécurité.
Lire les conditions du passage complet et vérifier le document officiel reste
obligatoire. Un score vectoriel n'est jamais une probabilité de vérité.

Le texte des PDF et la question sont des données non fiables. Ils ne peuvent
fournir des règles système, des noms de sources générés ou des outils exécutables.
L'application n'exécute aucune action issue d'un PDF ou d'une réponse. Le filtre
d'injections évidentes est une protection supplémentaire imparfaite; des attaques
obfusquées et des faux positifs restent possibles. Voir [GROUNDING.md](GROUNDING.md)
pour le contrat exact, les refus, les limites et les tests reproductibles.
