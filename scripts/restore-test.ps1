param([Parameter(Mandatory=$true)][string]$BackupPath)
$ErrorActionPreference = 'Stop'
$resolved = (Resolve-Path -LiteralPath $BackupPath).Path
$root = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if (-not $resolved.StartsWith((Join-Path $root 'backups'), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Le fichier doit se trouver dans le dossier backups du projet.'
}
if ([IO.Path]::GetExtension($resolved) -ne '.dump') {
    throw 'Ce test attend une sauvegarde PostgreSQL au format .dump.'
}
Push-Location $root
try {
    $containerPath = '/tmp/restore-test.dump'
    docker compose cp $resolved "postgres:$containerPath"
    docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists meta_reports_restore_test && createdb -U "$POSTGRES_USER" meta_reports_restore_test'
    docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d meta_reports_restore_test --no-owner /tmp/restore-test.dump'
    docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d meta_reports_restore_test -c "SELECT COUNT(*) AS insight_rows FROM reporting_insightdaily;"'
    docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" meta_reports_restore_test'
    Write-Host 'Test de restauration réussi.'
} finally {
    Pop-Location
}

