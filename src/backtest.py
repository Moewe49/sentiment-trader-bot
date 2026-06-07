"""src/backtest.py
Backtesting der technischen Strategie auf historischen Kursdaten.

Kostenlos: nutzt yfinance (Gratis-Kursdaten) + pandas. Verwendet exakt dieselbe
Score-Logik wie der Live-Bot (src/indicators.py), damit der Backtest aussagekräftig
ist und nicht von der echten Engine abweicht.

WICHTIG zur Aussagekraft:
  - Getestet wird der TECHNISCHE Teil der Strategie (Trend + RSI). Das Sentiment
    fließt hier NICHT ein, weil dafür jahrelange historische Headline-Daten nötig
    wären, die der Bot erst über die Zeit selbst aufbaut. Der Backtest zeigt also,
    ob die technische Basis trägt – die Sentiment-Komponente kommt im Echtbetrieb
    obendrauf.
  - Strategie hier: Long-only. KAUFEN wenn Score über der Kauf-Schwelle, komplett
    VERKAUFEN (in Cash) wenn Score unter der Verkauf-Schwelle. Gehandelt wird zum
    Schlusskurs des FOLGETAGES (kein Blick in die Zukunft).

Zusätzliche Robustheits-Checks (gegen Selbstbetrug / Overfitting):
  --slippage-bps  realistische Handelskosten je Wechsel (Default 5 = 0,05%)
  --mc            Monte-Carlo: zieht Trades mit Zurücklegen → Verteilung von
                  Endrendite & Drawdown ('Können oder Glück?')
  --wf            Walk-Forward: Schwellen rollierend auf ~9 Mon. tunen, auf den
                  folgenden ~3 Mon. (ungesehen) testen → ehrlichste Zahl

Aufruf (Beispiele):
  python -m src.backtest
  python -m src.backtest --tickers XLK XLE XLF --period 10y --mc --wf
  python -m src.backtest --tickers SPY --period 10y --buy 0.3 --sell -0.3 --mc
"""
import argparse
import sys

import numpy as np
import pandas as pd

from .indicators import technical_score_series, rsi, SMA_WINDOW

TRADING_DAYS = 252


# -------------------------------------------------------------------------
# Eine einzelne Aktie/ETF durchsimulieren
# -------------------------------------------------------------------------
def _run_strategy(close: pd.Series, score: pd.Series, buy_th: float,
                  sell_th: float, cost_bps: float,
                  max_rsi: float = None, max_sma_dist: float = None):
    """Kern der Long-only-Strategie über eine (Teil-)Kursreihe.
    Gibt (Strategie-Tagesrenditen, Liste der Round-Trip-Renditen) zurück.
    `cost_bps` = Gebühren + Slippage je Positionswechsel (Basispunkte).
    Optionaler Entry-Filter: kein Kauf bei RSI > max_rsi oder Kurs > max_sma_dist
    über dem SMA (verhindert das Kaufen überdehnter Werte)."""
    sma = close.rolling(SMA_WINDOW).mean()
    r = rsi(close)
    sc = score.values
    cv, smav, rv = close.values, sma.values, r.values
    state = 0
    positions = []
    for i in range(len(score)):
        s = sc[i]
        if not np.isnan(s):
            if state == 0 and s > buy_th:
                ok = True
                if max_rsi is not None and not np.isnan(rv[i]) and rv[i] > max_rsi:
                    ok = False
                if (ok and max_sma_dist is not None and not np.isnan(smav[i])
                        and smav[i] > 0 and (cv[i] - smav[i]) / smav[i] > max_sma_dist):
                    ok = False
                if ok:
                    state = 1
            elif state == 1 and s < sell_th:
                state = 0
        positions.append(state)
    pos = pd.Series(positions, index=close.index)

    # Heute gehaltene Position wurde aus dem GESTRIGEN Score entschieden
    held = pos.shift(1).fillna(0)
    daily_ret = close.pct_change().fillna(0.0)
    strat_ret = held * daily_ret

    # Kosten (Gebühren + Slippage) bei jedem Positionswechsel abziehen
    switches = held.diff().abs().fillna(0.0)
    strat_ret = strat_ret - switches * (cost_bps / 10_000.0)

    # Round-Trips (Einstieg → Ausstieg) für Trefferquote / Monte-Carlo
    trades = []
    entry_price = None
    for i in range(1, len(held)):
        prev, now = held.iloc[i - 1], held.iloc[i]
        if prev == 0 and now == 1:
            entry_price = close.iloc[i]
        elif prev == 1 and now == 0 and entry_price is not None:
            trades.append(close.iloc[i] / entry_price - 1)
            entry_price = None
    if entry_price is not None:   # am Ende noch investiert → zum letzten Kurs schließen
        trades.append(close.iloc[-1] / entry_price - 1)

    return strat_ret, trades


