# Carnet

Suivi détaillé de parties de golf : score, putts, distance du drive, distance
restante après le drive, passage en bunker, coup d'approche, et si le putt est
joué depuis le green ou en dehors.

## Stack

Flask + SQLite (fichier unique, monté sur un volume persistant), servi par
gunicorn dans un seul conteneur. Frontend en HTML/JS vanilla, sans dépendance
externe.

## Variables d'environnement

- `APP_PIN` : code d'accès (protège l'app, exposée sur un sous-domaine public)
- `SECRET_KEY` : clé de session Flask
- `DB_PATH` : chemin du fichier SQLite (doit pointer vers un volume persistant)

## Déploiement

Déployé sur Coolify. Le volume `/data` doit être monté en persistant pour ne
pas perdre les données à chaque redéploiement.
