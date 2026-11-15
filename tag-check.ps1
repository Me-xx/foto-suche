# Tag-Check: findet gleichnamige digiKam-Tags (ignore case) und schreibt
# tagMerge_<Datum-Uhrzeit>.xlsx mit Merge-Vorschlag (Tag mit den meisten
# Zuordnungen bleibt).
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$venvActivate = Join-Path $projectDir ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $venvActivate)) {
    Write-Error "venv nicht gefunden: $venvActivate"
    exit 1
}
& $venvActivate

Write-Host "=== Tag-Analyse (Namens-Dubletten) ===" -ForegroundColor Cyan
python -m app.tag_check
exit $LASTEXITCODE
