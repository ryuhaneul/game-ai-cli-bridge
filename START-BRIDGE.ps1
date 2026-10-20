[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('codex', 'claude', 'grok')]
    [string]$Provider,
    [switch]$KeepImageJobs,
    [string]$DumpCliEvents
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bridgeScript = Join-Path $scriptDir 'player2_cli_bridge.py'
$logPath = Join-Path $scriptDir 'LAST.log'
$secretDir = Join-Path $env:LOCALAPPDATA 'Player2CLIBridge'
$secretPath = Join-Path $secretDir 'openai-key.dpapi'
$keyPointer = [IntPtr]::Zero
$fishSecretPath = Join-Path $secretDir 'fish-audio-key.dpapi'
$fishKeyPointer = [IntPtr]::Zero
$exitCode = 1

try {
    if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
        try {
            $encrypted = [IO.File]::ReadAllText($secretPath).Trim()
            $secureKey = ConvertTo-SecureString -String $encrypted
            $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
            $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
            if ([string]::IsNullOrWhiteSpace($plainKey)) {
                throw '저장된 키가 비어 있습니다.'
            }
            $env:PLAYER2_OPENAI_EMBEDDING_KEY = $plainKey
            $plainKey = $null
            Write-Host 'OpenAI embedding key: configured (Windows DPAPI)'
        }
        catch {
            Remove-Item Env:PLAYER2_OPENAI_EMBEDDING_KEY -ErrorAction SilentlyContinue
            Write-Warning '저장된 OpenAI API key를 읽지 못했습니다. Embedding 없이 Bridge를 시작합니다.'
        }
    }
    elseif ([string]::IsNullOrWhiteSpace($env:PLAYER2_OPENAI_EMBEDDING_KEY)) {
        Write-Host 'OpenAI embedding key: not configured'
    }
    else {
        Write-Host 'OpenAI embedding key: configured (process environment)'
    }

    if (Test-Path -LiteralPath $fishSecretPath -PathType Leaf) {
        try {
            $fishEncrypted = [IO.File]::ReadAllText($fishSecretPath).Trim()
            $fishSecureKey = ConvertTo-SecureString -String $fishEncrypted
            $fishKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($fishSecureKey)
            $fishPlainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($fishKeyPointer)
            if ([string]::IsNullOrWhiteSpace($fishPlainKey)) {
                throw '저장된 키가 비어 있습니다.'
            }
            $env:PLAYER2_FISH_AUDIO_KEY = $fishPlainKey
            $fishPlainKey = $null
            Write-Host 'Fish Audio key: configured (Windows DPAPI)'
        }
        catch {
            Remove-Item Env:PLAYER2_FISH_AUDIO_KEY -ErrorAction SilentlyContinue
            Write-Warning '저장된 Fish Audio API key를 읽지 못했습니다. Fish TTS 없이 Bridge를 시작합니다.'
        }
    }
    elseif ([string]::IsNullOrWhiteSpace($env:PLAYER2_FISH_AUDIO_KEY)) {
        Write-Host 'Fish Audio key: not configured'
    }
    else {
        Write-Host 'Fish Audio key: configured (process environment)'
    }

    $arguments = @($bridgeScript, '--provider', $Provider)
    if ($KeepImageJobs) {
        $arguments += '--keep-image-jobs'
    }
    if (-not [string]::IsNullOrWhiteSpace($DumpCliEvents)) {
        $arguments += @('--dump-cli-events', $DumpCliEvents)
    }

    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        & $py.Source '-3' @arguments 2>&1 | Tee-Object -FilePath $logPath
    }
    else {
        $python = Get-Command 'python' -ErrorAction SilentlyContinue
        if ($null -eq $python) {
            throw 'Python 3를 찾지 못했습니다.'
        }
        & $python.Source @arguments 2>&1 | Tee-Object -FilePath $logPath
    }
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 1
    }
}
catch {
    Write-Host 'Bridge 시작 실패. 설정, Python 및 저장된 credential 상태를 확인하세요.' -ForegroundColor Red
    try {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $safeFailure = '[' + [DateTime]::UtcNow.ToString('o') + '] bridge_launcher error=setup_failed' + [Environment]::NewLine
        [IO.File]::WriteAllText($logPath, $safeFailure, $utf8NoBom)
    }
    catch {
    }
    $exitCode = 1
}
finally {
    Remove-Item Env:PLAYER2_OPENAI_EMBEDDING_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:PLAYER2_FISH_AUDIO_KEY -ErrorAction SilentlyContinue
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    if ($fishKeyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($fishKeyPointer)
    }
}

exit $exitCode
