"""src/research/ingest.py
Befüllt den historischen Kursspeicher (prices_daily) aus yfinance.

Zwei Modi:
  * Backfill : holt mehrere Jahre OHLCV für das ganze Research-Universum.
  * Update   : holt nur die letzten Tage (täglicher Cron) und schreibt drauf.

Bewusst über yfinance (gratis, nutzt curl_cffi) im Sammel-Request mit
Retry/Backoff – dieselbe robuste Logik wie in der Signal-Engine.

Aufruf (im signal-Container):
    python -m src.research.ingest --backfill --period 10y
    python -m src.research.ingest --update                 # letzte ~10 Tage
    python -m src.research.ingest --coverage               # nur Übersicht
"""
import argparse
import logging
import math
import time

import pandas as pd
import yfinance as yf

from ..db import (get_connection, ensure_prices_daily_table,
                  upsert_prices_daily, prices_daily_coverage)
from ..universe import RESEARCH_UNIVERSE

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("ingest")

# yfinance verträgt große Batches; wir teilen trotzdem in Häppchen auf, damit
# ein einzelner Fehlschlag nicht den ganzen Lauf kostet.
CHUNK = 20


def _download(tickers: list[str], period: str = None,
              start=None, end=None) -> pd.DataFrame:
    """Sammel-Download mit Retry/Backoff. Gibt das (evtl. leere) DataFrame."""
    for attempt in range(3):
        try:
            data = yf.download(tickers, period=period, start=start, end=end,
                               interval="1d", group_by="ticker",
                               auto_adjust=True, progress=False, threads=False)
            if data is not None and not data.empty:
                return data
        except Exception as e:
            log.warning("Download Versuch %d fehlgeschlagen: %s", attempt + 1, e)
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    return pd.DataFrame()


def _rows_for(ticker: str, df: pd.DataFrame) -> list[tuple]:
    """Wandelt ein OHLCV-DataFrame eines Tickers in Upsert-Tupel."""
    rows = []
    for ts, r in df.iterrows():
        try:
            day = pd.Timestamp(ts).date()
            o, h, l, c, v = (r.get("Open"), r.get("High"), r.get("Low"),
                             r.get("Close"), r.get("Volume"))
            if c is None or (isinstance(c, float) and math.isnan(c)):
                continue
            def f(x):
                return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
            rows.append((ticker, day, f(o), f(h), f(l), f(c), f(v)))
        except Exception:
            continue
    return rows


def ingest(conn, tickers: list[str], period: str = None,
           start=None, end=None) -> int:
    """Lädt und speichert OHLCV für `tickers`. Gibt die Zahl geschriebener
    Zeilen zurück."""
    ensure_prices_daily_table(conn)
    total = 0
    for i in range(0, len(tickers), CHUNK):
        batch = tickers[i:i + CHUNK]
        log.info("Lade %d Werte (%d/%d) …", len(batch),
                 i + len(batch), len(tickers))
        data = _download(batch, period=period, start=start, end=end)
        if data.empty:
            log.warning("  Batch leer – übersprungen: %s", batch)
            continue
        rows = []
        if len(batch) == 1:
            rows = _rows_for(batch[0], data)
        else:
            for t in batch:
                try:
                    sub = data[t]
                    if not sub.empty:
                        rows += _rows_for(t, sub)
                except Exception:
                    pass
        upsert_prices_daily(conn, rows)
        total += len(rows)
        log.info("  %d Zeilen gespeichert.", len(rows))
        time.sleep(1.0)            # höflich gegenüber yfinance
    return total


def main():
    ap = argparse.ArgumentParser(description="Kurs-Ingestion in prices_daily")
    ap.add_argument("--backfill", action="store_true",
                    help="Mehrjähriger Erst-Backfill")
    ap.add_argument("--update", action="store_true",
                    help="Nur die letzten Tage (täglicher Lauf)")
    ap.add_argument("--coverage", action="store_true",
                    help="Nur Abdeckung anzeigen, nichts laden")
    ap.add_argument("--period", default="10y",
                    help="Backfill-Zeitraum (yfinance period), Default 10y")
    ap.add_argument("--tickers", nargs="*", default=None,
                    help="Optionale Ticker-Auswahl (sonst ganzes Universum)")
    args = ap.parse_args()

    conn = get_connection()
    ensure_prices_daily_table(conn)
    universe = args.tickers or RESEARCH_UNIVERSE

    if args.coverage:
        cov = prices_daily_coverage(conn)
        log.info("Abdeckung prices_daily: %d Ticker", len(cov))
        for tkr, n, lo, hi in cov:
            log.info("  %-6s %5d Tage  %s … %s", tkr, n, lo, hi)
        return

    if args.backfill:
        log.info("Backfill %d Werte über %s …", len(universe), args.period)
        n = ingest(conn, universe, period=args.period)
        log.info("Backfill fertig: %d Zeilen gesamt.", n)
    elif args.update:
        log.info("Update (letzte ~10 Tage) für %d Werte …", len(universe))
        n = ingest(conn, universe, period="10d")
        log.info("Update fertig: %d Zeilen.", n)
    else:
        log.info("Nichts zu tun. --backfill, --update oder --coverage angeben.")


if __name__ == "__main__":
    main()
