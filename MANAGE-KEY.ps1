[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('openai', 'fish')]
    [string]$Key,

    [Parameter(Mandatory = $true)]
    [ValidateSet('Set', 'Remove', 'Status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$secretDir = Join-Path $env:LOCALAPPDATA 'Player2CLIBridge'
if ($Key -eq 'openai') {
    $secretFile = 'openai-key.dpapi'
    $keyName = 'OpenAI embedding key'
}
else {
    $secretFile = 'fish-audio-key.dpapi'
    $keyName = 'Fish Audio API key'
}
$secretPath = Join-Path $secretDir $secretFile
$temporaryPath = $null
$exitCode = 0

try {
    switch ($Action) {
        'Set' {
            Write-Host ($keyName + '를 입력하세요.')
            Write-Host '입력 내용은 화면에 표시되지 않으며 Windows DPAPI로 암호화됩니다.'
            $secureKey = Read-Host 'API key' -AsSecureString
            if ($secureKey.Length -eq 0) {
                throw '빈 키는 저장할 수 없습니다.'
            }
            $encrypted = ConvertFrom-SecureString -SecureString $secureKey
            [IO.Directory]::CreateDirectory($secretDir) | Out-Null
            $temporaryPath = $secretPath + '.new-' + $PID
            $utf8NoBom = New-Object Text.UTF8Encoding($false)
            [IO.File]::WriteAllText($temporaryPath, $encrypted, $utf8NoBom)
            Move-Item -LiteralPath $temporaryPath -Destination $secretPath -Force
            Write-Host ($keyName + ' 저장 완료.') -ForegroundColor Green
        }
        'Remove' {
            if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
                Remove-Item -LiteralPath $secretPath -Force
                Write-Host ('저장된 ' + $keyName + '를 삭제했습니다.') -ForegroundColor Green
            }
            else {
                Write-Host ('저장된 ' + $keyName + '가 없습니다.')
            }
        }
        'Status' {
            if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
                Write-Host ($keyName + ': configured (Windows DPAPI)')
                $exitCode = 0
                break
            }
            Write-Host ($keyName + ': not configured')
            $exitCode = 1
            break
        }
    }
}
catch {
    Write-Host ($keyName + ' 작업 실패. Windows DPAPI 및 저장소 권한을 확인하세요.') -ForegroundColor Red
    $exitCode = 1
}
finally {
    if ($temporaryPath -and (Test-Path -LiteralPath $temporaryPath -PathType Leaf)) {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    }
}
exit $exitCode
