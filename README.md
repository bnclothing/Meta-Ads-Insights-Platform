# ULTEx Meta Reports

Application locale et privée de pilotage Meta Ads. Elle synchronise plusieurs comptes publicitaires en lecture seule, calcule les indicateurs sans IA, détecte les variations à examiner et produit des rapports PDF/Excel versionnés.

## Ce que contient le MVP

- Tableau de bord par période en français, avec sélection du compte publicitaire.
- Suivi des leads du classeur DATA par feuille et période, avec lecture Google Sheets en temps réel.
- Détail compte, campagne, ensemble et publicité, avec filtres HTMX sans dépendance CDN.
- Synchronisation Meta Marketing API à chaque chargement du tableau de bord et toutes les heures, avec pagination, historique brut et tentatives automatiques.
- Mesure de résultat unique et vérifiable; aucune addition d’actions Meta qui se chevauchent.
- Alertes déterministes avec seuils modifiables.
- Rapports immuables PDF/Excel et traçabilité vers les données API.
- Authentification locale, chiffrement AES-256-GCM des secrets, HTTPS interne, sauvegardes et journal d’audit.

## Installation sur le serveur local

Prérequis: Docker Desktop ou Docker Engine avec Compose, une adresse IP locale stable et un accès sortant HTTPS vers Meta.

1. Dans PowerShell, exécuter `./scripts/initialize.ps1` et conserver le mot de passe administrateur affiché.
2. Ajouter `meta-reports.local` avec l’adresse IP du serveur dans le DNS local ou le fichier `hosts` de chaque poste.
3. Lancer `docker compose up -d --build`.
4. Ouvrir `https://meta-reports.local` et accepter/installer l’autorité locale Caddy selon la procédure de [docs/OPERATIONS.md](docs/OPERATIONS.md).
5. Se connecter, ouvrir **Paramètres**, ajouter chaque compte Meta puis tester chaque connexion.
6. Sélectionner un compte dans l’en-tête et lancer son import initial de 90 jours.



## Développement sans Docker

Une base SQLite est utilisée quand les variables PostgreSQL sont absentes.

```powershell
py -m venv .venv
./.venv/Scripts/python -m pip install -r requirements.txt
./.venv/Scripts/python manage.py migrate
./.venv/Scripts/python manage.py bootstrap_admin --password "un-mot-de-passe-fort"
./.venv/Scripts/python manage.py seed_demo --create-report
./.venv/Scripts/python manage.py runserver
```

Les données de démonstration ne contiennent aucune donnée personnelle ou réelle.

## Vérification locale

```powershell
./.venv/Scripts/python manage.py check
./.venv/Scripts/python manage.py makemigrations --check --dry-run
./.venv/Scripts/python manage.py test
./.venv/Scripts/python manage.py collectstatic --noinput
```

La suite automatisée couvre notamment le chiffrement, la suppression des secrets dans les pages Meta brutes, la pagination, l’idempotence, les imports partiels, les métriques manquantes, les alertes, la parité PDF/Excel, l’échec de génération, l’authentification et CSRF. La validation avec des données réelles et le pilote de trois jours restent des étapes d’exploitation: ils nécessitent le compte publicitaire, le jeton système et une comparaison humaine avec Ads Manager.

## Documentation

- [Connexion et validation Meta](docs/META_ONBOARDING.md)
- [Exploitation, sauvegardes et sécurité](docs/OPERATIONS.md)
- [Recette et critères d’acceptation](docs/ACCEPTANCE.md)
- [API interne](docs/API.md)
- [Connexion du classeur DATA Google Sheets](docs/DATA_SHEET_SETUP.md)
