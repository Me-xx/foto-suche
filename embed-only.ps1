# Embedding-Lauf ueber alle noch nicht embedded Captions (inkrementell, abbrechbar).
# Nutzt die Qwen-GPU-Umgebung (mcp-env, CUDA-torch), nicht die foto-suche-venv.
# Voraussetzung: sentence-transformers ist dort installiert (pip install --no-deps "sentence-transformers<6").
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$mcpPython = "C:\MeineDateienDesk\MCP\mcp-env\Scripts\python.exe"
if (-not (Test-Path $mcpPython)) {
    Write-Error "Qwen-GPU-Umgebung nicht gefunden: $mcpPython"
    exit 1
}

Write-Host "=== Embedding-Lauf (GPU, inkrementell, mit Strg+C abbrechbar) ===" -ForegroundColor Cyan
& $mcpPython -m app.index --embed-only
$code = $LASTEXITCODE

if ($code -eq 0) {
    Write-Host "Embedding-Lauf erfolgreich beendet." -ForegroundColor Green
} else {
    Write-Host "Embedding-Lauf abgebrochen oder fehlgeschlagen (Exit-Code $code). Fortschritt ist gespeichert, einfach erneut starten." -ForegroundColor Yellow
}
exit $code
