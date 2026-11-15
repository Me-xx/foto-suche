# Tag-Apply: fuehrt die in der tagMerge-Excel markierten Zusammenfuehrungen
# durch. Zeilen ohne 'neue Tag ID' werden uebersprungen.
#
# Aufruf:  .\tag-apply.ps1 <pfad-zur-excel> [-DryRun]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ExcelPath,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$venvActivate = Join-Path $projectDir ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $venvActivate)) {
    Write-Error "venv nicht gefunden: $venvActivate"
    exit 1
}
if (-not (Test-Path $ExcelPath)) {
    Write-Error "Excel nicht gefunden: $ExcelPath"
    exit 1
}

Write-Host "=== Tag-Apply (Excel: $ExcelPath) ===" -ForegroundColor Cyan
Write-Host "Hinweis: digiKam sollte geschlossen sein." -ForegroundColor Yellow

$extra = @()
if ($DryRun) { $extra += "--dry-run" }

& "$projectDir\.venv\Scripts\python.exe" -m app.tag_check $ExcelPath @extra
$code = $LASTEXITCODE

if ($code -eq 0) {
    Write-Host "Fertig. Danach 'python -m app.index --sync-only' laufen lassen." -ForegroundColor Green
} else {
    Write-Host "Apply fehlgeschlagen (Exit-Code $code)." -ForegroundColor Red
}
exit $code
