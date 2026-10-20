[CmdletBinding()]

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
$logPath = Join-Path $scriptDir 'LAST-IMAGE-TEST.log'
$previousLog = Join-Path $scriptDir 'LAST-IMAGE-TEST.previous.log'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outputPath = Join-Path $scriptDir ("image-test-output-{0}.png" -f $stamp)
$exitCode = 1
$logReady = $false

function Write-TestLog([string]$Message) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    if ($logReady) {
        try {
            [IO.File]::AppendAllText($logPath, $line + [Environment]::NewLine, (New-Object Text.UTF8Encoding($true)))
        }
        catch {
            $script:logReady = $false
        }
    }
}

try {
    if (Test-Path -LiteralPath $logPath -PathType Leaf) {
        Copy-Item -LiteralPath $logPath -Destination $previousLog -Force
    }
    [IO.File]::WriteAllText($logPath, '', (New-Object Text.UTF8Encoding($true)))
    $logReady = $true
    Write-TestLog ('Bridge URL: ' + $BridgeUrl)
    $health = Invoke-RestMethod -Uri ($BridgeUrl + '/v1/health') -Method Get -TimeoutSec 5
    Write-TestLog ('Health: status={0}, provider={1}, image_generate={2}' -f $health.status, $health.provider, $health.capabilities.image_generate)

    $requestBody = @{
        prompt = 'A cinematic medieval castle at sunrise, no text'
        width = 640
        height = 320
    } | ConvertTo-Json -Compress

    Write-TestLog 'Calling POST /v1/image/generate (640x320). This can take several minutes.'
    try {
        $result = Invoke-RestMethod -Uri ($BridgeUrl + '/v1/image/generate') -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($requestBody)) -TimeoutSec 600
    }
    catch {
        $status = 'unavailable'
        $detail = ''
        if ($null -ne $_.Exception.Response) {
            try { $status = [int]$_.Exception.Response.StatusCode }
            catch { }
            try {
                $reader = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
                $parsed = $reader.ReadToEnd() | ConvertFrom-Json
                $reader.Dispose()
                if ($null -ne $parsed.error) {
                    $detail = ' code=' + $parsed.error.code + ' message=' + $parsed.error.message
                }
            }
            catch { }
        }
        Write-TestLog ('Image endpoint failed: HTTP=' + $status + $detail)
        throw
    }

    if ($null -eq $result -or [string]::IsNullOrWhiteSpace([string]$result.image)) {
        throw 'Image endpoint returned no image field.'
    }
    $png = [Convert]::FromBase64String([string]$result.image)
    if ($png.Length -lt 24) { throw 'Returned image is shorter than a PNG header.' }
    $signature = @(0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A)
    for ($i = 0; $i -lt $signature.Count; $i++) {
        if ($png[$i] -ne $signature[$i]) { throw 'Returned image has an invalid PNG signature.' }
    }
    $width = ([int64]$png[16] * 16777216) + ([int64]$png[17] * 65536) + ([int64]$png[18] * 256) + [int64]$png[19]
    $height = ([int64]$png[20] * 16777216) + ([int64]$png[21] * 65536) + ([int64]$png[22] * 256) + [int64]$png[23]
    [IO.File]::WriteAllBytes($outputPath, $png)
    $hash = (Get-FileHash -LiteralPath $outputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-TestLog ('PASS: PNG={0}x{1}, bytes={2}, sha256={3}' -f $width, $height, $png.Length, $hash)
    Write-TestLog ('Saved: ' + (Split-Path -Leaf $outputPath))
    $exitCode = 0
}
catch {
    $reason = [string]$_.Exception.Message
    if (-not [string]::IsNullOrWhiteSpace($scriptDir)) { $reason = $reason.Replace($scriptDir, '<adapter>') }
    Write-TestLog ('FAIL: ' + $reason)
    Write-TestLog 'Bridge console에서 같은 시각의 request=image_generate/error 줄도 함께 확인하세요.'
    $exitCode = 1
}
finally {
    Write-TestLog ('Exit code: ' + $exitCode)
    Write-TestLog 'Log: LAST-IMAGE-TEST.log'
}

exit $exitCode
