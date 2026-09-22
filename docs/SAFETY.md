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

Avant un usage réel, prévoyez au minimum :

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

