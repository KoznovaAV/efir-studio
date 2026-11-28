#requires -Version 7.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ai-common.ps1')
Set-EfirAiEnvironment

if (!(Test-Path -LiteralPath $EfOllama)) { throw 'Сначала запустите scripts/setup-ai.ps1.' }
if ($process = Get-EfirOllamaProcess) {
    Write-Output $process.Id
    return
}
if (Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Порт 11434 уже занят процессом вне EFIR. Завершите его через приложение и повторите.'
}
$options = @{
    FilePath = $EfOllama
    ArgumentList = 'serve'
    WorkingDirectory = $EfAi
    RedirectStandardOutput = Join-Path $EfAi 'ollama.stdout.log'
    RedirectStandardError = Join-Path $EfAi 'ollama.stderr.log'
    WindowStyle = 'Hidden'
    PassThru = $true
}
$process = Start-Process @options
@{id=$process.Id; startTicks=$process.StartTime.ToUniversalTime().Ticks} |
    ConvertTo-Json | Set-Content -LiteralPath $EfOwner -Encoding UTF8NoBOM
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($process.HasExited) { throw 'Ollama завершился. Проверьте .efir-local/ai/ollama.stderr.log.' }
    try {
        $version = Invoke-RestMethod -Uri "$EfApi/api/version" -TimeoutSec 1
    } catch {
        Start-Sleep -Milliseconds 300
        continue
    }
    $listeners = @(Get-NetTCPConnection -OwningProcess $process.Id -State Listen -ErrorAction SilentlyContinue)
    if ($version.version -ne $EfOllamaVersion -or
        -not ($listeners | Where-Object { $_.LocalPort -eq 11434 -and $_.LocalAddress -eq '127.0.0.1' }) -or
        ($listeners | Where-Object LocalAddress -NotIn @('127.0.0.1', '::1'))) {
        Stop-Process -Id $process.Id
        throw 'Ollama запустился с неожиданной версией или сетевой конфигурацией.'
    }
    Write-Output $process.Id
    return
}
Stop-Process -Id $process.Id
throw 'Ollama не ответил на проверку готовности.'
