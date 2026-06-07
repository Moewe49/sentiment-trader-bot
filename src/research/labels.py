"""src/research/labels.py  (D3)
Erzeugt die Lern-Ziele (Labels) aus prices_daily:

  r_fwd_1 / r_fwd_5 / r_fwd_21 – Forward-Return über 1/5/21 Handelstage
  rank_fwd_21                  – Querschnitts-Rang (0..1) des 21-Tage-Forward-
                                 Returns je Datum (für Learning-to-Rank)

Die Labels sind absichtlich GETRENNT von den Features: Features sehen nur die
Vergangenheit, Labels nur die Zukunft. Die Harness (eval.py) joint beides über
(ticker, day) und stellt sicher, dass an Tag t mit Features(t) der Return
t→t+h erzielt wird – kein Look-ahead.

Diese Datei liefert v.a. Funktionen für die Harness; der CLI-Modus zeigt
Label-Statistiken zur Plausibilitätsprüfung.
"""
import argparse
import logging

import numpy as np
import pandas as pd

from ..db import get_connection, fetch_prices_history
from ..universe import RESEARCH_UNIVERSE

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("labels")

HORIZONS = (1, 5, 21)


def forward_returns(conn, tickers: list[str] = None,
                    horizons=HORIZONS) -> pd.DataFrame:
    """Long-DataFrame (ticker, day, r_fwd_<h> …) der Forward-Returns.
    r_fwd_h(t) = close(t+h)/close(t) - 1, je Ticker."""
    hist = fetch_prices_history(conn, tickers=tickers)
    if not hist:
        return pd.DataFrame(columns=["ticker", "day"])
    df = pd.DataFrame(hist)
    df["day"] = pd.to_datetime(df["day"])
    parts = []
    for tkr, g in df.groupby("ticker"):
        g = g.sort_values("day").set_index("day")
        close = g["close"].astype(float)
        lab = pd.DataFrame(index=g.index)
        lab["ticker"] = tkr
        for h in horizons:
            lab[f"r_fwd_{h}"] = close.shift(-h) / close - 1.0
        parts.append(lab.reset_index())
    out = pd.concat(parts, ignore_index=True)
    return out


def add_cross_sectional_rank(panel: pd.DataFrame,
                             col: str = "r_fwd_21") -> pd.DataFrame:
    """Fügt rank_<col> hinzu: Querschnitts-Rang 0..1 je Datum (1 = bester
    Forward-Return). Nützlich als Lernziel für Learning-to-Rank."""
    panel = panel.copy()
    panel[f"rank_{col}"] = (panel.groupby("day")[col]
                            .rank(pct=True, method="average"))
    return panel


def main():
    ap = argparse.ArgumentParser(description="Label-Builder (Forward-Returns)")
    ap.add_argument("--stats", action="store_true",
                    help="Label-Statistiken anzeigen (Plausibilitätscheck)")
    ap.add_argument("--tickers", nargs="*", default=None)
    args = ap.parse_args()
    conn = get_connection()
    lab = forward_returns(conn, tickers=args.tickers or RESEARCH_UNIVERSE)
    if lab.empty:
        log.warning("Keine Daten – erst prices_daily backfillen.")
        return
    if args.stats:
        for h in HORIZONS:
            s = lab[f"r_fwd_{h}"].dropna()
            log.info("r_fwd_%-2d  n=%d  Mittel %+.4f  Std %.4f  "
                     "Min %+.3f  Max %+.3f",
                     h, len(s), s.mean(), s.std(), s.min(), s.max())
        log.info("Zeitraum: %s … %s | Ticker: %d",
                 lab["day"].min().date(), lab["day"].max().date(),
                 lab["ticker"].nunique())


if __name__ == "__main__":
    main()
