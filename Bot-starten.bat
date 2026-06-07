@echo off
title Sentiment Trader - START
cd /d "%~dp0"
echo ===============================================
echo    SENTIMENT-TRADER-BOT wird gestartet ...
echo ===============================================
echo.

REM 1) Docker Desktop starten, falls es nicht laeuft
tasklist /FI "IMAGENAME eq Docker Desktop.exe" 2>nul | find /I "Docker Desktop.exe" >nul
if errorlevel 1 (
  echo Docker Desktop wird gestartet ...
  start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
)

REM 2) Warten, bis die Docker-Engine bereit ist (max ~3 Min)
echo Warte auf Docker-Engine ...
set /a tries=0
:waitloop
docker version --format "{{.Server.Version}}" >nul 2>&1
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% geq 36 (
  echo.
  echo Docker-Engine nach 3 Minuten nicht bereit. Bitte Docker Desktop pruefen.
  pause
  exit /b 1
)
timeout /t 5 /nobreak >nul
goto waitloop

:ready
echo Docker bereit. Container werden gestartet ...
docker compose up -d

echo.
echo Warte auf Dashboard ...
timeout /t 10 /nobreak >nul
start "" http://localhost:8080

echo.
echo ===============================================
echo    FERTIG. Dashboard: http://localhost:8080
echo    (Das IB-Gateway braucht ~1 Min zum Login.)
echo ===============================================
timeout /t 6 >nul
