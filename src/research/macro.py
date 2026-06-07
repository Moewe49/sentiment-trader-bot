"""src/research/macro.py
Cross-Asset-Korrelation: wie hängt das (tech-lastige) Portfolio mit Gold,
Silber, US-Dollar und US-Anleihen zusammen? Nutzt den historischen Kursspeicher
(prices_daily) – komplett offline, keine Börse nötig.

Aussage je Makro-Asset: Korrelation der Tagesrenditen (Portfolio-Proxy =
gleichgewichtetes Research-Universum) über ein rollierendes Fenster, plus eine
Klartext-Erläuterung.

CLI:
    python -m src.research.macro --show
"""
import argparse
import logging

import numpy as np
import pandas as pd

from ..db import get_connection, fetch_prices_history
from ..universe import RESEARCH_UNIVERSE, MACRO

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [macro] %(levelname)s: %(message)s")
log = logging.getLogger("macro")


def _wide(conn, tickers, lookback_days):
    from datetime import date, timedelta
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    hist = fetch_prices_history(conn, tickers=tickers, start=start)
    if not hist:
        return pd.DataFrame()
    df = pd.DataFrame(hist)
    df["day"] = pd.to_datetime(df["day"])
    return df.pivot(index="day", columns="ticker", values="close").sort_index()


def _explain(label: str, corr: float) -> str:
    if corr != corr:                      # NaN
        return "zu wenig Daten"
    strength = ("stark" if abs(corr) >= 0.5 else
                "moderat" if abs(corr) >= 0.25 else "kaum")
    if abs(corr) < 0.15:
        base = f"{strength} korreliert – läuft weitgehend unabhängig"
    elif corr > 0:
        base = f"{strength} gleichläufig – steigt/fällt tendenziell mit dem Portfolio"
    else:
        base = f"{strength} gegenläufig – wirkt tendenziell als Absicherung (Hedge)"
    notes = {
        "US-Dollar": " Ein starker Dollar ist für Tech/Export oft Gegenwind.",
        "Gold": " Gold gilt als Krisen-/Inflationsschutz.",
        "US-Anleihen": " Anleihen (TLT) steigen bei fallenden Zinsen – meist risk-off.",
        "Silber": " Silber ist Gold-ähnlich, aber volatiler/industrienäher.",
    }
    return base + notes.get(label, "")


def correlations(conn, lookback_days: int = 365, window: int = 180) -> list:
    """Liste (label, ticker, corr, n, erklärung). corr = Korrelation der
    Tagesrenditen über die letzten `window` gemeinsamen Tage."""
    macro_tickers = list(MACRO.values())
    wide = _wide(conn, RESEARCH_UNIVERSE + macro_tickers, lookback_days)
    if wide.empty:
        return []
    rets = wide.pct_change()
    # Portfolio-Proxy: gleichgewichtete Tagesrendite des Universums
    uni_cols = [c for c in RESEARCH_UNIVERSE if c in rets.columns]
    port = rets[uni_cols].mean(axis=1)
    out = []
    for label, tkr in MACRO.items():
        if tkr not in rets.columns:
            out.append((label, tkr, float("nan"), 0, "keine Daten"))
            continue
        pair = pd.concat([port, rets[tkr]], axis=1).dropna().tail(window)
        n = len(pair)
        corr = float(pair.iloc[:, 0].corr(pair.iloc[:, 1])) if n >= 30 else float("nan")
        out.append((label, tkr, corr, n, _explain(label, corr)))
    return out


def main():
    ap = argparse.ArgumentParser(description="Cross-Asset-Korrelation")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--window", type=int, default=180)
    ap.parse_args()
    conn = get_connection()
    for label, tkr, corr, n, why in correlations(conn):
        cs = "n/a" if corr != corr else f"{corr:+.2f}"
        log.info("  %-12s (%s)  Korr %s  [%d Tage]  – %s", label, tkr, cs, n, why)


if __name__ == "__main__":
    main()
