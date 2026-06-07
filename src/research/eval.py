"""src/research/eval.py  (D4 + D5)
Validierungs-Harness für Querschnitts-Strategien ("welche Werte ranken oben").

Kernidee (kein Look-ahead): an jedem Rebalancing-Tag t werden die Werte nach
einem Score (aus Features bei t) sortiert; das Top-Quantil wird gleichgewichtet
gekauft und über `hold` Handelstage gehalten – der realisierte Ertrag ist der
Forward-Return t→t+hold (Label). Nicht überlappende Halteperioden ⇒ sauberes
Aufzinsen ohne Doppelzählung.

Bewertet wird gegen zwei Benchmarks:
  * EW-Universum : gleichgewichtetes Halten ALLER Werte (Markt-Proxy)
  * (Aufrufer kann weitere Scores vergleichen)

Zusätzlich: purged + embargoed Walk-Forward-Splits für späteres ML-Training
(verhindert Leakage über überlappende Label-Fenster).

CLI (D5 – Momentum-Baseline):
    python -m src.research.eval --score mom_12_1 --top-q 0.2 --hold 21
    python -m src.research.eval --score mom_12_1 --hold 21 --start 2018-01-01
"""
import argparse
import logging

import numpy as np
import pandas as pd

from ..db import get_connection, fetch_features, fetch_prices_history
from ..universe import RESEARCH_UNIVERSE
from .labels import forward_returns

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("eval")

# Mehr-Horizont-Momentum-Features für den transparenten "Momentum-Blend".
MOM_FEATURES = ["r_63", "r_126", "r_252", "mom_12_1"]


