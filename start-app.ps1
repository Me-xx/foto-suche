# Startet die Foto-Suche-App (Streamlit) und oeffnet den Browser.
# Falls auf dem Port bereits eine Instanz laeuft, wird nur der Browser geoeffnet.
param(
    [int]$Port = 8501
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "venv nicht gefunden: $venvPython -- zuerst 'pip install -r requirements.txt' ausfuehren."
    exit 1
}

# Laeuft bereits eine Instanz auf dem Port?
$running = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect("127.0.0.1", $Port)
    $client.Close()
    $running = $true
} catch {
    $running = $false
}

if ($running) {
    Write-Host "App laeuft bereits auf Port $Port." -ForegroundColor Yellow
} else {
    Write-Host "Starte Foto-Suche auf http://localhost:$Port …" -ForegroundColor Cyan
    # Eigenes Konsolenfenster: schliessen des Fensters beendet die App.
    Start-Process -FilePath $venvPython -ArgumentList @(
        "-m", "streamlit", "run", "app/ui.py",
        "--server.port=$Port",
        "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false"
    ) -WorkingDirectory $projectDir

    # Warten, bis der Server antwortet (max. 30 s)
    $up = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $client.Connect("127.0.0.1", $Port)
            $client.Close()
            $up = $true
            break
        } catch { }
    }
    if (-not $up) {
        Write-Error "App ist nach 30 s nicht erreichbar -- Fenster mit der Fehlermeldung pruefen."
        exit 1
    }
    Write-Host "App ist erreichbar." -ForegroundColor Green
}

Start-Process "http://localhost:$Port"
Write-Host "Browser geoeffnet. App beenden: Konsolenfenster schliessen." -ForegroundColor Green
