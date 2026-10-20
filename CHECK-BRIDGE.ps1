[CmdletBinding()]
param(
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $scriptDir 'bridge-config.json'
$bridgePort = 4315
if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    try {
        $bridgeConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        if ($null -ne $bridgeConfig.port) {
            $bridgePort = [int]$bridgeConfig.port
        }
    }
    catch {
        $bridgePort = 4315
    }
}
$BridgeUrl = 'http://127.0.0.1:{0}' -f $bridgePort
$exitCode = 1

try {
    $result = Invoke-RestMethod -Uri ($BridgeUrl + '/v1/health') -Method Get -TimeoutSec 5
    $result | ConvertTo-Json -Depth 4
    Write-Host 'Bridge connection: PASS'
    $exitCode = 0
}
catch {
    Write-Host 'Bridge connection: FAIL. Start the bridge and retry.' -ForegroundColor Red
    $exitCode = 1
}
finally {
    if (-not $NoPause) {
        [void](Read-Host 'Press Enter to close')
    }
}

exit $exitCode
