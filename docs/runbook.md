# Betriebs-Runbook

Kurzanleitung zum sicheren Bedienen des Bots. Alle Befehle aus dem Projekt-
Ordner ausführen. `signal`-Container hat alle Tools (IB + DB + Research).

## 1. Stack

```powershell
docker compose up -d            # alles starten
docker compose up -d --build signal dashboard   # nach Code-Änderung neu bauen
docker compose restart          # alles neu starten (inkl. IB-Gateway-Re-Login ~1 Min)
docker compose logs --since 2m signal           # Engine-Logs
```
Dashboard: http://localhost:8080

## 2. Zwei Betriebsmodi (Schalter im ⚙-Dashboard oder DB-Setting)

| Setting | Wirkung |
|---|---|
| `SYSTEMATIC_LIVE = Aus` | **Altlogik**: Sentiment+Technik je Wert, Buy&Hold + Trim + Einzel-Stops. |
| `SYSTEMATIC_LIVE = An`  | **Systematischer Kern**: Momentum-Blend Top 30 % + SMA200-Regime, monatliches Rebalancing, KEINE Einzel-Stops. Altlogik aus. |
| `SYSTEMATIC_DEPLOY = An` | Einmal-Schalter: beim **nächsten US-Open** einmal aufs Ziel umschichten, setzt sich dann selbst zurück. |

Setting per CLI setzen:
```powershell
docker compose run --rm signal python -c "from src.db import *; c=get_connection(); ensure_settings_table(c); set_settings(c, {'SYSTEMATIC_LIVE':'yes'})"
```

## 3. Systematischen Kern live schalten (Ablauf)

1. **Markt muss offen sein** (US 15:30–22:00 MESZ). Sonst werden Market-Orders nur eingereiht → der eingebaute Schutz verhindert die Ausführung.
2. `SYSTEMATIC_LIVE = An` setzen (Altlogik aus).
3. Entweder `SYSTEMATIC_DEPLOY = An` (Engine deployt automatisch am nächsten Open)
   **oder** manuell bei offener Börse:
   ```powershell
   docker compose run --rm signal python -m src.research.live --dry         # Vorschau
   docker compose run --rm signal python -m src.research.live --rebalance    # ausführen
   ```
4. Danach rebalanciert die Engine automatisch monatlich (Shadow-Takt).

## 4. Live-Rebalancer (src/research/live.py)

```powershell
... live --dry          # nur geplante Orders zeigen (führt nie aus)
... live --rebalance     # Konto aufs Ziel umschichten (nur bei offener Börse)
... live --liquidate     # alles glattstellen → Cash (nur bei offener Börse)
... live --rebalance --force   # Markt-offen-Schutz umgehen (NUR bewusst!)
```
Sicherheiten: Markt-offen-Pflicht, Sync vor jedem Lesen (`reqPositions`),
Verkäufe vor Käufen, Cash-Deckel (kein Hebel), Short-Schließung.

## 5. Recovery (wenn etwas klemmt)

```powershell
# 1) Wahren Kontostand prüfen (mit Sync!)
docker compose run --rm signal python -c "from src.executor import IBKRExecutor; e=IBKRExecutor(); e.connect(client_id=13); e.ib.reqPositions(); e.ib.sleep(5); print([(p.contract.symbol,int(p.position)) for p in e.ib.positions() if p.position])"
# 2) Alle Orders stornieren (sicherer Zustand)
docker compose run --rm signal python -c "from src.executor import IBKRExecutor; e=IBKRExecutor(); e.connect(client_id=8); e.ib.reqGlobalCancel(); e.ib.sleep(3)"
# 3) Bei offener Börse sauber neu aufsetzen:
docker compose run --rm signal python -m src.research.live --liquidate
docker compose run --rm signal python -m src.research.live --rebalance
```
**Nie** den Stack neu starten, während ein `--rebalance`/`--liquidate` läuft (killt den Lauf → Teilzustand).

## 6. Research-Werkzeuge

```powershell
... python -m src.research.ingest   --backfill --period 10y   # Kursspeicher füllen
... python -m src.research.ingest   --update                  # täglich (läuft autom.)
... python -m src.research.features --build                   # Features bauen
... python -m src.research.eval     --score mom_blend --regime # Backtest + Regime
... python -m src.research.ml        --hold 21 --top-q 0.3      # ML vs Momentum (OOS)
... python -m src.research.shadow    --status                  # Shadow-Track ansehen
... python -m src.catalysts          --scan                    # LLM-Katalysator-Scan
```

## 7. Wichtige Kennzahlen (validiert, Stand Phase 1)

- Systematischer Kern: **Sharpe ~1,51, MaxDD ~−21 %** (Momentum-Blend + Regime).
- ML (GBM) schlägt simples Momentum derzeit **nicht** → braucht orthogonale Daten
  (Katalysatoren/Fundamentals-Historie), nicht mehr Modell.
- Absolute Backtest-Renditen sind durch **Survivorship Bias** zu optimistisch;
  belastbar ist der relative Vorsprung + die Risikoverbesserung.

## 8. Client-IDs (IB) – nicht doppelt belegen

| ID | Nutzung |
|----|---------|
| 1  | Executor |
| 2  | Signal-Engine |
| 7  | Dashboard-Stream |
| 8  | Live-Rebalancer / Wartung |
| 9  | Manueller Verkauf (Dashboard) |
| 11–13 | Ad-hoc-Checks |
