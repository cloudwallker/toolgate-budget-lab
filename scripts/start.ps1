$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (-not $env:DOCKER_CONFIG) { $env:DOCKER_CONFIG = Join-Path $taskRoot '.cache/docker' }
& (Join-Path $PSScriptRoot 'bootstrap.ps1')
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& docker compose --project-directory $taskRoot up -d --build --wait --wait-timeout 120
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output 'API A: http://127.0.0.1:58081 ; API B: http://127.0.0.1:58082'
Write-Output 'Existing volumes, credentials, call history and budgets are preserved.'
