@echo off
title Sentiment Trader - STOPP
cd /d "%~dp0"
echo Bot wird gestoppt ...
echo (Deine Positionen bei IBKR bleiben unveraendert - es wird NICHTS verkauft.)
echo.
docker compose stop
echo.
echo Gestoppt. Mit "Bot-starten.bat" geht es wieder los.
timeout /t 6 >nul
