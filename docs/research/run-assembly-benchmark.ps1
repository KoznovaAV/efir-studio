#requires -Version 7.0
param(
    [string]$Output = '.efir-local/benchmark-96s',
    [string]$Ffmpeg = $env:EFIR_FFMPEG,
    [string]$Voice = 'Microsoft Irina Desktop',
    [ValidateRange(1, 10)][int]$Runs = 3
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
if (!$Ffmpeg) { $Ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source }
$Ffmpeg = (Get-Command $Ffmpeg -ErrorAction Stop).Source
$env:EFIR_FFMPEG = $Ffmpeg
if (Test-Path -LiteralPath $Output) { throw 'Choose a fresh output directory.' }
$workspace = (New-Item -ItemType Directory -Path $Output).FullName
$media = (New-Item -ItemType Directory -Path (Join-Path $workspace 'media')).FullName
$python = (Get-Command python -ErrorAction Stop).Source
$ffmpegName = [IO.Path]::GetFileNameWithoutExtension($Ffmpeg)

function Invoke-FFmpeg {
    & $Ffmpeg -nostdin -hide_banner -loglevel error @args
    if ($LASTEXITCODE -ne 0) { throw "FFmpeg exited with code $LASTEXITCODE." }
}

function Invoke-Efir {
    & $python -m efir @args
    if ($LASTEXITCODE -ne 0) { throw "EFIR exited with code $LASTEXITCODE." }
}

$lines = @(
    'Три часа тридцать три минуты. Ночной эфир начинается. В городе пока тихо.',
    'На контрольном экране движется сигнал. Пожалуйста, проверьте второй канал.',
    'В журнале появилась новая отметка. Время сверено. Пауза две секунды.',
    'На четвёртом канале изменился цвет. Сравните запись с первым фрагментом.',
    'Голос и фоновый сигнал должны быть слышны одновременно. Проверьте границы сцены.',
    'Контрольная запись завершена. Сохраните выпуск и проверьте файлы после переноса.'
)

Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speaker.SelectVoice($Voice)
    $speaker.Rate = 0
    for ($number = 1; $number -le 6; $number++) {
        $speaker.SetOutputToWaveFile((Join-Path $media "speech-$number.wav"))
        $speaker.Speak($lines[$number - 1])
    }
    $speaker.SetOutputToNull()
} finally {
    $speaker.Dispose()
}

# Two tone tracks are a synthetic background, with the same phase at each 16 s boundary.
Invoke-FFmpeg -f lavfi -i 'sine=frequency=110:sample_rate=48000:duration=96' -f lavfi -i 'sine=frequency=165:sample_rate=48000:duration=96' -filter_complex '[0:a][1:a]amix=inputs=2,volume=0.15' (Join-Path $media 'background.wav')
for ($number = 1; $number -le 7; $number++) {
    $pattern = 'testsrc2=size=540x960:rate=25:duration=16'
    Invoke-FFmpeg -f lavfi -i $pattern -vf "hue=h=$($number * 45)" '-c:v' libx264 '-threads:v' 4 -preset veryfast -crf 18 -an (Join-Path $media "clip-$number.mp4")
    if ($number -eq 7) { continue }
    $offset = ($number - 1) * 16
    Invoke-FFmpeg -i (Join-Path $media "speech-$number.wav") -ss $offset -i (Join-Path $media 'background.wav') -filter_complex '[0:a]adelay=1000,aresample=48000[voice];[1:a][voice]amix=inputs=2:duration=first:normalize=0' -t 16 -ac 2 (Join-Path $media "mix-$number.wav")
}

$fixtureFiles = Get-ChildItem -LiteralPath $media -File | Sort-Object Name
$fixtureFiles | ForEach-Object {
    [PSCustomObject]@{ file = $_.Name; bytes = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName).Hash }
} | Export-Csv -NoTypeInformation -Encoding UTF8 (Join-Path $workspace 'assets.csv')

function Measure-Export($Project, $Destination, $Trial, $Operation) {
    $knownPids = @(Get-Process -Name $ffmpegName -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $outFile = "$Destination.stdout.txt"
    $errFile = "$Destination.stderr.txt"
    $arguments = "-m efir export ""$Project"" ""$Destination"""
    $process = Start-Process -FilePath $python -ArgumentList $arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $peak = 0
    $cpu = @{}
    do {
        $observed = @(Get-Process -Id $process.Id -ErrorAction SilentlyContinue)
        $observed += @(Get-Process -Name $ffmpegName -ErrorAction SilentlyContinue | Where-Object { $_.Id -notin $knownPids })
        $workingSet = 0
        foreach ($item in $observed) {
            $workingSet += $item.WorkingSet64
            $cpu[$item.Id] = [Math]::Max([double]$cpu[$item.Id], [double]$item.CPU)
        }
        $peak = [Math]::Max($peak, $workingSet)
        Start-Sleep -Milliseconds 100
        $process.Refresh()
    } until ($process.HasExited)
    $process.WaitForExit()
    $timer.Stop()
    if ($process.ExitCode -ne 0) { throw (Get-Content -LiteralPath $errFile -Raw -Encoding UTF8) }
    $progress = Get-Content -LiteralPath $errFile -Encoding UTF8
    [PSCustomObject]@{
        trial = $Trial
        operation = $Operation
        seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 3)
        peak_sampled_working_set_mib = [Math]::Round($peak / 1MB, 1)
        sampled_cpu_seconds_lower_bound = [Math]::Round(($cpu.Values | Measure-Object -Sum).Sum, 2)
        encoded = @($progress -match ' · кодирование$').Count
        reused = @($progress -match ' · из кэша$').Count
        output_bytes = (Get-Item -LiteralPath $Destination).Length
    }
}

$results = foreach ($trial in 1..$Runs) {
    $project = Join-Path $workspace "Проект $trial"
    $null = Invoke-Efir init $project --title 'ЭФИР 03:33. Технический стенд' --aspect '9:16'
    $sceneIds = foreach ($number in 1..6) {
        Invoke-Efir add $project --title "Сцена $number" --visual (Join-Path $media "clip-$number.mp4") --voice (Join-Path $media "mix-$number.wav") --duration 16 --caption $lines[$number - 1]
    }
    $srt = Join-Path $workspace 'captions.srt'
    if ($trial -eq 1) { $null = Invoke-Efir srt $project $srt }
    Measure-Export $project (Join-Path $workspace "$trial-cold.mp4") $trial 'cold'
    Measure-Export $project (Join-Path $workspace "$trial-warm.mp4") $trial 'warm'
    $null = Invoke-Efir edit $project $sceneIds[1] --visual (Join-Path $media 'clip-7.mp4')
    Measure-Export $project (Join-Path $workspace "$trial-replace.mp4") $trial 'replace'
    $null = Invoke-Efir edit $project $sceneIds[1] --duration 15.52
    Measure-Export $project (Join-Path $workspace "$trial-duration.mp4") $trial 'duration'
    $null = Invoke-Efir move $project $sceneIds[1] 4
    $null = Invoke-Efir edit $project $sceneIds[1] --caption 'Контрольная правка русских субтитров.'
    Measure-Export $project (Join-Path $workspace "$trial-order.mp4") $trial 'order-caption'
    $copy = Join-Path $workspace "Перенос $trial"
    Copy-Item -LiteralPath $project -Destination $copy -Recurse
    Measure-Export $copy (Join-Path $workspace "$trial-copy.mp4") $trial 'copy'
}
$results | Export-Csv -NoTypeInformation -Encoding UTF8 (Join-Path $workspace 'results.csv')
$results | Format-Table -AutoSize
