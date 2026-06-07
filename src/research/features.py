"""src/research/features.py  (D2)
Berechnet point-in-time Merkmale je (ticker, day) aus prices_daily und
schreibt sie nach features_daily.

WICHTIG (kein Look-ahead): Jedes Merkmal an Tag t nutzt ausschließlich Daten
BIS einschließlich t. Forward-Returns (Labels) kommen getrennt (labels.py).

Merkmale:
  r_21/63/126/252   – Trailing-Returns (Momentum über 1/3/6/12 Monate)
  mom_12_1          – 12-1-Momentum (klassisch): Return t-252→t-21
  vol_20/60         – realisierte Tages-Vola (Std der Tagesrenditen)
  rsi_14            – RSI(14)
  sma20/50_dist     – Abstand des Kurses zum SMA (Anteil)
  dist_52w_high     – Abstand zum 52-Wochen-Hoch (Anteil, ≤ 0)
  vol_ratio         – Volumen / 20-Tage-Durchschnittsvolumen

Aufruf (im signal-Container):
    python -m src.research.features --build
    python -m src.research.features --build --tickers NVDA AAPL
"""
import argparse
import logging

import numpy as np
import pandas as pd

from ..db import (get_connection, fetch_prices_history,
                  ensure_features_table, upsert_features, FEATURE_COLUMNS)
from ..indicators import rsi
from ..universe import RESEARCH_UNIVERSE

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("features")


def features_for(df: pd.DataFrame) -> pd.DataFrame:
    """Erwartet ein nach Datum sortiertes DataFrame mit close/volume eines
    Tickers (Index = day). Gibt ein DataFrame mit FEATURE_COLUMNS zurück."""
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    ret = close.pct_change()
    out = pd.DataFrame(index=df.index)
    out["r_21"] = close.pct_change(21)
    out["r_63"] = close.pct_change(63)
    out["r_126"] = close.pct_change(126)
    out["r_252"] = close.pct_change(252)
    out["mom_12_1"] = close.shift(21) / close.shift(252) - 1.0
    out["vol_20"] = ret.rolling(20).std()
    out["vol_60"] = ret.rolling(60).std()
    out["rsi_14"] = rsi(close)
    out["sma20_dist"] = close / close.rolling(20).mean() - 1.0
    out["sma50_dist"] = close / close.rolling(50).mean() - 1.0
    out["dist_52w_high"] = close / close.rolling(252).max() - 1.0
    out["vol_ratio"] = volume / volume.rolling(20).mean()
    return out[FEATURE_COLUMNS]


def build_features(conn, tickers: list[str] = None) -> int:
    """Baut Features für alle (oder ausgewählte) Ticker und schreibt sie.
    Gibt die Zahl geschriebener Zeilen zurück."""
    ensure_features_table(conn)
    hist = fetch_prices_history(conn, tickers=tickers)
    if not hist:
        log.warning("Keine Kursdaten in prices_daily – erst Backfill laufen lassen.")
        return 0
    df = pd.DataFrame(hist)
    df["day"] = pd.to_datetime(df["day"])
    total = 0
    for tkr, g in df.groupby("ticker"):
        g = g.sort_values("day").set_index("day")
        if len(g) < 30:                      # zu kurz für sinnvolle Features
            continue
        feats = features_for(g)
        feats = feats.replace([np.inf, -np.inf], np.nan)
        rows = []
        for day, r in feats.iterrows():
            vals = [None if pd.isna(r[c]) else float(r[c]) for c in FEATURE_COLUMNS]
            if all(v is None for v in vals):
                continue
            rows.append((tkr, day.date(), *vals))
        upsert_features(conn, rows)
        total += len(rows)
        log.info("  %-6s %d Zeilen", tkr, len(rows))
    return total


def main():
    ap = argparse.ArgumentParser(description="Feature-Builder (features_daily)")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--tickers", nargs="*", default=None)
    args = ap.parse_args()
    conn = get_connection()
    if args.build:
        n = build_features(conn, tickers=args.tickers or RESEARCH_UNIVERSE)
        log.info("Features fertig: %d Zeilen.", n)
    else:
        log.info("Nichts zu tun. --build angeben.")


if __name__ == "__main__":
    main()
