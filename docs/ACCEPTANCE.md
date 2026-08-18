# Recette ULTEx

## Données et calculs

- [ ] Trois dates correspondent à Ads Manager pour dépenses, résultats, impressions, clics et coût/résultat.
- [ ] Les comparaisons utilisent les jours précédents et gèrent les divisions par zéro avec « non calculable ».
- [ ] Une deuxième synchronisation de la même date ne crée aucun doublon.
- [ ] Les quatre niveaux (compte, campagne, ensemble, publicité) sont présents.
- [ ] Chaque type d’action utilisé est mappé à une seule mesure, vérifiée humainement.

## Erreurs et sécurité

- [ ] Pagination, réponse vide, jeton invalide, limite API, panne réseau et réponse partielle ont été testés.
- [ ] Un rapport complet n’est pas généré après une synchronisation partielle.
- [ ] Les secrets sont absents des journaux, sauvegardes lisibles, réponses API et exports.
- [ ] Les pages et API privées refusent les utilisateurs non connectés.
- [ ] Cinq échecs de connexion bloquent temporairement le couple utilisateur/adresse.

## Rapports

- [ ] Le PDF contient les dix rubriques et passe une inspection visuelle page par page.
- [ ] L’Excel contient les sept feuilles demandées.
- [ ] PDF et Excel proviennent du même `snapshot_hash`.
- [ ] Les IDs d’insight et de payload permettent de remonter à la réponse Meta brute.

## Exploitation

- [ ] Trois cycles quotidiens consécutifs terminent dans les quinze minutes.
- [ ] Une sauvegarde est créée et restaurée dans une base temporaire.
- [ ] Un redémarrage du serveur ne duplique pas le cycle planifié.
- [ ] La direction ou le responsable digital signe la concordance Ads Manager.