def xs_pct(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Ersetzt die Spalten durch ihren Querschnitts-Perzentilrang je Tag
    (rein tag-intern → keine zeitliche Leckage)."""
    out = df.copy()
    g = out.groupby("day")
    for c in cols:
        out[c] = g[c].rank(pct=True)
    return out


def add_mom_blend(panel: pd.DataFrame) -> pd.DataFrame:
    """Fügt 'mom_blend' hinzu: Mittel der tag-intern perzentilisierten
    Momentum-Feature-Ränge (transparenter Nicht-ML-Composite)."""
    out = panel.copy()
    ranked = xs_pct(out, MOM_FEATURES)
    out["mom_blend"] = ranked[MOM_FEATURES].mean(axis=1)
    return out


def build_market_index(conn, tickers: list[str] = None) -> pd.DataFrame:
    """Gleichgewichteter Markt-Index aus prices_daily als Regime-Proxy.
    Gibt DataFrame (Index=day) mit level, sma200, vol_ann zurück."""
    hist = fetch_prices_history(conn, tickers=tickers)
    if not hist:
        return pd.DataFrame()
    df = pd.DataFrame(hist)
    df["day"] = pd.to_datetime(df["day"])
    wide = df.pivot(index="day", columns="ticker", values="close").sort_index()
    mret = wide.pct_change().mean(axis=1)            # EW-Tagesrendite
    level = (1.0 + mret.fillna(0)).cumprod()
    out = pd.DataFrame({"ret": mret, "level": level})
    out["sma200"] = level.rolling(200).mean()
    out["vol_ann"] = mret.rolling(60).std() * np.sqrt(252)
    return out


def load_panel(conn, tickers: list[str] = None, start=None) -> pd.DataFrame:
    """Join aus Features + Forward-Returns über (ticker, day)."""
    feats = pd.DataFrame(fetch_features(conn, tickers=tickers, start=start))
    if feats.empty:
        return feats
    feats["day"] = pd.to_datetime(feats["day"])
    lab = forward_returns(conn, tickers=tickers)
    panel = feats.merge(lab, on=["ticker", "day"], how="inner")
    if start:
        panel = panel[panel["day"] >= pd.Timestamp(start)]
    return panel.sort_values(["day", "ticker"]).reset_index(drop=True)


def _metrics(rets: list[float], eq: list[float], hold: int) -> dict:
    rets = np.asarray(rets, dtype=float)
    eq = np.asarray(eq, dtype=float)
    ppy = 252.0 / hold                       # Perioden pro Jahr
    sharpe = (rets.mean() / rets.std() * np.sqrt(ppy)) if rets.std() > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq / peak - 1.0).min()) if len(eq) else 0.0
    years = len(rets) * hold / 252.0
    total = float(eq[-1] - 1.0) if len(eq) else 0.0
    cagr = float(eq[-1] ** (1.0 / years) - 1.0) if years > 0 and len(eq) else 0.0
    win = float((rets > 0).mean()) if len(rets) else 0.0
    return {"total": total, "cagr": cagr, "sharpe": sharpe,
            "max_dd": max_dd, "win": win, "periods": len(rets)}


def cross_sectional_backtest(panel: pd.DataFrame, score_col: str,
                             top_q: float = 0.2, hold: int = 21,
                             cost_bps: float = 10.0,
                             ascending: bool = False,
                             market: pd.DataFrame = None,
                             regime: bool = False,
                             vol_target: float = None,
                             max_exposure: float = 1.0) -> dict:
    """Long-only Top-Quantil-Strategie auf `score_col`. Optionaler Risiko-
    Overlay (nur DE-risken, nie hebeln):
      * regime=True   : Markt unter SMA200 → in Cash (Exposure 0).
      * vol_target=x  : Exposure = min(max_exposure, x / annualisierte Marktvola).
    `market` ist der DataFrame aus build_market_index (Index=day).
    Gibt Kennzahlen + Equity-Kurven (Strategie & EW-Benchmark) zurück."""
    horizon = hold                            # Halten = Forward-Return-Fenster
    rcol = f"r_fwd_{horizon}"
    if rcol not in panel.columns:
        raise ValueError(f"Forward-Return {rcol} fehlt – Horizont nicht geladen.")
    days = np.sort(panel["day"].unique())
    rebal = days[::hold]                      # nicht überlappende Perioden
    use_overlay = market is not None and (regime or vol_target)

    eq, beq = 1.0, 1.0
    s_curve, b_curve, s_rets, b_rets, turns, exps = [], [], [], [], [], []
    prev: set = set()

    for t in rebal:
        sl = panel[panel["day"] == t].dropna(subset=[score_col, rcol])
        if len(sl) < 5:                       # zu wenige Werte für Querschnitt
            continue
        k = max(1, int(round(len(sl) * top_q)))
        picks = sl.sort_values(score_col, ascending=ascending).head(k)
        sel = set(picks["ticker"])

        # Risiko-Overlay: Exposure 0..max_exposure aus Markt-Regime/-Vola
        exposure = 1.0
        if use_overlay and t in market.index:
            row = market.loc[t]
            if regime and not np.isnan(row["sma200"]) and row["level"] < row["sma200"]:
                exposure = 0.0
            if (vol_target and exposure > 0 and not np.isnan(row["vol_ann"])
                    and row["vol_ann"] > 0):
                exposure = min(max_exposure, vol_target / row["vol_ann"])
        exps.append(exposure)

        turn = 1.0 if not prev else len(sel ^ prev) / max(len(sel), 1)
        cost = turn * cost_bps / 1e4
        port = exposure * (float(picks[rcol].mean()) - cost)
        bench = float(sl[rcol].mean())

        eq *= (1.0 + port); beq *= (1.0 + bench)
        s_rets.append(port); b_rets.append(bench); turns.append(turn)
        s_curve.append(eq); b_curve.append(beq)
        prev = sel

    m = _metrics(s_rets, s_curve, hold)
    bm = _metrics(b_rets, b_curve, hold)
    m["avg_turnover"] = float(np.mean(turns)) if turns else 0.0
    m["avg_holdings"] = k if 'k' in dir() else 0
    m["avg_exposure"] = float(np.mean(exps)) if exps else 1.0
    return {"strategy": m, "benchmark": bm,
            "rebalances": len(s_rets)}


def purged_walk_forward_splits(days: np.ndarray, n_splits: int = 5,
                               embargo: int = 21):
    """Generator (train_days, test_days) für späteres ML: zeitliche Splits mit
    Embargo-Lücke, damit Train-Labels nicht in den Test-Zeitraum lecken."""
    days = np.sort(np.asarray(days))
    fold = len(days) // (n_splits + 1)
    for i in range(1, n_splits + 1):
        test_start = i * fold
        test_end = min((i + 1) * fold, len(days))
        train_end = max(0, test_start - embargo)
        yield days[:train_end], days[test_start:test_end]


def _print(title: str, res: dict):
    s, b = res["strategy"], res["benchmark"]
    log.info("=== %s  (%d Rebalancings) ===", title, res["rebalances"])
    log.info("  %-10s %9s %9s %8s %8s %7s",
             "", "Gesamt", "CAGR", "Sharpe", "MaxDD", "Win%")
    log.info("  %-10s %+8.1f%% %+8.1f%% %8.2f %+7.1f%% %6.0f%%",
             "Strategie", s["total"]*100, s["cagr"]*100, s["sharpe"],
             s["max_dd"]*100, s["win"]*100)
    log.info("  %-10s %+8.1f%% %+8.1f%% %8.2f %+7.1f%% %6.0f%%",
             "EW-Markt", b["total"]*100, b["cagr"]*100, b["sharpe"],
             b["max_dd"]*100, b["win"]*100)
    log.info("  Ø Turnover/Periode %.0f%% | Ø Positionen %d | Ø Exposure %.0f%%",
             s["avg_turnover"]*100, s["avg_holdings"],
             s.get("avg_exposure", 1.0)*100)


def main():
    ap = argparse.ArgumentParser(description="Validierungs-Harness (cross-sectional)")
    ap.add_argument("--score", default="mom_12_1",
                    help="Feature-Spalte als Score (Default: mom_12_1)")
    ap.add_argument("--top-q", type=float, default=0.2)
    ap.add_argument("--hold", type=int, default=21,
                    help="Halte-/Rebalancing-Fenster in Handelstagen (1/5/21)")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--ascending", action="store_true",
                    help="niedriger Score = besser (z.B. für Mean-Reversion)")
    ap.add_argument("--regime", action="store_true",
                    help="Risiko-Overlay: Markt unter SMA200 → in Cash")
    ap.add_argument("--vol-target", type=float, default=None,
                    help="Risiko-Overlay: Ziel-Jahresvola (z.B. 0.18); skaliert "
                         "Exposure runter, nie über 100%%")
    args = ap.parse_args()

    conn = get_connection()
    panel = load_panel(conn, tickers=RESEARCH_UNIVERSE, start=args.start)
    if panel.empty:
        log.warning("Panel leer – erst features bauen (python -m src.research.features --build).")
        return
    if args.score == "mom_blend":
        panel = add_mom_blend(panel)
    market = build_market_index(conn, tickers=RESEARCH_UNIVERSE) \
        if (args.regime or args.vol_target) else None
    log.info("Panel: %d Zeilen, %d Ticker, %s … %s",
             len(panel), panel["ticker"].nunique(),
             panel["day"].min().date(), panel["day"].max().date())
    res = cross_sectional_backtest(panel, args.score, top_q=args.top_q,
                                   hold=args.hold, cost_bps=args.cost_bps,
                                   ascending=args.ascending, market=market,
                                   regime=args.regime, vol_target=args.vol_target)
    tag = []
    if args.regime: tag.append("Regime")
    if args.vol_target: tag.append(f"VolZiel {int(args.vol_target*100)}%")
    suffix = (" + " + "+".join(tag)) if tag else ""
    _print(f"Score='{args.score}' top {int(args.top_q*100)}%, "
           f"hold {args.hold}d{suffix}", res)


if __name__ == "__main__":
    main()
