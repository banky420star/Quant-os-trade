# Creates the MT5 Quant OS desktop shortcut pointing at LAUNCH_ALL.bat
# Usage: powershell -ExecutionPolicy Bypass -File scripts\create_desktop_shortcut.ps1

$ErrorActionPreference = 'Stop'

$repoRoot  = (Resolve-Path "$PSScriptRoot\..").Path
$batPath   = Join-Path $repoRoot 'LAUNCH_ALL.bat'
$iconPath  = Join-Path $repoRoot 'mt5_quant_agent\dashboard\favicon.ico'

if (-not (Test-Path $batPath)) {
    throw "Launcher not found: $batPath"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$env:USERPROFILE\Desktop\MT5 Quant OS.lnk")
$shortcut.TargetPath       = $batPath
$shortcut.WorkingDirectory = $repoRoot
$shortcut.IconLocation     = "$batPath,0"
if (Test-Path $iconPath) {
    $shortcut.IconLocation = "$iconPath,0"
}
$shortcut.WindowStyle   = 1          # normal
$shortcut.Description   = 'MT5 Quant OS - Full Stack Launcher (claude + fixes)'
$shortcut.Save()

Write-Host "Created: $($shortcut.FullName)"
