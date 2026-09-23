# Connexion du classeur DATA Google Sheets

La page **DATA Sheet** lit uniquement les trois feuilles utiles au suivi des leads : **Landing page**, **Clicks** et **Dossier IA**. Elle ne modifie jamais le classeur. L’option **Vue consolidée** réunit les trois feuilles dans une seule chronologie tout en conservant la feuille d’origine de chaque ligne. Le KPI **Contacts uniques** déduplique les numéros présents dans plusieurs feuilles.

## Règles de date appliquées

- **Landing page** : à partir de la ligne `LE 23/06/2025`, chaque lead hérite de la date du séparateur `LE …` précédent, jusqu’au séparateur suivant.
- **Clicks** : à partir du 04/05/2024, chaque lead hérite de la date du séparateur précédent, jusqu’au séparateur suivant.
- **Dossier IA** : la date d’entrée du lead provient de `📅 Date — Statut 1`, y compris lorsque la valeur contient une heure et le suffixe `UTC`.

La page affiche seulement la date d’entrée, le code client, le nom lorsqu’il existe, le contact, le service ou la source, le produit, la quantité, le statut actuel et une information de suivi courte.

## Connexion recommandée

Utiliser un **compte de service Google en lecture seule**. Le classeur contient des numéros de téléphone : il ne doit pas être publié sur le Web ni partagé avec « toute personne disposant du lien ».

1. Dans Google Cloud, créer ou sélectionner un projet puis activer **Google Sheets API**.
2. Créer un compte de service et télécharger sa clé JSON.
3. Dans le classeur DATA, cliquer sur **Partager** et ajouter l’adresse `client_email` de cette clé avec le rôle **Lecteur**.
4. Convertir le fichier JSON en une valeur Base64 sur une seule ligne. Dans PowerShell :

   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\chemin-securise\google-sheets-service-account.json"))
   ```

5. Dans `.env`, conserver l’identifiant du classeur et coller la valeur obtenue :

   ```dotenv
   GOOGLE_SHEET_ID=1mjjAgCvllHzTvraF5Oye6wlQCON6kMXqQwKoMzpiAHU
   GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=valeur-base64-ici
   GOOGLE_SHEETS_CACHE_SECONDS=60
   ```

6. Redémarrer l’application :

   ```powershell
   docker compose up -d --build web
   ```

Le tableau de bord relit la feuille au plus toutes les 60 secondes afin de préserver les quotas Google. Le bouton **Actualiser maintenant** ignore cette courte mémoire et récupère immédiatement les dernières lignes.

Le serveur doit pouvoir joindre `oauth2.googleapis.com` et `sheets.googleapis.com` en HTTPS. La clé JSON reste dans `.env`, déjà exclu de Git, et n’est jamais renvoyée au navigateur.

## Développement avec le fichier Excel

Pour vérifier l’interface sans joindre Google, définir temporairement `DATA_SHEET_LOCAL_XLSX` avec le chemin absolu de `DATA.xlsx`. Ce mode est un aperçu local ; la production doit utiliser la connexion Google ci-dessus pour obtenir les données à jour.
