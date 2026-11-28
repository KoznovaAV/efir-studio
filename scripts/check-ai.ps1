#requires -Version 7.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ai-common.ps1')
Set-EfirAiEnvironment
$status = (Invoke-RestMethod -Uri "$EfApi/api/tags" -TimeoutSec 4).models |
    Where-Object { $_.name -eq $EfModel -and $_.digest -eq $EfModelDigest }
if (!$status -or $status.details.quantization_level -ne 'Q4_K_M') { throw 'Установите модель через scripts/setup-ai.ps1.' }
if (!(Test-Path -LiteralPath $EfPiper) -or !(Test-Path -LiteralPath $EfVoice)) { throw 'Piper не подготовлен.' }
& $EfPiper --help | Out-Null
if ($LASTEXITCODE) { throw 'Piper не запускается.' }
$status
