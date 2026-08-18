$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root '.env'
$Example = Join-Path $Root '.env.example'

if (Test-Path -LiteralPath $EnvFile) {
    throw '.env existe déjà. Le script refuse de l’écraser.'
}

$secret = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48)).Replace('+','-').Replace('/','_')
$encryption = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).Replace('+','-').Replace('/','_')
$dbPassword = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(24)).Replace('+','-').Replace('/','_')
$adminPassword = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(18)).Replace('+','-').Replace('/','_')
$content = Get-Content -LiteralPath $Example -Raw
$content = $content.Replace('replace-with-a-long-random-value', $secret)
$content = $content.Replace('replace-with-generated-base64-key', $encryption)
$content = $content.Replace('replace-with-a-strong-database-password', $dbPassword)
$content = $content.Replace('replace-with-a-strong-password', $adminPassword)
Set-Content -LiteralPath $EnvFile -Value $content -Encoding UTF8

Write-Host 'Configuration locale créée dans .env.'
Write-Host "Mot de passe administrateur initial: $adminPassword"
Write-Host 'Conservez-le dans un gestionnaire de mots de passe, puis lancez: docker compose up -d --build'

