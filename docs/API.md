# API interne v1

Toutes les routes, sauf `/health`, nécessitent la session locale Django et la protection CSRF pour les écritures.

| Méthode | Route | Usage |
|---|---|---|
| POST | `/api/v1/meta/test` | Tester le compte et le jeton chiffré |
| GET/POST | `/api/v1/syncs` | Lister ou démarrer une synchronisation |
| GET | `/api/v1/syncs/{id}` | Suivre l’état et les erreurs |
| GET | `/api/v1/dashboard?connection=ID&start=YYYY-MM-DD&end=YYYY-MM-DD` | Lire le résumé agrégé d’un compte |
| GET | `/api/v1/insights?level=&start=&end=` | Lire les métriques normalisées |
| GET/POST | `/api/v1/reports` | Lister ou générer un rapport |
| GET | `/api/v1/reports/{id}/download?format=pdf|excel` | Télécharger une version |
| GET/PATCH | `/api/v1/settings` | Lire ou modifier les paramètres non secrets |
| GET | `/health` | Santé minimale du service et de la base |

Les API n’acceptent aucune opération de création, modification ou pause de campagne Meta.

Le paramètre de requête `connection=ID` sélectionne un compte pour les lectures et le conserve dans la session. Les écritures JSON `/meta/test`, `/syncs` et `/reports` acceptent `connection_id`; lorsqu’il est absent, la connexion conservée dans la session est utilisée.
