$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    docker compose exec -T web python manage.py backup_database
} finally {
    Pop-Location
}

