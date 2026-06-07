# start-bot.ps1
# Startet den kompletten Sentiment-Trader-Bot - inkl. Docker Desktop selbst,
# falls die Engine noch nicht laeuft. Wird von start-bot-hidden.vbs ohne
# sichtbares Fenster aufgerufen. Ein einziger Aufruf genuegt.
$ErrorActionPreference = "SilentlyContinue"

$proj      = "C:\Users\nikla\Downloads\sentiment-trader_4\sentiment-trader"
$log       = Join-Path $proj "scripts\startup.log"
$dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

function Log($m) { "$(Get-Date -Format s)  $m" | Out-File -FilePath $log -Append -Encoding utf8 }

Log "Startup-Task ausgeloest"

# 1) Laeuft die Docker-Engine schon? Wenn nicht: Docker Desktop starten.
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    if (Test-Path $dockerExe) {
        Log "Docker-Engine aus -> starte Docker Desktop"
        Start-Process -FilePath $dockerExe
    } else {
        Log "FEHLER: Docker Desktop.exe nicht gefunden ($dockerExe)"
    }
}

# 2) Bis zu 5 Minuten warten, bis die Engine Befehle annimmt.
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; Log "Docker bereit nach ca. $($i*5)s"; break }
    Start-Sleep -Seconds 5
}
if (-not $ready) { Log "Docker wurde nicht rechtzeitig bereit - Abbruch"; exit 1 }

# 3) Alle Container hochfahren.
Set-Location $proj
docker compose up -d *>> $log
Log "'docker compose up -d' ausgefuehrt (Exit $LASTEXITCODE)"

# 4) Dashboard im Standardbrowser oeffnen, sobald es antwortet.
#    Per Umgebungsvariable OPEN_DASHBOARD=0 abschaltbar.
if ($env:OPEN_DASHBOARD -ne "0") {
    $dashUrl = "http://localhost:8080"
    $dashReady = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            if ((Invoke-WebRequest -Uri $dashUrl -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) {
                $dashReady = $true; break
            }
        } catch { }
        Start-Sleep -Seconds 3
    }
    if ($dashReady) {
        Start-Process $dashUrl
        Log "Dashboard geoeffnet: $dashUrl"
    } else {
        Log "Dashboard nicht erreichbar - nicht geoeffnet"
    }
}
