$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (-not $env:DOCKER_CONFIG) { $env:DOCKER_CONFIG = Join-Path $taskRoot '.cache/docker' }
$taskProject = 'toolgate-verify-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$env:POSTGRES_PORT = '58438'
$env:API_A_PORT = '58581'
$env:API_B_PORT = '58582'
$env:TOOLS_PORT = '58583'
$values = @{}
foreach ($line in [IO.File]::ReadAllLines((Join-Path $taskRoot '.env'))) {
    if ($line -match '^([A-Z_]+)=(.*)$') { $values[$matches[1]] = $matches[2] }
}
try {
    & docker compose --project-directory $taskRoot -p $taskProject up -d --build --wait --wait-timeout 120
    if ($LASTEXITCODE -ne 0) { throw 'Clean Compose startup failed.' }
    foreach ($port in @('58581', '58582')) {
        $ready = Invoke-RestMethod "http://127.0.0.1:$port/health/ready"
        if ($ready.status -ne 'ready') { throw 'Clean API readiness failed.' }
    }
    $headers = @{ Authorization = "Bearer $($values['ALPHA_KEY'])"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
    $body = '{"tool":"catalog.lookup","arguments":{"sku":"SKU-001"}}'
    $call = Invoke-RestMethod 'http://127.0.0.1:58581/v1/calls' -Method Post -Headers $headers -ContentType 'application/json' -Body $body
    $replay = Invoke-RestMethod 'http://127.0.0.1:58582/v1/calls' -Method Post -Headers $headers -ContentType 'application/json' -Body $body
    $budget = Invoke-RestMethod 'http://127.0.0.1:58582/v1/budget' -Headers $headers
    if ($call.state -ne 'succeeded' -or $call.id -ne $replay.id -or $budget.used -ne 1 -or $budget.inflight -ne 0) {
        throw 'Clean end-to-end call/replay verification failed.'
    }
    Write-Output 'Clean fresh-volume startup, two API readiness, real tool call and cross-API replay: PASS.'
} finally {
    # This identifier was generated above; only this script-owned project and volume are removed.
    & docker compose --project-directory $taskRoot -p $taskProject down -v --remove-orphans
    if ($LASTEXITCODE -ne 0) { throw 'Cleanup of the script-owned verification project failed.' }
}
