param([string[]]$TestArguments = @('-q'))
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$values = @{}
foreach ($line in [IO.File]::ReadAllLines((Join-Path $taskRoot '.env'))) {
    if ($line -match '^([A-Z_]+)=(.*)$') { $values[$matches[1]] = $matches[2] }
}
$port = $values['POSTGRES_PORT']
$env:TOOLGATE_TEST_DATABASE_URL = "postgresql+psycopg://toolgate_gateway:$($values['GATEWAY_DB_PASSWORD'])@127.0.0.1:$port/toolgate_test"
$env:MOCK_TOOLS_TEST_DATABASE_URL = "postgresql+psycopg://toolgate_tools:$($values['TOOLS_DB_PASSWORD'])@127.0.0.1:$port/toolgate_test"
$env:PYTHONUTF8 = '1'
& (Join-Path $taskRoot '.venv\Scripts\python.exe') -m pytest @TestArguments
exit $LASTEXITCODE
