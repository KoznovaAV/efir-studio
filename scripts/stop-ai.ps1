#requires -Version 7.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ai-common.ps1')
Set-EfirAiEnvironment
if ($process = Get-EfirOllamaProcess) {
    try {
        $loaded = (Invoke-RestMethod -Uri "$EfApi/api/ps" -TimeoutSec 3).models
        foreach ($item in $loaded) { & $EfOllama stop $item.name }
    } catch {
        Write-Verbose 'Сервер Ollama не ответил на запрос выгрузки модели.'
    }
    $process.Refresh()
    if (!$process.HasExited) {
        Stop-Process -InputObject $process
        $null = $process.WaitForExit(5000)
    }
}
