[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('quality', 'unit', 'integration', 'e2e', 'coverage', 'package')]
    [string] $Stage,
    [string] $ReportsDirectory = 'reports'
)

$ErrorActionPreference = 'Stop'
$efirRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$reportPath = Join-Path $efirRoot $ReportsDirectory
$dockerLimits = @('--cpus', '4', '--memory', '3g', '--pids-limit', '512',
    '--security-opt', 'no-new-privileges:true')

Push-Location -LiteralPath $efirRoot
try {
    $engineOS = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $engineOS -ne 'linux') {
        throw 'Start Docker Desktop in Linux containers mode before running EFIR CI.'
    }
    New-Item -ItemType Directory -Path $reportPath -Force | Out-Null

    if ($Stage -eq 'package') {
        $tag = 'efir-studio:ci-' + [guid]::NewGuid().ToString('N')
        & docker build --pull --tag $tag .
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $encodingBefore = $OutputEncoding
        try {
            $metadataPath = Join-Path $reportPath 'docker'
            New-Item -ItemType Directory -Path $metadataPath -Force | Out-Null
            $metadata = & docker image inspect $tag
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            [IO.File]::WriteAllLines((Join-Path $metadataPath 'image.json'),
                [string[]] $metadata, [Text.UTF8Encoding]::new($false))
            $OutputEncoding = [Text.UTF8Encoding]::new($false)
            Get-Content -Raw -Encoding UTF8 -LiteralPath 'tests/test_cli_e2e.py' |
                & docker run --rm -i @dockerLimits --network none --entrypoint python $tag - -v
            exit $LASTEXITCODE
        }
        finally {
            $OutputEncoding = $encodingBefore
            & docker image rm $tag | Out-Null
        }
    }

    $pythonImage = $env:EFIR_CI_PYTHON_IMAGE
    if ([string]::IsNullOrWhiteSpace($pythonImage)) {
        throw 'EFIR_CI_PYTHON_IMAGE must be set to the pinned image from .gitlab-ci.yml.'
    }

    # Export tracked sources only. The worker container does not see .git, local
    # files, CI credentials or the Docker socket.
    $archiveDir = Join-Path ([IO.Path]::GetTempPath()) ('efir-ci-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $archiveDir | Out-Null
    try {
        $archive = Join-Path $archiveDir 'source.tar'
        & git archive --format=tar "--output=$archive" HEAD
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $bootstrap = @'
work=$(mktemp -d)
tar -xf /source.tar -C "$work"
cd "$work"
status=0
sh scripts/ci-python-stage.sh "$1" || status=$?
if [ -d reports ]; then cp -R reports/. /ci-artifacts/; fi
exit "$status"
'@
        & docker run --rm @dockerLimits --env PYTHONUTF8=1 `
            --mount "type=bind,source=$archive,target=/source.tar,readonly" `
            --mount "type=bind,source=$reportPath,target=/ci-artifacts" `
            $pythonImage sh -ec $bootstrap efir-ci $Stage
        exit $LASTEXITCODE
    }
    finally {
        $archiveReal = (Resolve-Path -LiteralPath $archiveDir).Path
        $tempReal = (Resolve-Path -LiteralPath ([IO.Path]::GetTempPath())).Path
        if ((Split-Path -Parent $archiveReal) -eq $tempReal -and
            (Split-Path -Leaf $archiveReal) -match '^efir-ci-[a-f0-9]{32}$') {
            Remove-Item -LiteralPath $archiveReal -Recurse -Force
        }
    }
}
finally {
    Pop-Location
}
