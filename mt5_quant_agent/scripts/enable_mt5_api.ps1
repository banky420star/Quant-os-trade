# Ensure Experts Api=1 in common.ini (required for Python IPC dispatcher).
param(
    [string]$ConfigPath = "C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\config\common.ini"
)

if (-not (Test-Path $ConfigPath)) {
    Write-Error "common.ini not found: $ConfigPath"
    exit 1
}

$content = Get-Content $ConfigPath -Encoding Unicode
$updated = $content -replace '(?m)^Api=0\s*$', 'Api=1'
if ($content -join "`n" -eq $updated -join "`n") {
    Write-Host "Api already enabled or key not found in $ConfigPath"
} else {
    Set-Content -Path $ConfigPath -Value $updated -Encoding Unicode
    Write-Host "Set Experts Api=1 in $ConfigPath"
}
Select-String -Path $ConfigPath -Pattern '^(AllowDllImport|Enabled|Api)='