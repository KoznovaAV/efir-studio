#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('tts', 'llm', 'profile')][string]$Kind,
    [Parameter(Mandatory)][string]$Output,
    [ValidateRange(2048, 8192)][int]$Context = 8192,
    [ValidateRange(0, 300)][int]$KeepAlive = 0,
    [switch]$Think
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ai-common.ps1')
Set-EfirAiEnvironment
if (!(Test-Path -LiteralPath $EfPiper) -or !(Test-Path -LiteralPath $EfVoice)) {
    Write-Output 'Пропущено: настройте Piper Denis через scripts/setup-ai.ps1.'
    return
}
$manifest = Join-Path $env:OLLAMA_MODELS 'manifests/registry.ollama.ai/library/qwen3/4b'
if ($Kind -ne 'tts' -and (!(Test-Path -LiteralPath $EfOllama) -or !(Test-Path -LiteralPath $manifest))) {
    Write-Output 'Пропущено: локальный Ollama и закреплённая модель Qwen3 не готовы.'
    return
}
$target = [IO.Path]::GetFullPath($Output)
$qa = Join-Path $EfRoot '.efir-local/qa-venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $qa)) { $qa = (Get-Command python -CommandType Application | Select-Object -First 1).Source }
& $qa -c 'import psutil'
if ($LASTEXITCODE) { throw 'Для замера ресурсов требуется psutil из requirements-qa.txt.' }
$started = $Kind -ne 'tts' -and !(Get-EfirOllamaProcess)
Push-Location -LiteralPath $EfRoot
try {
    $options = @('--context', $Context, '--keep-alive', $KeepAlive)
    if ($Think) { $options += '--think' }
    if ($Kind -ne 'tts') {
        $serverId = & (Join-Path $PSScriptRoot 'start-ai.ps1')
        & (Join-Path $PSScriptRoot 'check-ai.ps1') | Out-Null
        $options += @('--server-pid', $serverId)
    }
    & $qa -m scripts.benchmark_ai $Kind $target @options
    if ($LASTEXITCODE) { throw 'Приёмочный прогон завершился ошибкой. Частичные результаты остаются в выходном каталоге.' }
} finally {
    if ($started) { & (Join-Path $PSScriptRoot 'stop-ai.ps1') }
    Pop-Location
}
