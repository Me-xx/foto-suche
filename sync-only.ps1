# Metadaten- und Tag-Sync aus der digiKam-DB (keine Embeddings).
# Erzeugt beim ersten Lauf config.yaml und index.db.
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$venvActivate = Join-Path $projectDir ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $venvActivate)) {
    Write-Error "venv nicht gefunden: $venvActivate -- zuerst 'python -m venv .venv' und 'pip install -r requirements.txt' ausfuehren."
    exit 1
}

& $venvActivate

Write-Host "=== Metadaten-Sync (digikam -> index.db) ===" -ForegroundColor Cyan
python -m app.index --sync-only
$code = $LASTEXITCODE

if ($code -eq 0) {
    Write-Host "Sync erfolgreich." -ForegroundColor Green
} else {
    Write-Host "Sync fehlgeschlagen (Exit-Code $code)." -ForegroundColor Red
}
exit $code
