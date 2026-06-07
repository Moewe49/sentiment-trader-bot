# check-bot.ps1
# Schneller Gesundheits-Check: Laufen alle Container? Wurde heute gehandelt?
# Doppelklick (oder: Rechtsklick -> mit PowerShell ausfuehren). Fenster bleibt offen.
$proj = "C:\Users\nikla\Downloads\sentiment-trader_4\sentiment-trader"
Set-Location $proj

Write-Host "==================== SENTIMENT-TRADER CHECK ====================" -ForegroundColor Cyan
Write-Host ("Zeit (lokal): " + (Get-Date).ToString("yyyy-MM-dd HH:mm"))
$utc = (Get-Date).ToUniversalTime()
$ny  = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId($utc, "Eastern Standard Time")
Write-Host ("New York:     " + $ny.ToString("yyyy-MM-dd HH:mm") + "  (" + $ny.DayOfWeek + ")")
$open = ($ny.DayOfWeek -lt [DayOfWeek]::Saturday -and $ny.DayOfWeek -gt [DayOfWeek]::Sunday -and ($ny.Hour*60+$ny.Minute) -ge 570 -and ($ny.Hour*60+$ny.Minute) -le 960)
Write-Host ("US-Boerse offen: " + $(if ($open) { "JA" } else { "NEIN (kein Handel ausserhalb 9:30-16:00 Mo-Fr NY)" }))

Write-Host "`n--- 1) Container-Status ---" -ForegroundColor Cyan
docker compose ps --format "{{.Service}}`t{{.State}}`t{{.Status}}"

Write-Host "`n--- 2) Signal-Durchlaeufe & Trades (letzte 8h) ---" -ForegroundColor Cyan
docker compose logs --since 8h --no-color signal 2>&1 |
  Select-String 'Durchlauf startet|Zusammenfassung|-> BUY|: BUY|: SELL|SELL \(|kein Kurs|Exposure-Limit|Handel ausgesetzt|keine Position|Kein Trade|Kauf fehlgeschlagen|Verkauf fehlgeschlagen|Too Many Requests' |
  ForEach-Object { ($_ -replace '\s+$','') }

Write-Host "`n--- 3) Konto & offene Positionen (letzte 2h) ---" -ForegroundColor Cyan
docker compose logs --since 2h --no-color executor 2>&1 |
  Select-String 'Kontostand|NetLiquidation|Positionen|@ ' |
  Select-Object -Last 12 | ForEach-Object { ($_ -replace '\s+$','') }

Write-Host "`n--- 4) yfinance-Rate-Limit heute? ---" -ForegroundColor Cyan
$rl = docker compose logs --since 8h --no-color signal 2>&1 | Select-String 'Too Many Requests'
if ($rl) { Write-Host ("WARNUNG: " + $rl.Count + " Rate-Limit-Treffer") -ForegroundColor Yellow }
else { Write-Host "keine Rate-Limit-Fehler - gut." -ForegroundColor Green }

Write-Host "`nDashboard: http://localhost:8080" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Read-Host "`n[Enter] zum Schliessen"
