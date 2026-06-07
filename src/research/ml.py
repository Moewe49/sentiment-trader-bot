"""src/research/ml.py  (D6)
ML-Ranking gegen die Momentum-Baseline – ehrlich validiert.

Frage, die hier beantwortet wird: Bringt ein gelerntes Modell über die 12
Features hinaus etwas gegenüber simplem Momentum? Beide laufen durch DIESELBE
Harness (eval.cross_sectional_backtest) auf dem IDENTISCHEN Out-of-Sample-
Zeitraum – Apfel-zu-Apfel.

Design (bewusst overfit-arm):
  * Ziel y      : Querschnitts-Rang (0..1) des 21-Tage-Forward-Returns je Tag
                  (stationärer als Rohrenditen).
  * Features    : pro Tag cross-sectional perzentil-normalisiert (regime-robust;
                  jede Transformation nutzt nur denselben Tag → kein Look-ahead).
  * Validierung : purged + embargoed EXPANDING walk-forward (Embargo = 21 Tage
                  = Label-Horizont, damit Train-Labels nicht in den Test lecken).
  * Modell      : HistGradientBoosting (CPU, NaN-fähig), konservativ reguliert.
  * Vergleich   : 12-1-Momentum (Baseline) und ein transparenter Momentum-Blend
                  (Mittel der Momentum-Feature-Ränge) – wenn der Blend ~ GBM,
                  lohnt die Komplexität nicht.

Metriken: Information Coefficient (IC = Ø tägliche Spearman-Korr. von Score und
Forward-Return) + die Equity-Kennzahlen der Harness.

CLI:
    python -m src.research.ml --hold 21 --top-q 0.3 --splits 6
"""
import argparse
import logging

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from ..db import get_connection, FEATURE_COLUMNS
from ..universe import RESEARCH_UNIVERSE
from .eval import (load_panel, cross_sectional_backtest, _print,
                   purged_walk_forward_splits, xs_pct as _xs_pct,
                   add_mom_blend, MOM_FEATURES)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("ml")

HORIZON = 21


def daily_ic(panel: pd.DataFrame, score_col: str, ret_col: str) -> float:
    """Ø täglicher Spearman-IC zwischen Score und Forward-Return."""
    def _ic(grp):
        if grp[score_col].notna().sum() < 5:
            return np.nan
        return grp[[score_col, ret_col]].corr(method="spearman").iloc[0, 1]
    ics = panel.groupby("day").apply(_ic)
    return float(ics.mean())


def train_walk_forward(panel: pd.DataFrame, horizon: int = HORIZON,
                       n_splits: int = 6, embargo: int = None) -> pd.DataFrame:
    """Expanding purged walk-forward. Trainiert je Fold auf der Vergangenheit
    und sagt das (out-of-sample) Test-Fenster vorher. Gibt (ticker, day,
    ml_score) der OOS-Predictions zurück. Embargo = Label-Horizont."""
    if embargo is None:
        embargo = horizon
    ret_col = f"r_fwd_{horizon}"
    data = panel.dropna(subset=[ret_col]).copy()
    # Ziel: Querschnitts-Rang des Forward-Returns je Tag
    data["y"] = data.groupby("day")[ret_col].rank(pct=True)
    # Features tag-intern perzentil-normalisieren
    data = _xs_pct(data, FEATURE_COLUMNS)

    days = np.sort(data["day"].unique())
    preds = []
    for k, (train_days, test_days) in enumerate(
            purged_walk_forward_splits(days, n_splits, embargo), 1):
        if len(train_days) < 252 or len(test_days) == 0:
            continue
        tr = data[data["day"].isin(train_days)]
        te = data[data["day"].isin(test_days)]
        model = HistGradientBoostingRegressor(
            learning_rate=0.05, max_iter=400, max_leaf_nodes=15,
            min_samples_leaf=200, l2_regularization=1.0,
            early_stopping=True, validation_fraction=0.1, random_state=42)
        model.fit(tr[FEATURE_COLUMNS].values, tr["y"].values)
        p = model.predict(te[FEATURE_COLUMNS].values)
        sub = te[["ticker", "day"]].copy()
        sub["ml_score"] = p
        preds.append(sub)
        log.info("  Fold %d: Train %d Tage → Test %d Tage (%s…%s)",
                 k, len(train_days), len(test_days),
                 pd.Timestamp(test_days[0]).date(),
                 pd.Timestamp(test_days[-1]).date())
    return pd.concat(preds, ignore_index=True) if preds else pd.DataFrame()


def run(conn, top_q: float = 0.3, hold: int = 21, cost_bps: float = 10.0,
        n_splits: int = 6):
    raw = load_panel(conn, tickers=RESEARCH_UNIVERSE)
    if raw.empty:
        log.warning("Panel leer – erst features bauen.")
        return
    ret_col = f"r_fwd_{hold}"

    preds = train_walk_forward(raw, horizon=hold, n_splits=n_splits)
    if preds.empty:
        log.warning("Keine OOS-Predictions erzeugt.")
        return

    oos_days = set(preds["day"])
    raw_oos = raw[raw["day"].isin(oos_days)].copy()
    # Momentum-Blend: Mittel der (tag-intern perzentilisierten) Momentum-Ränge
    blend = _xs_pct(raw_oos, MOM_FEATURES)
    raw_oos["mom_blend"] = blend[MOM_FEATURES].mean(axis=1)
    ml_panel = raw_oos.merge(preds, on=["ticker", "day"], how="left")

    log.info("OOS-Zeitraum: %s … %s | %d Tage | %d Ticker",
             min(oos_days).date() if hasattr(min(oos_days), 'date')
             else pd.Timestamp(min(oos_days)).date(),
             pd.Timestamp(max(oos_days)).date(),
             len(oos_days), raw_oos["ticker"].nunique())

    # Information Coefficients (höher = besser; ~0.03+ gilt als brauchbar)
    ic_ml = daily_ic(ml_panel, "ml_score", ret_col)
    ic_mom = daily_ic(raw_oos.assign(s=raw_oos["mom_12_1"]), "s", ret_col)
    ic_blend = daily_ic(ml_panel.assign(s=ml_panel["mom_blend"]), "s", ret_col)
    log.info("IC (Ø tägl. Spearman):  Momentum %.4f | Blend %.4f | ML %.4f",
             ic_mom, ic_blend, ic_ml)

    # Gleiche Harness, gleicher OOS-Zeitraum, gleiche Rebalancing-Tage
    res_mom = cross_sectional_backtest(raw_oos, "mom_12_1", top_q, hold, cost_bps)
    res_blend = cross_sectional_backtest(ml_panel, "mom_blend", top_q, hold, cost_bps)
    res_ml = cross_sectional_backtest(ml_panel, "ml_score", top_q, hold, cost_bps)

    log.info("")
    _print(f"Momentum 12-1  (OOS, top {int(top_q*100)}%, hold {hold}d)", res_mom)
    _print(f"Momentum-Blend (OOS, top {int(top_q*100)}%, hold {hold}d)", res_blend)
    _print(f"ML-GBM         (OOS, top {int(top_q*100)}%, hold {hold}d)", res_ml)


def main():
    ap = argparse.ArgumentParser(description="ML-Ranking vs. Momentum (OOS)")
    ap.add_argument("--top-q", type=float, default=0.3)
    ap.add_argument("--hold", type=int, default=HORIZON)
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--splits", type=int, default=6)
    args = ap.parse_args()
    conn = get_connection()
    run(conn, top_q=args.top_q, hold=args.hold, cost_bps=args.cost_bps,
        n_splits=args.splits)


if __name__ == "__main__":
    main()
