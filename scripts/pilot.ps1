#requires -Version 7.0
param(
    [Parameter(Mandatory)][string]$Output,
    [string]$Ffmpeg = $env:EFIR_FFMPEG,
    [string]$Voice = 'Microsoft Irina Desktop'
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
if (!$Ffmpeg) { $Ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source }
$env:EFIR_FFMPEG = (Resolve-Path -LiteralPath $Ffmpeg).Path
if (Test-Path -LiteralPath $Output) { throw 'Укажите новый выходной каталог.' }
$workspace = (New-Item -ItemType Directory -Path $Output).FullName
$media = (New-Item -ItemType Directory -Path (Join-Path $workspace 'Источники')).FullName
$project = Join-Path $workspace 'ЭФИР 03.33'

function Invoke-Efir {
    & python -m efir @args
    if ($LASTEXITCODE) { throw "EFIR Studio: код завершения $LASTEXITCODE" }
}

function Invoke-FFmpeg {
    & $env:EFIR_FFMPEG -v error -nostdin @args
    if ($LASTEXITCODE) { throw "FFmpeg: код завершения $LASTEXITCODE" }
}

$scenes = @(
    @{ Title='Контрольный сигнал'; Card='ЭФИР 03:33'; Status='СЛУЖЕБНЫЙ КАНАЛ'; Kind='signal'; Color='#83edb1'
       Text='Вы смотрите служебный канал. Сейчас три часа тридцать три минуты. Если на ваших часах другое время, пожалуйста, не спорьте с часами.' },
    @{ Title='Несанкционированный чайник'; Card='ПОМЕХА'; Status='ЧАСТОТА 033.3'; Kind='kettle'; Color='#ffd695'
       Text='На частоте тридцать три и три обнаружен посторонний сигнал. По тембру это наш чайник. Чайнику запрещено передавать новости без согласования.' },
    @{ Title='Лишний коридор'; Card='КАМЕРА 04'; Status='ТЕХНИЧЕСКИЙ ЭТАЖ'; Kind='door'; Color='#82d3ee'
       Text='Камера четыре показывает запасной коридор. Он должен заканчиваться дверью. Но сегодня дверь заканчивается ещё одним запасным коридором.' },
    @{ Title='Слова из будущего'; Card='АУДИОАРХИВ'; Status='ВЧЕРА / ЗАВТРА'; Kind='wave'; Color='#ff9eab'
       Text='Мы получили запись завтрашнего эфира. На ней я прошу не читать эту фразу. Бухгалтерия просит зачитать её дважды, для внутреннего контроля.' },
    @{ Title='Протокол тишины'; Card='НЕ ОТВЕЧАЙТЕ'; Status='СИГНАЛ С ПРИЁМНИКА'; Kind='antenna'; Color='#c9abf3'
       Text='Если из выключенного приёмника слышно ваше имя, не отвечайте. Особенно если голос говорит правильно. У нас никто не умеет правильно произносить фамилии.' },
    @{ Title='Продолжение эфира'; Card='МЫ В ЭФИРЕ'; Status='ПОВТОР ЗАПИСИ'; Kind='loop'; Color='#83edb1'
       Text='На этом выпуск завершён. Следующий выпуск начнётся в три часа тридцать три минуты. Если он уже начался, не выключайте канал. Вы нам ещё понадобитесь.' }
)
$newLine = 'Чайник вышел на частоту тридцать три и три. Он объявил себя редактором. Мы попросили документ, и чайник вскипятил две копии.'

Add-Type -AssemblyName System.Drawing.Common
Add-Type -AssemblyName System.Speech

function Write-Card($Scene, [int]$Index, [string]$Path) {
    $bitmap = [Drawing.Bitmap]::new(540, 960)
    $surface = [Drawing.Graphics]::FromImage($bitmap)
    $surface.SmoothingMode = 'AntiAlias'
    $surface.TextRenderingHint = 'AntiAliasGridFit'
    $accent = [Drawing.ColorTranslator]::FromHtml($Scene.Color)
    $field = [Drawing.Drawing2D.LinearGradientBrush]::new(
        [Drawing.Rectangle]::new(0, 0, 540, 960), [Drawing.Color]::FromArgb(25, 34, 34),
        [Drawing.Color]::FromArgb(4, 8, 10), 72)
    $pen = [Drawing.Pen]::new($accent, 2)
    $dim = [Drawing.Pen]::new([Drawing.Color]::FromArgb(55, $accent), 1)
    $scan = [Drawing.Pen]::new([Drawing.Color]::FromArgb(35, 0, 0, 0), 1)
    $text = [Drawing.SolidBrush]::new($accent)
    $muted = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(178, 194, 182))
    $display = [Drawing.Font]::new('Consolas', 31, [Drawing.FontStyle]::Bold)
    $small = [Drawing.Font]::new('Consolas', 13)
    try {
        $surface.FillRectangle($field, 0, 0, 540, 960)
        $surface.DrawRectangle($dim, 31, 42, 477, 874)
        $surface.DrawString('РЕГИОНАЛЬНЫЙ ЦЕНТР ВЕЩАНИЯ', $small, $muted, 48, 69)
        $surface.DrawString(('03:33:0{0}   /   КАНАЛ {0:00}' -f $Index), $small, $text, 48, 101)
        $surface.DrawLine($dim, 49, 148, 491, 148)
        for ($x = 50; $x -le 490; $x += 22) { $surface.DrawLine($dim, $x, 206, $x, 592) }
        for ($y = 218; $y -le 592; $y += 22) { $surface.DrawLine($dim, 50, $y, 490, $y) }
        switch ($Scene.Kind) {
            'signal' {
                foreach ($size in 130, 214, 305) {
                    $surface.DrawEllipse($dim, 270 - $size / 2, 397 - $size / 2, $size, $size)
                }
                $surface.DrawLine($pen, 102, 397, 438, 397)
                $surface.DrawLine($pen, 270, 235, 270, 558)
                $surface.FillEllipse($text, 263, 390, 14, 14)
            }
            'kettle' {
                $surface.DrawArc($pen, 330, 348, 74, 80, 260, 211)
                $surface.DrawEllipse($pen, 177, 319, 160, 151)
                $surface.DrawEllipse($pen, 228, 305, 57, 22)
                $surface.DrawLine($pen, 177, 391, 116, 325)
                $surface.DrawLine($pen, 116, 325, 151, 325)
                $surface.DrawLine($pen, 151, 325, 191, 358)
                $surface.DrawString('НЕ ПОДКЛЮЧАТЬ К АНТЕННЕ', $small, $muted, 138, 514)
            }
            'door' {
                foreach ($inset in 0, 22, 42, 60, 75) {
                    $surface.DrawRectangle($pen, 120 + $inset, 243 + $inset, 300 - 2 * $inset, 315 - 2 * $inset)
                }
                $surface.DrawLine($dim, 50, 592, 270, 397)
                $surface.DrawLine($dim, 490, 592, 270, 397)
                $surface.FillEllipse($text, 318, 390, 7, 7)
            }
            'wave' {
                $points = for ($x = 52; $x -lt 488; $x += 3) {
                    $amplitude = [Math]::Sin($x * 0.016) * 115
                    [Drawing.PointF]::new($x, 397 + $amplitude * [Math]::Sin($x * 0.195))
                }
                $surface.DrawLines($pen, [Drawing.PointF[]]$points)
            }
            'antenna' {
                foreach ($radius in 84, 132, 181) {
                    $surface.DrawArc($pen, 270 - $radius, 400 - $radius, 2 * $radius, 2 * $radius, 210, 120)
                }
                $surface.FillEllipse($text, 258, 388, 24, 24)
                $surface.DrawLine($pen, 270, 400, 220, 543)
                $surface.DrawLine($pen, 270, 400, 320, 543)
            }
            'loop' {
                $surface.DrawArc($pen, 115, 242, 310, 310, 32, 291)
                $surface.DrawLines($pen, [Drawing.Point[]]@(
                    [Drawing.Point]::new(399, 259), [Drawing.Point]::new(394, 305), [Drawing.Point]::new(351, 298)
                ))
                $surface.DrawString('03:33', $display, $text, 207, 379)
            }
        }
        $surface.DrawString($Scene.Card, $display, $text, 45, 657)
        $surface.DrawString($Scene.Status, $small, $muted, 49, 718)
        $surface.DrawLine($dim, 49, 809, 491, 809)
        $surface.DrawString('АРХИВ / КОПИЯ С КОНТРОЛЬНОЙ ЛЕНТЫ', $small, $muted, 48, 843)
        for ($y = 1; $y -lt 960; $y += 3) { $surface.DrawLine($scan, 0, $y, 540, $y) }
        $bitmap.Save($Path, [Drawing.Imaging.ImageFormat]::Png)
    } finally {
        foreach ($item in $small, $display, $muted, $text, $scan, $dim, $pen, $field, $surface, $bitmap) {
            $item.Dispose()
        }
    }
}

$speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $speaker.SelectVoice($Voice)
    $speaker.Rate = -2
    $voiceTimer = [Diagnostics.Stopwatch]::StartNew()
    for ($index = 0; $index -lt 7; $index++) {
        $speaker.SetOutputToWaveFile((Join-Path $media "voice-$index.wav"))
        $speaker.Speak($(if ($index -eq 6) { $newLine } else { $scenes[$index].Text }))
        $speaker.SetOutputToNull()
    }
    $voiceTimer.Stop()
} finally {
    $speaker.Dispose()
}

$bed = Join-Path $media 'room-tone.wav'
Invoke-FFmpeg -f lavfi -i 'anoisesrc=color=brown:amplitude=0.12:seed=333:d=96:r=48000' `
    -f lavfi -i 'sine=f=49.9:d=96:r=48000' -filter_complex '[0:a]lowpass=f=850[n];[1:a]volume=0.4[h];[n][h]amix' $bed
$null = Invoke-Efir init $project --sqlite --title 'ЭФИР 03:33 · Контрольная лента'
$sceneIds = for ($index = 0; $index -lt $scenes.Count; $index++) {
    $scene = $scenes[$index]
    $card = Join-Path $media "card-$index.png"
    $clip = Join-Path $media "clip-$index.mp4"
    Write-Card $scene ($index + 1) $card
    Invoke-FFmpeg -loop 1 -i $card -vf 'noise=alls=9:allf=t+u,fade=t=in:d=0.10,fade=t=out:st=15.88:d=0.12' `
        -r 25 -t 16 -pix_fmt yuv420p '-c:v' libx264 -threads 4 -preset veryfast $clip
    Invoke-Efir add $project --title $scene.Title --visual $clip --duration 16 `
        --speech $scene.Text --caption $scene.Text --voice (Join-Path $media "voice-$index.wav")
}
$null = Invoke-Efir background $project $bed

function Invoke-PilotExport {
    $job = Invoke-Efir queue-export $project
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $null = Invoke-Efir work $project
    $timer.Stop()
    $record = Invoke-Efir jobs $project | ConvertFrom-Json -AsHashtable |
        Where-Object id -eq $job | Select-Object -First 1
    if ($record.state -ne 'succeeded') { throw "Экспорт прерван: $($record.error)" }
    $result = $record.result | ConvertFrom-Json -AsHashtable
    $result.seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 3)
    return $result
}

$first = Invoke-PilotExport
$null = Invoke-Efir edit $project $sceneIds[1] --speech $newLine --caption $newLine --voice (Join-Path $media 'voice-6.wav')
$second = Invoke-PilotExport
if ($first.encoded -ne 6 -or $second.encoded -ne 1 -or $second.reused -ne 5) {
    throw 'Нарушена выборочная инвалидация кэша.'
}
$captions = Join-Path $workspace 'ЭФИР 03.33.srt'
$null = Invoke-Efir srt $project $captions
$copy = Join-Path $workspace 'Перенос'
$null = Invoke-Efir portable $project $copy
$null = Invoke-Efir list $copy
$final = Join-Path $project $second.file
$copied = Join-Path $copy $second.file
Copy-Item -LiteralPath $final -Destination (Join-Path $workspace 'ЭФИР 03.33.mp4')
if ((Get-FileHash -LiteralPath $final).Hash -ne (Get-FileHash -LiteralPath $copied).Hash) {
    throw 'Контрольная сумма переносимого MP4 отличается.'
}
$frames = @(Invoke-FFmpeg -i $final -map 0:v:0 -f framecrc - | Select-String '^0,').Count
if ($frames -ne 2400) { throw "Ожидалось 2400 кадров, получено $frames." }
[ordered]@{
    speech_engine = $Voice
    speech_seconds_for_seven_takes = [Math]::Round($voiceTimer.Elapsed.TotalSeconds, 3)
    mp4_frames = $frames
    fps = 25
    file_bytes = (Get-Item -LiteralPath $final).Length
    output = $second.file
    first_export = $first
    second_export = $second
    portable_sha256 = (Get-FileHash -LiteralPath $copied).Hash
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $workspace 'results.json') -Encoding UTF8
