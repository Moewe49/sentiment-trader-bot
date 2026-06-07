"""src/research/shadow.py
Shadow-Modus: der validierte systematische Kern (Momentum-Blend Top 30 % +
SMA200-Regime-Filter) berechnet LIVE sein Ziel-Portfolio und schreibt eine
hypothetische Equity-Kurve fort – OHNE echte Orders. Liefert einen ehrlichen,
risikofreien Forward-Track-Record, bevor echtes (Paper-)Geld dranhängt.

Aufruf wird täglich aus der Signal-Engine getriggert (kein eigener Container).
Manuell:
    python -m src.research.shadow --run      # einen Update-Schritt ausführen
    python -m src.research.shadow --status    # aktuelles Ziel + Kurve zeigen
"""
import argparse
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..db import (get_connection, fetch_prices_history, ensure_shadow_tables,
                  upsert_shadow_portfolio, latest_shadow_portfolio,
                  upsert_shadow_equity, latest_shadow_equity,
                  fetch_shadow_equity, count_shadow_rebalances)
from ..universe import RESEARCH_UNIVERSE

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("shadow")

TOP_Q = 0.20             # Konzentration: Top 20% (~12 Werte) der Momentum-Rangliste
REBAL_DAYS = 21          # monatliches Rebalancing (Handelstage)
MOM_FEATURES = ["r_63", "r_126", "r_252", "mom_12_1"]


def _load_wide(conn, lookback_days: int = 600) -> pd.DataFrame:
    """Wide-Close-DataFrame (Index=day, Spalten=ticker) der letzten Tage."""
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    hist = fetch_prices_history(conn, tickers=RESEARCH_UNIVERSE, start=start)
    if not hist:
        return pd.DataFrame()
    df = pd.DataFrame(hist)
    df["day"] = pd.to_datetime(df["day"])
    return df.pivot(index="day", columns="ticker", values="close").sort_index()


def compute_target(wide: pd.DataFrame, top_q: float = TOP_Q):
    """Aktuelles Ziel-Portfolio + Regime-Status aus dem jüngsten Tag.
    Gibt (day, regime_on, picks, blend_series) zurück."""
    feat = pd.DataFrame(index=wide.columns)
    feat["r_63"] = wide.pct_change(63).iloc[-1]
    feat["r_126"] = wide.pct_change(126).iloc[-1]
    feat["r_252"] = wide.pct_change(252).iloc[-1]
    feat["mom_12_1"] = (wide.shift(21) / wide.shift(252) - 1.0).iloc[-1]
    ranks = feat.rank(pct=True)                 # Querschnitts-Perzentil je Feature
    blend = ranks.mean(axis=1).dropna()
    k = max(1, int(round(len(blend) * top_q)))
    picks = blend.sort_values(ascending=False).head(k).index.tolist()

    mret = wide.pct_change().mean(axis=1)
    level = (1.0 + mret.fillna(0)).cumprod()
    sma200 = level.rolling(200).mean()
    regime_on = (True if np.isnan(sma200.iloc[-1])
                 else bool(level.iloc[-1] >= sma200.iloc[-1]))
    day = wide.index[-1].date()
    return day, regime_on, picks, blend.sort_values(ascending=False)


def update_shadow(conn, top_q: float = TOP_Q, rebal_days: int = REBAL_DAYS):
    """Ein Update-Schritt: ggf. neu rebalancieren + Equity bis zum jüngsten Tag
    fortschreiben. Idempotent (mehrfacher Aufruf am selben Tag schadet nicht)."""
    ensure_shadow_tables(conn)
    wide = _load_wide(conn)
    if wide.empty or len(wide) < 260:
        log.warning("Shadow: zu wenig Kurshistorie (%d Tage) – übersprungen.",
                    0 if wide.empty else len(wide))
        return False
    day, regime_on, picks, _ = compute_target(wide, top_q)

    # 1) Rebalancing fällig? (kein Eintrag oder >= rebal_days Handelstage her)
    last = latest_shadow_portfolio(conn)
    if last is None:
        need_rebal = True
    else:
        days_since = int((wide.index > pd.Timestamp(last[0])).sum())
        need_rebal = days_since >= rebal_days
    if need_rebal:
        upsert_shadow_portfolio(conn, day, regime_on, picks)
        log.info("Shadow-Rebalancing %s | Regime %s | %d Werte: %s",
                 day, "RISK-ON" if regime_on else "RISK-OFF (Cash)",
                 len(picks), ", ".join(picks[:10]) + (" …" if len(picks) > 10 else ""))

    # 2) Mark-to-Market der Equity-Kurve bis zum jüngsten Tag
    le = latest_shadow_equity(conn)
    if le is None:                                   # Start: bei 1.0 verankern
        upsert_shadow_equity(conn, day, 1.0, None,
                             1.0 if regime_on else 0.0,
                             len(picks) if regime_on else 0)
        log.info("Shadow-Equity gestartet bei 1.0 (%s).", day)
        return need_rebal
    last_eq_day, last_level = le[0], float(le[1])
    if day <= last_eq_day:
        return need_rebal                            # schon aktuell

    tgt = latest_shadow_portfolio(conn)              # aktuell aktives Ziel
    t_regime, t_tickers = bool(tgt[1]), list(tgt[2])
    exposure = 1.0 if t_regime else 0.0
    ret = 0.0
    if exposure > 0 and t_tickers:
        cols = [c for c in t_tickers if c in wide.columns]
        try:
            p0 = wide[cols].loc[pd.Timestamp(last_eq_day)]
            p1 = wide[cols].loc[pd.Timestamp(day)]
            ret = float((p1 / p0 - 1.0).mean())
            if np.isnan(ret):
                ret = 0.0
        except KeyError:
            ret = 0.0
    new_level = last_level * (1.0 + exposure * ret)
    upsert_shadow_equity(conn, day, new_level, ret, exposure,
                         len(t_tickers) if exposure > 0 else 0)
    log.info("Shadow-Equity %s: %.4f (%+.2f%% × Exposure %.0f%%)",
             day, new_level, ret * 100, exposure * 100)
    return need_rebal


def status(conn):
    ensure_shadow_tables(conn)
    tgt = latest_shadow_portfolio(conn)
    if not tgt:
        log.info("Noch kein Shadow-Ziel berechnet. 'python -m src.research.shadow --run'")
        return
    log.info("Aktuelles Ziel (Rebalancing %s): Regime %s | %d Werte",
             tgt[0], "RISK-ON" if tgt[1] else "RISK-OFF (Cash)", len(tgt[2]))
    log.info("  %s", ", ".join(tgt[2]))
    curve = fetch_shadow_equity(conn)
    if curve:
        first, last = curve[0], curve[-1]
        log.info("Shadow-Equity: %d Tage, %s … %s | Level %.4f (%+.1f%% seit Start) "
                 "| %d Rebalancings",
                 len(curve), first[0], last[0], float(last[1]),
                 (float(last[1]) - 1.0) * 100, count_shadow_rebalances(conn))


def main():
    ap = argparse.ArgumentParser(description="Shadow-Modus (systematischer Kern, ohne Handel)")
    ap.add_argument("--run", action="store_true", help="Update-Schritt ausführen")
    ap.add_argument("--status", action="store_true", help="Ziel + Kurve anzeigen")
    args = ap.parse_args()
    conn = get_connection()
    if args.run:
        update_shadow(conn)
    if args.status or not args.run:
        status(conn)


if __name__ == "__main__":
    main()
