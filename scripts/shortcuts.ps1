param([string]$Name)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$app = Get-Content -LiteralPath (Join-Path $root 'app.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Name) {
    $Name = switch ($app.kind) {
        'douyin' { '抖音转录系统' }
        'bilibili' { 'B站转录系统' }
        'console' { '电商操作台' }
    }
}
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $desktop ($Name + '.lnk')))
$shortcut.TargetPath = Join-Path $root '.venv/Scripts/pythonw.exe'
$shortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'launch.py') + '"'
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = $shortcut.TargetPath + ',0'
$shortcut.Description = $app.name
if (-not (Test-Path -LiteralPath $shortcut.TargetPath)) { throw 'Install the project first.' }
$shortcut.Save()
