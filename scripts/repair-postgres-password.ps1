$ErrorActionPreference = "Stop"

$projectDirectory = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$environmentFile = Join-Path $projectDirectory ".env"

if (-not (Test-Path -LiteralPath $environmentFile)) {
    throw "The .env file was not found at $environmentFile"
}

$configuration = @{}
Get-Content -LiteralPath $environmentFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) {
        return
    }
    if ($line -match "^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$") {
        $configuration[$Matches[1]] = $Matches[2].Trim()
    }
}

$requiredSettings = @("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")
foreach ($settingName in $requiredSettings) {
    if (-not $configuration.ContainsKey($settingName) -or
        [string]::IsNullOrWhiteSpace($configuration[$settingName])) {
        throw "$settingName is missing from .env"
    }
}

$databaseUser = $configuration["POSTGRES_USER"]
$databaseName = $configuration["POSTGRES_DB"]
$databasePassword = $configuration["POSTGRES_PASSWORD"]

if ($databaseUser -notmatch "^[A-Za-z_][A-Za-z0-9_.-]*$") {
    throw "POSTGRES_USER contains unsupported characters."
}
if ($databaseName -notmatch "^[A-Za-z_][A-Za-z0-9_.-]*$") {
    throw "POSTGRES_DB contains unsupported characters."
}

$escapedUser = $databaseUser.Replace('"', '""')
$escapedPassword = $databasePassword.Replace("'", "''")
$passwordUpdateSql = "ALTER ROLE `"$escapedUser`" WITH PASSWORD '$escapedPassword';"

$startingDirectory = Get-Location
try {
    Set-Location -LiteralPath $projectDirectory

    $passwordUpdateSql |
        docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U $databaseUser -d $databaseName
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL rejected the password update."
    }

    Write-Host "PostgreSQL password synchronized with .env."
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose could not start all services."
    }

    docker compose ps
}
finally {
    Set-Location -LiteralPath $startingDirectory
}
