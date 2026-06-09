"""src/research/guard.py
Tiefes Risikomanagement für den systematischen Kern. Läuft in JEDEM
Engine-Zyklus (alle 15 Min) während der US-Handelszeit – greift aber nur in
Ausnahmefällen ein. Normalfall: nichts tun.

Schutzschichten (in dieser Reihenfolge):
  1. Kill-Switch      TRADING_ENABLED=Aus → der Guard rührt nichts an.
  2. Drawdown-Bremse  Kontowert fällt mehr als CORE_DD_LIMIT (Default 12%)
                      unter sein 30-Tage-Hoch → ALLES in Cash + Not-Halt
                      (SYSTEMATIC_HALT=an). Harte Notbremse gegen Crashs
                      ZWISCHEN den monatlichen Rebalancings.
  3. Tägliches Regime Markt-Index (EW-Universum) fällt 2% unter seine SMA200
                      → in Cash (ohne Halt). Steigt er wieder ÜBER die SMA200
                      → automatischer Wiedereinstieg ins Ziel-Portfolio.
                      Die 2%-Hysterese verhindert Whipsaw an der Linie.
  4. Halt-Erholung    Der Not-Halt löst sich selbst, wenn das Regime wieder
                      OK ist und der Drawdown unter die halbe Bremse fällt –
                      der Nutzer muss nichts tun (kann ihn im ⚙ aber auch
                      manuell lösen).

Alle Eingriffe nur bei offener Börse, mit Rebalance-Lock gegen Parallel-Läufe.
"""
import logging

import numpy as np

from ..db import equity_peak, set_settings
from ..markets import is_open
from ..signal_engine import _account_equity_and_cash
from .live import rebalance, liquidate_all, _try_lock, _unlock
from .shadow import _load_wide, core_universe

log = logging.getLogger("guard")

_US_REF = "NVDA"
EXIT_BAND = 0.02            # Regime-Exit erst 2% UNTER der SMA200 (Hysterese)


def _market_state(conn):
    """(Index-Level, SMA200) des gleichgewichteten Universums oder None."""
    wide = _load_wide(conn, tickers=core_universe(conn))
    if wide.empty or len(wide) < 260:
        return None
    mret = wide.pct_change().mean(axis=1)
    level = (1.0 + mret.fillna(0)).cumprod()
    sma = level.rolling(200).mean()
    lv, sm = float(level.iloc[-1]), float(sma.iloc[-1])
    if np.isnan(sm):
        return None
    return lv, sm


def daily_guard(conn, executor, cfg: dict):
    """Ein Wach-Durchlauf. Greift nur ein, wenn eine Schutzschicht auslöst."""
    if not cfg.get("SYSTEMATIC_LIVE") or not cfg.get("TRADING_ENABLED", True):
        return
    if not is_open(_US_REF):
        return

    # Kontostand + Drawdown gegen das 30-Tage-Hoch
    net, _cash = _account_equity_and_cash(executor)
    peak = equity_peak(conn, days=30)
    dd = ((peak - net) / peak) if (peak and net > 0) else 0.0

    try:
        positions = executor.get_positions()
    except Exception as e:
        log.error("Guard: Positionen nicht lesbar: %s", e)
        return
    invested = any(q > 0 for q in positions.values())
    state = _market_state(conn)

    # 4) Not-Halt: hält Käufe an; löst sich bei Erholung selbst
    if cfg.get("SYSTEMATIC_HALT"):
        if invested:                       # Halt gesetzt, aber noch investiert
            log.warning("Guard: Not-Halt aktiv, aber noch investiert – stelle glatt.")
            if _try_lock(conn):
                try:
                    liquidate_all(executor)
                finally:
                    _unlock(conn)
            return
        regime_ok = state and state[0] > state[1]
        if regime_ok and dd < cfg["CORE_DD_LIMIT"] / 2:
            set_settings(conn, {"SYSTEMATIC_HALT": "no"})
            log.warning("Guard: Erholung erkannt (Regime OK, Drawdown %.1f%%) – "
                        "Not-Halt gelöst, Wiedereinstieg beim nächsten Zyklus.", dd * 100)
        return

    # 2) Drawdown-Bremse: harter Stopp + Not-Halt
    if invested and cfg["CORE_DD_LIMIT"] and dd >= cfg["CORE_DD_LIMIT"]:
        log.warning("Guard: DRAWDOWN-BREMSE! Kontowert %.0f liegt %.1f%% unter "
                    "dem 30-Tage-Hoch %.0f (Limit %.0f%%) – alles in Cash + Not-Halt.",
                    net, dd * 100, peak, cfg["CORE_DD_LIMIT"] * 100)
        if _try_lock(conn):
            try:
                liquidate_all(executor)
                set_settings(conn, {"SYSTEMATIC_HALT": "yes"})
            finally:
                _unlock(conn)
        return

    # 3) Tägliches Regime mit Hysterese
    if state is None:
        return
    level, sma = state
    if invested and level < sma * (1 - EXIT_BAND):
        log.warning("Guard: Regime-Exit – Markt-Index %.2f liegt >%.0f%% unter "
                    "SMA200 %.2f → alles in Cash (Wiedereinstieg automatisch).",
                    level, EXIT_BAND * 100, sma)
        if _try_lock(conn):
            try:
                liquidate_all(executor)
            finally:
                _unlock(conn)
        return
    if (not invested) and level > sma:
        log.info("Guard: Regime wieder RISK-ON (Index %.2f > SMA200 %.2f) – "
                 "steige ins Ziel-Portfolio ein.", level, sma)
        rebalance(executor, conn, cfg)      # bringt eigene Locks/Gates mit
