# Exploitation locale

## Réseau et HTTPS

Le service n’expose que les ports 80/443 sur le réseau local. PostgreSQL et Redis restent dans le réseau Docker privé. Caddy génère une autorité interne; installer son certificat racine sur les postes ULTEx autorisés afin d’éviter l’avertissement navigateur.

Le certificat est disponible dans le volume `caddy_data`, au chemin `pki/authorities/local/root.crt`. Le copier depuis le conteneur Caddy puis l’installer dans les autorités racines de confiance du système.

Le préchargement HSTS public reste volontairement désactivé: `meta-reports.local` est un nom interne et ne doit pas être soumis à la liste de préchargement des navigateurs. Caddy et Django imposent néanmoins HTTPS et HSTS sur le réseau local.

## Cycle quotidien

- À chaque chargement du tableau de bord, tous les comptes Meta actifs et connectés sont actualisés automatiquement. Le rechargement qui affiche le résultat est marqué afin de ne pas créer une boucle de synchronisation.
- Toutes les heures, le scheduler actualise la fenêtre glissante des 28 derniers jours jusqu’à aujourd’hui. Un cycle déjà actif ou terminé depuis moins d’une heure pour la même fenêtre est réutilisé.
- À l’heure définie dans les paramètres (08:00 par défaut), le scheduler lance la synchronisation du jour précédent et réimporte les 28 derniers jours.
- Les tentatives réseau sont espacées de 1, 5 puis 30 minutes.
- Un rapport est généré uniquement après une synchronisation complète.
- Les valeurs absentes restent absentes; elles ne sont jamais transformées en zéro.

## Sauvegardes

Une sauvegarde est lancée chaque nuit à 02:30. Sept copies quotidiennes et quatre copies hebdomadaires sont conservées dans `backups/`.

- Sauvegarde manuelle: `./scripts/backup.ps1`
- Test de restauration isolé: `./scripts/restore-test.ps1 -BackupPath ./backups/daily/<fichier>.dump`

Le test crée une base temporaire, restaure la sauvegarde, vérifie la table d’insights puis supprime uniquement la base temporaire. Réaliser ce test au moins une fois par trimestre.

## Mise à jour et redémarrage

1. Lancer une sauvegarde manuelle.
2. Construire la nouvelle image avec `docker compose build`.
3. Exécuter les tests dans l’image.
4. Redémarrer avec `docker compose up -d`.
5. Contrôler `/health`, la dernière synchronisation et un export.

La vérification de l’image doit inclure `docker compose config`, `docker compose build`, puis `docker compose run --rm web python manage.py test`. Le conteneur web contient Chromium pour le PDF et le client PostgreSQL requis par les sauvegardes.

## Incidents

- **Jeton expiré/invalide:** remplacer le jeton dans Paramètres; le rapport reste disponible avec les dernières données horodatées.
- **Meta indisponible:** les trois tentatives automatiques s’exécutent; l’échec reste visible et le rapport complet n’est pas produit.
- **Synchronisation partielle:** relancer après correction; le moteur ne classe pas les métriques manquantes comme zéro.
- **Disque faible:** exporter ou déplacer les anciennes sauvegardes, sans supprimer les volumes de base de données.
