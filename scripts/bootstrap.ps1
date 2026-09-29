$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $taskRoot '.env'
if (Test-Path -LiteralPath $envFile) {
    Write-Output 'Local .env already exists; leaving credentials unchanged.'
    exit 0
}
function New-LocalSecret {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}
$lines = @(
    "POSTGRES_PASSWORD=$(New-LocalSecret)"
    "GATEWAY_DB_PASSWORD=$(New-LocalSecret)"
    "TOOLS_DB_PASSWORD=$(New-LocalSecret)"
    "TOOL_INTERNAL_KEY=$(New-LocalSecret)"
    "ALPHA_KEY=$(New-LocalSecret)"
    "BETA_KEY=$(New-LocalSecret)"
    'POSTGRES_PORT=55438'
)
[IO.File]::WriteAllLines($envFile, $lines, (New-Object Text.UTF8Encoding $false))
Write-Output 'Generated ignored .env with local demo credentials; values are not printed.'
