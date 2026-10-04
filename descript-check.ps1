# Caption-Check: durchsucht die digiKam-DB nach Datenmuell/redundanten
# Captions und schreibt descriptError_<Datum-Uhrzeit>.xlsx ins Projektverzeichnis.
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$venvActivate = Join-Path $projectDir ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $venvActivate)) {
    Write-Error "venv nicht gefunden: $venvActivate"
    exit 1
}
& $venvActivate

Write-Host "=== Caption-Analyse (Datenmuell + Redundanz) ===" -ForegroundColor Cyan
python -m app.caption_check
exit $LASTEXITCODE
