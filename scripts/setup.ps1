param([switch]$NoShortcuts)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$app = Get-Content -LiteralPath (Join-Path $root 'app.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$runtime = Join-Path $root '.runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$assetsPath = Join-Path $PSScriptRoot 'runtime-assets.json'
$assets = Get-Content -LiteralPath $assetsPath -Raw | ConvertFrom-Json

function Invoke-Checked([string]$program, [string[]]$arguments) {
    & $program @arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $program" }
}

function Install-Archive([string]$name, [string]$marker, [string]$folderPattern) {
    $destination = Join-Path $runtime $name
    if (Test-Path -LiteralPath (Join-Path $destination $marker)) { return $destination }
    $asset = $assets.$name
    $downloads = Join-Path $runtime 'downloads'
    New-Item -ItemType Directory -Force -Path $downloads | Out-Null
    $archive = if ($asset.bundled) { Join-Path (Join-Path $root 'vendor') $asset.archive } else { Join-Path $downloads $asset.archive }
    if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.sha256) {
        if ($asset.bundled) { throw "Bundled runtime missing or corrupt: $name. Download the complete project again." }
        Write-Host "Downloading $name..."
        if ($name -eq 'uv') { Invoke-WebRequest -Uri $asset.url -OutFile $archive -UseBasicParsing }
        else { Invoke-Checked $python @((Join-Path $PSScriptRoot 'fetch_asset.py'),$assetsPath,$name,$archive) }
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.sha256) { throw "Archive checksum mismatch: $name" }
    $unpack = Join-Path $runtime ($name + '-unpack')
    New-Item -ItemType Directory -Force -Path $unpack | Out-Null
    if ($python -and (Test-Path -LiteralPath $python)) { Invoke-Checked $python @('-m','zipfile','-e',$archive,$unpack) } else { Expand-Archive -LiteralPath $archive -DestinationPath $unpack -Force }
    if ($folderPattern) {
        $folders = @(Get-ChildItem -LiteralPath $unpack -Directory | Where-Object Name -Like $folderPattern)
        if ($folders.Count -ne 1) { throw "Unexpected archive layout: $name" }
        $from = $folders[0].FullName
    } else { $from = $unpack }
    $boundary = [IO.Path]::GetFullPath($runtime) + [IO.Path]::DirectorySeparatorChar
    foreach ($target in @($from,$destination)) {
        if (-not [IO.Path]::GetFullPath($target).StartsWith($boundary,[StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime path escaped project' }
    }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Get-ChildItem -LiteralPath $from -Force | Copy-Item -Destination $destination -Recurse -Force
    if (-not (Test-Path -LiteralPath (Join-Path $destination $marker))) { throw "Missing runtime: $name" }
    return $destination
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
    $uvDir = Install-Archive 'uv' 'uv.exe' ''
    $uv = Join-Path $uvDir 'uv.exe'
    $pythonDir = Install-Archive 'python' 'python.exe' 'python'
    $basePython = Join-Path $pythonDir 'python.exe'
    $env:PATH = $pythonDir + ';' + $env:PATH
    $env:UV_PYTHON_DOWNLOADS = 'never'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $runtime 'browsers'
    $venv = Join-Path $root '.venv'
    $python = Join-Path $venv 'Scripts/python.exe'
    $venvHome = ''
    $venvConfig = Join-Path $venv 'pyvenv.cfg'
    if (Test-Path -LiteralPath $venvConfig) {
        $homeLine = Get-Content -LiteralPath $venvConfig | Where-Object { $_ -like 'home = *' } | Select-Object -First 1
        if ($homeLine) { $venvHome = $homeLine.Substring(7).Trim() }
    }
    if (-not (Test-Path -LiteralPath $python) -or -not $venvHome -or [IO.Path]::GetFullPath($venvHome) -ne [IO.Path]::GetFullPath($pythonDir)) {
        Invoke-Checked $uv @('venv','--allow-existing','--python',$basePython,$venv)
    }
    $syncArgs = @('pip','sync','--python',$python,(Join-Path $root 'requirements.lock'))
    if ($app.kind -ne 'console') { $syncArgs += @('--extra-index-url','https://download.pytorch.org/whl/cpu','--index-strategy','unsafe-best-match') }
    Invoke-Checked $uv $syncArgs
    if ($app.kind -eq 'console') {
        Invoke-Checked $uv @('pip','install','--python',$python,'--no-deps',(Join-Path $root 'dependencies/price_compare'),(Join-Path $root 'dependencies/xianyu'))
        $null = Install-Archive 'postgres' 'bin/pg_ctl.exe' 'pgsql'
        $nodeDir = Install-Archive 'node' 'node.exe' 'node-*'
        $env:PATH = $nodeDir + ';' + $env:PATH
        Push-Location (Join-Path $root 'frontend')
        try {
            Invoke-Checked (Join-Path $nodeDir 'npm.cmd') @('ci','--no-audit','--no-fund')
            Invoke-Checked (Join-Path $nodeDir 'npm.cmd') @('run','build')
        } finally { Pop-Location }
    } else {
        $null = Install-Archive 'ffmpeg' 'bin/ffmpeg.exe' 'ffmpeg-*'
        foreach ($directory in @('配置','输出','video and audio')) { New-Item -ItemType Directory -Force -Path (Join-Path $root $directory) | Out-Null }
    }
    foreach ($directory in @('credentials','config','data','logs')) { New-Item -ItemType Directory -Force -Path (Join-Path $root ('.local/' + $directory)) | Out-Null }
    Invoke-Checked $python @('-m','playwright','install','chromium')
    if ($app.kind -eq 'console') { Invoke-Checked $python @((Join-Path $PSScriptRoot 'console_bootstrap.py'),'--initialize') }
    if (-not $NoShortcuts) { & (Join-Path $PSScriptRoot 'shortcuts.ps1') }
    Write-Host 'Installed. Use the desktop shortcut; complete platform login/Cookie in the UI.' -ForegroundColor Green
} catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
