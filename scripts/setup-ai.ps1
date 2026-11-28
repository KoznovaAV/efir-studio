#requires -Version 7.0
[CmdletBinding()]
param([ValidateSet('All', 'Ollama', 'Piper')][string]$Component = 'All')

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'ai-common.ps1')
Set-EfirAiEnvironment

$python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
& $python -c 'import sys, struct; sys.exit(sys.version_info[:2] != (3,13) or struct.calcsize("P") != 8)'
if ($LASTEXITCODE) { throw 'Для профиля Windows нужен 64-битный CPython 3.13.' }
New-Item -ItemType Directory -Path $EfAi -Force | Out-Null

function Invoke-EfirPython {
    & $python @args
    if ($LASTEXITCODE) { throw "Python завершился с кодом $LASTEXITCODE." }
}

function Get-VoiceFile([string]$Name, [string]$Hash) {
    $destination = Join-Path (Split-Path -Parent $EfVoice) $Name
    if (Test-Path -LiteralPath $destination) {
        if ((Get-FileHash -LiteralPath $destination).Hash -ne $Hash) {
            throw "Неожиданный SHA-256 файла $Name. Проверьте существующий файл."
        }
        return
    }
    $revision = '613b6e5ad36ddca4e70e171957291483dc244d9f'
    $base = "https://huggingface.co/rhasspy/piper-voices/resolve/$revision/ru/ru_RU/denis/medium"
    $part = "$destination.download"
    Invoke-WebRequest -Uri "$base/$Name" -TimeoutSec 180 -OutFile $part
    if ((Get-FileHash -LiteralPath $part).Hash -ne $Hash) { throw "Неожиданный SHA-256 файла $Name." }
    Move-Item -LiteralPath $part -Destination $destination
}

if ($Component -in 'All','Piper') {
    $voice = Split-Path -Parent $EfVoice
    $wheels = Join-Path $EfAi 'downloads/wheels'
    New-Item -ItemType Directory -Path $voice,$wheels -Force | Out-Null
    Get-VoiceFile 'MODEL_CARD' '917110d5056810771fd3daea2644bbadf1341816b7631f2043b33be245903432'
    Get-VoiceFile 'ru_RU-denis-medium.onnx' '879bc365cb14d1380e3f5ed6044162ccf54c99f6161871569b7f830aec3ce841'
    Get-VoiceFile 'ru_RU-denis-medium.onnx.json' '5891b94580ae739a8cdc5d17591db6800333f85164d13599aca893883d99b9eb'
    $venv = Join-Path $EfAi 'piper-venv'
    $piperPython = Join-Path $venv 'Scripts/python.exe'
    if (!(Test-Path -LiteralPath $piperPython)) { Invoke-EfirPython -m venv $venv }
    $lock = Join-Path $PSScriptRoot 'piper-win-py313.lock'
    $pipArgs = @('-m','pip','--isolated','--disable-pip-version-check','--no-cache-dir')
    $lockArgs = @('--require-hashes','--only-binary=:all:','-r',$lock)
    & $piperPython @pipArgs install --no-index --find-links $wheels @lockArgs
    if ($LASTEXITCODE) {
        & $piperPython @pipArgs download --index-url https://pypi.org/simple --dest $wheels @lockArgs
        if ($LASTEXITCODE) { throw 'Загрузка зависимостей Piper завершилась ошибкой.' }
        & $piperPython @pipArgs install --no-index --find-links $wheels @lockArgs
        if ($LASTEXITCODE) { throw 'Установка Piper завершилась ошибкой.' }
    }
    & $EfPiper --help | Out-Null
    if ($LASTEXITCODE) { throw 'Piper не запускается.' }
}

if ($Component -in 'All','Ollama') {
    $fetch = Join-Path $PSScriptRoot 'fetch-ai.py'
    if (!(Test-Path -LiteralPath $EfOllama)) {
        $free = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($EfAi)).AvailableFreeSpace
        if ($free -lt 12GB) { throw 'На диске установки требуется не менее 12 GiB свободного места.' }
        Invoke-EfirPython $fetch ollama
        $zip = Join-Path $EfAi "downloads/ollama-v$EfOllamaVersion-windows-amd64.zip"
        $stage = Join-Path $EfAi ("ollama-stage-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $stage | Out-Null
        tar.exe -xf $zip -C $stage
        if ($LASTEXITCODE -or !(Test-Path -LiteralPath (Join-Path $stage 'ollama.exe'))) {
            throw 'Не удалось распаковать проверенный архив Ollama.'
        }
        Move-Item -LiteralPath $stage -Destination (Split-Path -Parent $EfOllama)
    }
    Invoke-EfirPython $fetch qwen
    $started = !(Get-EfirOllamaProcess)
    try {
        & (Join-Path $PSScriptRoot 'start-ai.ps1') | Out-Null
        $present = (Invoke-RestMethod "$EfApi/api/tags").models |
            Where-Object { $_.name -eq $EfModel -and $_.digest -eq $EfModelDigest }
        if (!$present) {
            $manifest = Invoke-WebRequest 'https://registry.ollama.ai/v2/library/qwen3/manifests/4b'
            $digest = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([byte[]]$manifest.Content))
            if ($digest -ne $EfModelDigest) { throw 'Публичный тег Qwen3 изменился. Проверьте закреплённый манифест.' }
            & $EfOllama pull $EfModel
            if ($LASTEXITCODE) { throw 'Ollama не смог установить манифест модели.' }
        }
        $verified = (Invoke-RestMethod "$EfApi/api/tags").models |
            Where-Object { $_.name -eq $EfModel -and $_.digest -eq $EfModelDigest -and
                $_.details.quantization_level -eq 'Q4_K_M' }
        if (!$verified) { throw 'Digest или квантизация модели отличается от зафиксированного.' }
    } finally {
        if ($started) { & (Join-Path $PSScriptRoot 'stop-ai.ps1') }
    }
}