def simulate_ticker(close: pd.Series, buy_th: float, sell_th: float,
                    commission_bps: float = 0.0, slippage_bps: float = 0.0,
                    max_rsi: float = None, max_sma_dist: float = None) -> dict:
    """Simuliert die Long-only-Strategie für eine Kursreihe.
    Gibt Kennzahlen + Equity-Kurve + die Liste der Einzeltrade-Renditen zurück."""
    close = close.astype(float).dropna()
    score = technical_score_series(close)
    strat_ret, trades = _run_strategy(close, score, buy_th, sell_th,
                                      commission_bps + slippage_bps,
                                      max_rsi, max_sma_dist)
    equity = (1 + strat_ret).cumprod()

    wins = sum(1 for t in trades if t > 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0

    return {
        "equity": equity,
        "total_return": equity.iloc[-1] - 1,
        "cagr": _cagr(equity),
        "max_dd": _max_drawdown(equity),
        "sharpe": _sharpe(strat_ret),
        "n_trades": len(trades),
        "win_rate": win_rate,
        "buy_hold": close.iloc[-1] / close.iloc[0] - 1,
        "trades": trades,
    }


# -------------------------------------------------------------------------
# Monte-Carlo: Trade-Reihenfolge 1000x mischen → Robustheit / Drawdown-Risiko
# -------------------------------------------------------------------------
def monte_carlo(trade_returns: list, n_perms: int = 1000, seed: int = 42) -> dict:
    """Zieht aus den beobachteten Trades viele Male einen neuen Satz gleicher
    Größe MIT Zurücklegen (Bootstrap) und schaut, wie Endrendite und maximaler
    Drawdown streuen. Antwort auf: 'Können oder Glück?' und 'Wie schlimm kann der
    Drawdown realistisch werden?'. (Reines Umsortieren würde die Endrendite nicht
    verändern – Multiplikation ist kommutativ –, daher Ziehen mit Zurücklegen.)"""
    arr = np.asarray([t for t in trade_returns if t is not None], dtype=float)
    if len(arr) < 5:
        return None
    rng = np.random.default_rng(seed)
    finals, dds = [], []
    for _ in range(n_perms):
        sample = rng.choice(arr, size=len(arr), replace=True)
        eq = np.cumprod(1.0 + sample)
        finals.append(eq[-1] - 1.0)
        dds.append(float((eq / np.maximum.accumulate(eq) - 1.0).min()))
    finals, dds = np.array(finals), np.array(dds)
    p = lambda a, q: float(np.percentile(a, q))
    return {
        "n_perms": n_perms, "n_trades": len(arr),
        "ret_p5": p(finals, 5), "ret_p50": p(finals, 50), "ret_p95": p(finals, 95),
        "dd_median": p(dds, 50), "dd_worst": float(dds.min()),
        "prob_loss": float((finals < 0).mean()),
    }


# -------------------------------------------------------------------------
# Walk-Forward: rollierend auf Vergangenheit tunen, auf Zukunft testen
# -------------------------------------------------------------------------
def walk_forward(close: pd.Series, cost_bps: float = 0.0,
                 is_days: int = 189, oos_days: int = 63,
                 buy_grid=(0.2, 0.3, 0.5),
                 sell_grid=(-0.2, -0.3, -0.5)) -> dict:
    """Optimiert die Schwellen rollierend auf einem In-Sample-Fenster (~9 Mon.)
    und testet sie auf dem direkt folgenden, UNGESEHENEN Out-of-Sample-Fenster
    (~3 Mon.). Verhindert Overfitting – zeigt realistische Performance."""
    close = close.astype(float).dropna()
    score = technical_score_series(close)
    n = len(close)
    segments = []
    windows = []
    start = 0
    while start + is_days + oos_days <= n:
        is_sl = slice(start, start + is_days)
        oos_sl = slice(start + is_days, start + is_days + oos_days)
        # Beste Schwellen auf dem In-Sample-Fenster (nach Sharpe)
        best = None
        for b in buy_grid:
            for s in sell_grid:
                r, _ = _run_strategy(close.iloc[is_sl], score.iloc[is_sl], b, s, cost_bps)
                sh = _sharpe(r)
                if best is None or sh > best[0]:
                    best = (sh, b, s)
        _, bb, bs = best
        # Auf das Out-of-Sample-Fenster anwenden
        r_oos, _ = _run_strategy(close.iloc[oos_sl], score.iloc[oos_sl], bb, bs, cost_bps)
        segments.append(r_oos)
        windows.append({"oos_start": close.index[start + is_days].date(),
                        "buy": bb, "sell": bs,
                        "oos_ret": float((1 + r_oos).prod() - 1)})
        start += oos_days

    if not segments:
        return None
    oos_ret = pd.concat(segments)
    eq = (1 + oos_ret).cumprod()
    return {
        "equity": eq, "windows": windows,
        "total_return": float(eq.iloc[-1] - 1), "cagr": _cagr(eq),
        "max_dd": _max_drawdown(eq), "sharpe": _sharpe(oos_ret),
        "n_windows": len(windows),
    }


# -------------------------------------------------------------------------
# Kennzahlen
# -------------------------------------------------------------------------
def _cagr(equity: pd.Series) -> float:
    years = len(equity) / TRADING_DAYS
    if years <= 0 or equity.iloc[-1] <= 0:
        return 0.0
    return equity.iloc[-1] ** (1 / years) - 1


def _max_drawdown(equity: pd.Series) -> float:
    cummax = equity.cummax()
    return float((equity / cummax - 1).min())


def _sharpe(returns: pd.Series) -> float:
    sd = returns.std()
    if sd == 0 or np.isnan(sd):
        return 0.0
    return float(returns.mean() / sd * np.sqrt(TRADING_DAYS))


# -------------------------------------------------------------------------
# Ausgabe
# -------------------------------------------------------------------------
def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def print_report(results: dict, period: str):
    header = (f"{'Ticker':<8}{'Rendite':>10}{'B&H':>10}{'CAGR':>9}"
              f"{'MaxDD':>9}{'Sharpe':>8}{'Trades':>8}{'Treffer':>9}")
    print("\n" + "=" * len(header))
    print(f"BACKTEST (technisch)  |  Zeitraum: {period}")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for tkr, r in results.items():
        print(f"{tkr:<8}{_pct(r['total_return']):>10}{_pct(r['buy_hold']):>10}"
              f"{_pct(r['cagr']):>9}{_pct(r['max_dd']):>9}"
              f"{r['sharpe']:>8.2f}{r['n_trades']:>8}{r['win_rate']:>8.0f}%")
    print("=" * len(header))
    print("Rendite = Strategie | B&H = Buy & Hold (Vergleich) | MaxDD = max. Drawdown")
    print("Hinweis: nur technische Signale, ohne Sentiment-Komponente.\n")


def print_montecarlo(mc: dict):
    if not mc:
        print("Monte-Carlo: zu wenige Trades (<5) für eine sinnvolle Streuung.\n")
        return
    print("=" * 60)
    print(f"MONTE-CARLO  |  {mc['n_perms']} gemischte Reihenfolgen, "
          f"{mc['n_trades']} Trades")
    print("=" * 60)
    print(f"  Endrendite   schlecht (5%):  {_pct(mc['ret_p5'])}")
    print(f"               typisch (Median): {_pct(mc['ret_p50'])}")
    print(f"               gut (95%):       {_pct(mc['ret_p95'])}")
    print(f"  Max-Drawdown typisch:        {_pct(mc['dd_median'])}")
    print(f"               worst case:     {_pct(mc['dd_worst'])}")
    print(f"  Wahrscheinlichkeit Verlust:  {mc['prob_loss'] * 100:.0f}%")
    print("Lesart: je enger die Spanne & je näher an 0% die Verlust-Wahrsch., "
          "desto robuster (weniger Glück).\n")


def print_walkforward(wf_results: dict):
    print("=" * 64)
    print("WALK-FORWARD  |  out-of-sample (auf je ~9 Mon. getunt, ~3 Mon. getestet)")
    print("=" * 64)
    print(f"{'Ticker':<8}{'OOS-Rendite':>13}{'CAGR':>9}{'MaxDD':>9}"
          f"{'Sharpe':>8}{'Fenster':>9}")
    print("-" * 64)
    for tkr, wf in wf_results.items():
        if not wf:
            print(f"{tkr:<8}{'zu wenig Daten':>13}")
            continue
        print(f"{tkr:<8}{_pct(wf['total_return']):>13}{_pct(wf['cagr']):>9}"
              f"{_pct(wf['max_dd']):>9}{wf['sharpe']:>8.2f}{wf['n_windows']:>9}")
    print("=" * 64)
    print("Das ist die EHRLICHE Zahl: nur auf nie-gesehenen Daten, Schwellen "
          "rollierend nachgezogen.\n")


# -------------------------------------------------------------------------
def run(tickers: list[str], period: str, buy_th: float, sell_th: float,
        commission_bps: float, slippage_bps: float = 0.0,
        do_wf: bool = False, max_rsi: float = None,
        max_sma_dist: float = None) -> tuple[dict, dict]:
    import yfinance as yf  # lazy: nur fürs echte Laden nötig, nicht für Tests

    results = {}
    wf_results = {}
    equity_curves = []
    cost_bps = commission_bps + slippage_bps

    for tkr in tickers:
        print(f"Lade Kursdaten für {tkr} ({period}) ...", file=sys.stderr)
        df = yf.download(tkr, period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 30:
            print(f"  ⚠ Zu wenig Daten für {tkr} – übersprungen.", file=sys.stderr)
            continue
        close = df["Close"].squeeze()
        res = simulate_ticker(close, buy_th, sell_th, commission_bps, slippage_bps,
                              max_rsi, max_sma_dist)
        results[tkr] = res
        equity_curves.append(res["equity"].rename(tkr))
        if do_wf:
            wf_results[tkr] = walk_forward(close, cost_bps=cost_bps)

    # Gleichgewichtetes Portfolio über alle Ticker
    if len(equity_curves) > 1:
        combined = pd.concat(equity_curves, axis=1).ffill().dropna()
        # jede Kurve auf Start 1.0 normieren, dann mitteln
        norm = combined / combined.iloc[0]
        port_equity = norm.mean(axis=1)
        port_ret = port_equity.pct_change().fillna(0.0)
        results["PORTF."] = {
            "equity": port_equity,
            "total_return": port_equity.iloc[-1] - 1,
            "cagr": _cagr(port_equity),
            "max_dd": _max_drawdown(port_equity),
            "sharpe": _sharpe(port_ret),
            "n_trades": sum(results[t]["n_trades"] for t in tickers if t in results),
            "win_rate": float(np.mean([results[t]["win_rate"]
                                       for t in tickers if t in results])),
            "buy_hold": float(np.mean([results[t]["buy_hold"]
                                       for t in tickers if t in results])),
        }

    return results, wf_results


def main():
    ap = argparse.ArgumentParser(description="Backtest der technischen Strategie")
    ap.add_argument("--tickers", nargs="+", default=["XLK", "XLE", "XLF"],
                    help="Ticker-Symbole (Default: XLK XLE XLF)")
    ap.add_argument("--period", default="5y",
                    help="Zeitraum, z.B. 1y, 5y, 10y, max (Default: 5y)")
    ap.add_argument("--buy", type=float, default=0.30,
                    help="Kauf-Schwelle (Default: 0.30)")
    ap.add_argument("--sell", type=float, default=-0.30,
                    help="Verkauf-Schwelle (Default: -0.30)")
    ap.add_argument("--commission-bps", type=float, default=0.0,
                    help="Gebühren je Wechsel in Basispunkten (Default: 0)")
    ap.add_argument("--slippage-bps", type=float, default=5.0,
                    help="Slippage je Wechsel in Basispunkten (Default: 5 = 0,05%)")
    ap.add_argument("--mc", action="store_true",
                    help="Monte-Carlo-Robustheitstest (Trade-Reihenfolge mischen)")
    ap.add_argument("--wf", action="store_true",
                    help="Walk-Forward-Validation (out-of-sample, ehrlichste Zahl)")
    ap.add_argument("--max-rsi", type=float, default=None,
                    help="Entry-Filter: kein Kauf wenn RSI über diesem Wert (z.B. 72)")
    ap.add_argument("--max-sma-dist", type=float, default=None,
                    help="Entry-Filter: kein Kauf wenn Kurs > x über SMA (z.B. 0.12)")
    args = ap.parse_args()

    results, wf_results = run(args.tickers, args.period, args.buy, args.sell,
                              args.commission_bps, args.slippage_bps, do_wf=args.wf,
                              max_rsi=args.max_rsi, max_sma_dist=args.max_sma_dist)
    if not results:
        print("Keine Ergebnisse – Kursdaten konnten nicht geladen werden.",
              file=sys.stderr)
        sys.exit(1)
    print_report(results, args.period)

    if args.mc:
        pooled = []
        for t in args.tickers:
            if t in results:
                pooled.extend(results[t].get("trades", []))
        print_montecarlo(monte_carlo(pooled))

    if args.wf and wf_results:
        print_walkforward(wf_results)


if __name__ == "__main__":
    main()
