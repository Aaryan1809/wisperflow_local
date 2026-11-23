# LocalFlow setup
# Downloads the speech engine and model into %LOCALAPPDATA%\LocalFlow (NOT Google Drive)
# and creates a small Python environment there. Safe to re-run: finished steps are skipped.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Code   = $PSScriptRoot
$Data   = Join-Path $env:LOCALAPPDATA 'LocalFlow'
$Bin    = Join-Path $Data 'bin'
$Models = Join-Path $Data 'models'
$Venv   = Join-Path $Data 'venv'

# whisper.cpp v1.9.5, CPU build with OpenBLAS (fastest CPU option, no NVIDIA libraries needed)
$EngineUrl = 'https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-blas-bin-x64.zip'

$Files = @(
    @{ Name = 'ggml-large-v3-turbo-q5_0.bin'
       Url  = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin'
       Sha  = '394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2' },
    @{ Name = 'ggml-silero-v5.1.2.bin'
       Url  = 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin'
       Sha  = '29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf' }
)

New-Item -ItemType Directory -Force $Bin, $Models | Out-Null

Write-Host '[1/3] Speech engine (whisper.cpp)'
if (Test-Path (Join-Path $Bin 'whisper-server.exe')) {
    Write-Host '      already installed'
} else {
    $tmp = Join-Path $env:TEMP ('localflow-' + [guid]::NewGuid())
    New-Item -ItemType Directory $tmp | Out-Null
    try {
        curl.exe -L --fail --progress-bar -o "$tmp\engine.zip" $EngineUrl
        if ($LASTEXITCODE -ne 0) { throw 'Engine download failed' }
        Expand-Archive "$tmp\engine.zip" "$tmp\x"
        # keep only what the server needs (~62 MB instead of ~90 MB)
        $keep = @('whisper-server.exe', 'whisper.dll', 'ggml.dll', 'ggml-base.dll', 'ggml-blas.dll', 'libopenblas.dll')
        Get-ChildItem "$tmp\x" -Recurse -File |
            Where-Object { ($keep -contains $_.Name) -or ($_.Name -like 'ggml-cpu-*.dll') } |
            Copy-Item -Destination $Bin
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
    Write-Host '      installed'
}

Write-Host '[2/3] Models'
foreach ($f in $Files) {
    $dest = Join-Path $Models $f.Name
    if (-not (Test-Path $dest)) {
        Write-Host "      downloading $($f.Name)"
        curl.exe -L --fail --progress-bar -C - -o "$dest.part" $f.Url
        if ($LASTEXITCODE -ne 0) { throw "Download failed: $($f.Name)" }
        Move-Item -Force "$dest.part" $dest
    }
    $hash = (Get-FileHash -Algorithm SHA256 $dest).Hash.ToLower()
    if ($hash -ne $f.Sha) {
        Remove-Item $dest
        throw "Checksum mismatch for $($f.Name) - deleted it, please re-run setup."
    }
    Write-Host "      $($f.Name) OK"
}

Write-Host '[3/3] Python environment'
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment (is Python installed?)' }
}
& (Join-Path $Venv 'Scripts\python.exe') -m pip install --disable-pip-version-check --quiet -r (Join-Path $Code 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing Python packages failed' }

$mb = [math]::Round(((Get-ChildItem $Data -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB)
Write-Host ''
Write-Host "Done. LocalFlow uses $mb MB in $Data"
Write-Host 'Start it with LocalFlow.bat'
