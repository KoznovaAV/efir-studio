#requires -Version 7.0
[CmdletBinding()]
param([string]$Project)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ai-common.ps1')
Set-EfirAiEnvironment
$launched = !(Get-EfirOllamaProcess)
$projectArgs = @()
if ($Project) { $projectArgs += (Resolve-Path -LiteralPath $Project).Path }
Push-Location -LiteralPath $EfRoot
try {
    & (Join-Path $PSScriptRoot 'start-ai.ps1') | Out-Null
    & (Join-Path $PSScriptRoot 'check-ai.ps1') | Out-Null
    python -m efir desktop @projectArgs
    if ($LASTEXITCODE) { throw "EFIR Studio завершилась с кодом $LASTEXITCODE." }
} finally {
    if ($launched) { & (Join-Path $PSScriptRoot 'stop-ai.ps1') }
    Pop-Location
}
