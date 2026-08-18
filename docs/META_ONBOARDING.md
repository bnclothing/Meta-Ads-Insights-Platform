# Connexion et validation Meta

## 1. Préparer l’accès en lecture seule

1. Créer ou utiliser une application appartenant à ULTEx dans Meta for Developers.
2. Ajouter le produit Marketing API.
3. Dans le portefeuille Business ULTEx, créer un utilisateur système et lui attribuer le compte publicitaire avec la tâche de consultation/analyse.
4. Générer un jeton avec uniquement `ads_read`. Ne pas demander `ads_management`.
5. Relever l’identifiant du compte publicitaire dans Ads Manager; l’application accepte la valeur avec ou sans `act_`.
6. Dans **Paramètres → Comptes publicitaires**, cliquer sur **Ajouter un compte**, saisir l’App ID, le secret, le jeton système et l’identifiant du compte, puis cliquer sur **Tester la connexion**.

Les secrets sont chiffrés avant stockage. Le jeton complet n’est jamais renvoyé au navigateur, inscrit dans les journaux ou ajouté aux exports.

Répéter cette opération pour chaque compte publicitaire. Le sélecteur **Compte Meta** dans l’en-tête détermine les données visibles sur le tableau de bord, la performance, les rapports et les appels API. La sélection est conservée dans la session. Les cycles quotidiens synchronisent séparément toutes les connexions actives.

## 2. Backfill initial de 90 jours

Après un test réussi, lancer une synchronisation via `POST /api/v1/syncs`:

```json
{
  "connection_id": 2,
  "start": "2026-05-19",
  "end": "2026-08-16",
  "levels": ["account", "campaign", "adset", "ad"],
  "generate_report": false
}
```

Les périodes supérieures à 31 jours utilisent un rapport Insights asynchrone. L’application conserve les pages brutes, puis enregistre les métriques normalisées dans une transaction. Un niveau indisponible produit un statut partiel; aucun rapport complet n’est alors généré.

## 3. Valider la mesure de résultat

Pour trois journées représentatives:

1. Ouvrir Ads Manager avec le même fuseau, la même fenêtre d’attribution et l’option de rapport par impression.
2. Comparer compte, campagne, ensemble et publicité.
3. Relever l’unique `action_type` qui correspond à la colonne Résultats pour chaque objectif/campagne.
4. Dans **Paramètres → Mesure de résultat principale**, créer la portée, l’action et le libellé.
5. Cocher **vérifiée** uniquement après égalité avec Ads Manager, aux arrondis d’affichage près.

Tant que cette étape n’est pas terminée, les rapports utilisent le libellé **Meta résultat**. Les actions qui se chevauchent ne sont jamais additionnées.

## 4. Versions et maintenance API

L’intégration cible `v25.0` par configuration. Avant toute montée de version:

1. Cloner la base de test ou utiliser des fixtures anonymisées.
2. Modifier `META_GRAPH_API_VERSION`.
3. Rejouer les tests du connecteur et un backfill de trois jours.
4. Comparer les résultats aux rapports de production avant la bascule.
