# Caption-Apply: setzt die in der descriptError-Excel markierten Aktionen um.
#   - 'x' in 'merge description'  -> Caption per LLM (Qwen2-VL, GPU) zusammenfassen
#   - 'x' in 'cleanup description' -> Caption leeren (Qwen-Tagger kann neu taggen)
# Nutzt die Qwen-GPU-Umgebung (mcp-env) statt die foto-suche-venv.
#
# Aufruf:  .\descript-apply.ps1 <pfad-zur-excel> [-DryRun]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ExcelPath,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$mcpPython = "C:\MeineDateienDesk\MCP\mcp-env\Scripts\python.exe"
if (-not (Test-Path $mcpPython)) {
    Write-Error "Qwen-GPU-Umgebung nicht gefunden: $mcpPython"
    exit 1
}
if (-not (Test-Path $ExcelPath)) {
    Write-Error "Excel nicht gefunden: $ExcelPath"
    exit 1
}

Write-Host "=== Caption-Apply (Excel: $ExcelPath) ===" -ForegroundColor Cyan
Write-Host "Hinweis: digiKam sollte geschlossen sein." -ForegroundColor Yellow

$extra = @()
if ($DryRun) { $extra += "--dry-run" }

& $mcpPython -m app.caption_check $ExcelPath @extra
$code = $LASTEXITCODE

if ($code -eq 0) {
    Write-Host "Fertig. Danach: descript-check nicht noetig; 'python -m app.index --sync-only' + '--embed-only' laufen lassen." -ForegroundColor Green
} else {
    Write-Host "Apply fehlgeschlagen (Exit-Code $code)." -ForegroundColor Red
}
exit $code
