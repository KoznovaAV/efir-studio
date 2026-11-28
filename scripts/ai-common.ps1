$EfRoot = Split-Path -Parent $PSScriptRoot
$EfAi = Join-Path $EfRoot '.efir-local/ai'
$EfOllamaVersion = '0.40.19'
$EfPiperVersion = '1.8.0'
$EfModel = 'qwen3:4b'
$EfModelDigest = '359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7'
$EfOllama = Join-Path $EfAi "ollama-$EfOllamaVersion/ollama.exe"
$EfPiper = Join-Path $EfAi 'piper-venv/Scripts/piper.exe'
$EfVoice = Join-Path $EfAi 'voices/denis/ru_RU-denis-medium.onnx'
$EfOwner = Join-Path $EfAi 'ollama-process.json'
$EfApi = 'http://127.0.0.1:11434'

function Set-EfirAiEnvironment {
    $env:OLLAMA_HOST = '127.0.0.1:11434'
    $env:OLLAMA_NO_CLOUD = '1'
    $env:OLLAMA_MODELS = Join-Path $EfAi 'models'
    $env:OLLAMA_NUM_PARALLEL = '1'
    $env:OLLAMA_MAX_LOADED_MODELS = '1'
    $env:OLLAMA_KEEP_ALIVE = '0'
    $env:EFIR_PIPER = $EfPiper
    $env:EFIR_PIPER_VERSION = $EfPiperVersion
    $env:EFIR_PIPER_MODEL = $EfVoice
    $env:EFIR_OLLAMA_MODEL = $EfModel
    $env:PYTHONUTF8 = '1'
    if (!$env:EFIR_FFMPEG -and (Test-Path (Join-Path $EfRoot '.efir-local/ffmpeg.exe'))) {
        $env:EFIR_FFMPEG = Join-Path $EfRoot '.efir-local/ffmpeg.exe'
    }
}

function Get-EfirOllamaProcess {
    if (!(Test-Path -LiteralPath $EfOwner)) { return }
    $owner = Get-Content -LiteralPath $EfOwner -Raw | ConvertFrom-Json
    $process = Get-Process -Id $owner.id -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq $EfOllama -and
        $process.StartTime.ToUniversalTime().Ticks -eq [long]$owner.startTicks) { $process }
}
