$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (-not $env:DOCKER_CONFIG) { $env:DOCKER_CONFIG = Join-Path $taskRoot '.cache/docker' }
& docker compose --project-directory $taskRoot stop
exit $LASTEXITCODE
