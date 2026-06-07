"""src/research/live.py
Live-Rebalancer des validierten systematischen Kerns (Momentum-Blend Top 30 % +
SMA200-Regime). Schichtet das echte (Paper-)Konto auf das Ziel-Portfolio um:
gleichgewichtet, voll investiert bei Risk-On, komplett Cash bei Risk-Off.
KEINE Einzel-Stops (Risiko = Regime + Diversifikation + monatliches Rebalancing).

Sicherheits-Design:
  * compute_orders() ist eine REINE Funktion (ohne IB) → trocken testbar.
  * Verkäufe zuerst (Cash freimachen), dann Käufe mit Cash-Deckelung (kein Hebel).
  * Wird nur aktiv, wenn Setting SYSTEMATIC_LIVE = An ODER per CLI angestoßen.

CLI:
    python -m src.research.live --dry         # nur Orders anzeigen (keine Ausführung)
    python -m src.research.live --rebalance    # Konto aufs Ziel umschichten
    python -m src.research.live --liquidate    # alles glattstellen (zu Cash)
"""
import argparse
import logging

from ..executor import IBKRExecutor
from ..db import get_connection
from ..settings import load_settings
from ..markets import is_open
from ..signal_engine import (fetch_prices, fetch_fx, _account_equity_and_cash)
from .shadow import _load_wide, compute_target, TOP_Q

# Referenz-US-Symbol für die "Börse offen?"-Prüfung (Universum ist US).
_US_REF = "NVDA"

log = logging.getLogger("live")

REBAL_CLIENT_ID = 8        # eigener clientId für die CLI (≠ Engine=2/Executor=1)


def compute_orders(positions: dict, target: list, regime_on: bool,
                   net_liq_eur: float, prices_usd: dict, fx_usd: float,
                   buffer: float = 0.98) -> list:
    """REINE Sizing-Logik (ohne IB). Gibt [(symbol, 'BUY'/'SELL', qty)] zurück.
      * positions : {symbol: gehaltene Stückzahl}
      * target    : Ziel-Symbole (gleichgewichtet)
      * regime_on : False → alles in Cash (nur Verkäufe)
      * net_liq_eur: Kontowert in EUR (Basis für die Zielgewichte)
      * prices_usd: {symbol: aktueller Kurs in USD}
      * fx_usd    : USD je 1 EUR (EURUSD)
    """
    exposure = 1.0 if regime_on else 0.0
    tset = set(target)
    n = len(target)
    target_val_eur = (net_liq_eur * buffer * exposure / n) if (n and exposure) else 0.0

    orders = []
    # 1) Glattstellen: bei Risk-Off ALLES; sonst alles, was nicht (mehr) zum Ziel
    #    gehört. Longs verkaufen, Shorts zurückkaufen (kein Leerverkauf-Rest).
    for sym, qty in positions.items():
        q = int(qty)
        if q == 0:
            continue
        if exposure == 0 or sym not in tset:
            if q > 0:
                orders.append((sym, "SELL", q))
            else:
                orders.append((sym, "BUY", -q))     # Short schließen
    # 2) Ziel-Werte auf Sollgröße bringen (Delta kaufen/verkaufen)
    if exposure and fx_usd:
        for sym in target:
            price = prices_usd.get(sym)
            if not price or price <= 0:
                continue
            desired = int((target_val_eur * fx_usd) / price)
            cur = int(positions.get(sym, 0))
            delta = desired - cur
            if delta > 0:
                orders.append((sym, "BUY", delta))
            elif delta < 0:
                orders.append((sym, "SELL", -delta))
    return orders


def _sync_positions(executor: IBKRExecutor):
    """Erzwingt eine frische Positions-Synchronisation. Bei einer NEU
    aufgebauten IB-Verbindung ist ib.positions() sonst kurz leer → sonst
    liest man fälschlich '0 Positionen'."""
    try:
        executor.ib.reqPositions()
        executor.ib.sleep(4)
    except Exception as e:
        log.warning("reqPositions fehlgeschlagen: %s", e)


def _eurusd_from_ib(executor: IBKRExecutor):
    """EURUSD direkt von IB holen (Fallback, falls yfinance-FX leer ist).
    IB kennt den Kurs immer – verhindert, dass ein yfinance-Aussetzer das
    Rebalancing blockiert."""
    try:
        from ib_async import Forex
        c = Forex("EURUSD")
        executor.ib.qualifyContracts(c)
        try:
            executor.ib.reqMarketDataType(4)   # verzögerte Daten genügen
        except Exception:
            pass
        tk = executor.ib.reqTickers(c)
        if tk:
            for px in (tk[0].marketPrice(), tk[0].last, tk[0].close):
                if px and px == px and px > 0:
                    return float(px)
    except Exception as e:
        log.warning("IB-EURUSD-Fallback fehlgeschlagen: %s", e)
    return None


def _target_now(conn):
    """Frisches Ziel (day, regime_on, picks) aus den aktuellsten Kursen."""
    wide = _load_wide(conn)
    if wide.empty or len(wide) < 260:
        return None
    day, regime_on, picks, _ = compute_target(wide, TOP_Q)
    return day, regime_on, picks


def rebalance(executor: IBKRExecutor, conn, cfg: dict,
              log_only: bool = False, require_open: bool = True) -> list:
    """Schichtet das Konto auf das aktuelle Ziel um. Gibt die (geplanten)
    Orders zurück. `log_only=True` führt NICHTS aus (Trockenlauf).
    `require_open=True`: bei geschlossener US-Börse wird NICHT ausgeführt
    (verhindert das Einreihen von Market-Orders → Doppelfills/Shorts)."""
    tgt = _target_now(conn)
    if not tgt:
        log.warning("Kein Ziel berechenbar (zu wenig Kurshistorie).")
        return []
    _, regime_on, picks = tgt
    net_liq, cash = _account_equity_and_cash(executor)
    if net_liq <= 0:
        log.warning("Kontowert 0 – Rebalance abgebrochen.")
        return []
    _sync_positions(executor)
    positions = executor.get_positions()
    # Kurse für Ziel-Werte UND alle gehaltenen Werte (sonst fehlt z.B. der Kurs
    # zum Decken eines Nicht-Ziel-Shorts → Deck-Kauf würde übersprungen).
    need_px = set(picks) | {s for s, q in positions.items() if q}
    prices = fetch_prices(sorted(need_px))
    prices_last = {s: float(v.iloc[-1]) for s, v in prices.items() if v is not None and len(v)}
    fx_usd = fetch_fx(["USD"]).get("USD") or _eurusd_from_ib(executor)
    if not fx_usd:
        log.warning("Kein EURUSD-Kurs (yfinance + IB) – Rebalance abgebrochen.")
        return []

    orders = compute_orders(positions, picks, regime_on, net_liq,
                            prices_last, fx_usd)
    log.info("Ziel: %s | Regime %s | %d Werte | Kontowert %.0f € | geplante Orders: %d",
             tgt[0], "RISK-ON" if regime_on else "RISK-OFF (Cash)",
             len(picks), net_liq, len(orders))
    for sym, act, qty in orders:
        log.info("   %-5s %-4s %d", sym, act, qty)
    if log_only or not orders:
        return orders
    if require_open and not is_open(_US_REF):
        log.warning("US-Börse geschlossen – Orders werden NICHT platziert "
                    "(nur Vorschau). Ausführung erst zur Handelszeit.")
        return orders

    # Ausführung: erst Verkäufe (Cash frei), dann Käufe mit Cash-Deckel.
    sells = [o for o in orders if o[1] == "SELL"]
    buys = [o for o in orders if o[1] == "BUY"]
    # Short-Deckungen (Nicht-Ziel-Käufe) zuerst – müssen IMMER durchgehen.
    _pset = set(picks)
    buys.sort(key=lambda o: 0 if o[0] not in _pset else 1)
    for sym, _a, qty in sells:
        try:
            executor.place_market_order(sym, "SELL", qty, currency="USD")
            executor.ib.sleep(0.8)
        except Exception as e:
            log.error("   Verkauf %s fehlgeschlagen: %s", sym, e)
    executor.ib.sleep(3)                      # Fills setzen lassen
    _net, cash = _account_equity_and_cash(executor)
    avail_usd = max(0.0, cash) * fx_usd       # verfügbares Cash in USD
    for sym, _a, qty in buys:
        price = prices_last.get(sym)
        if not price:
            continue
        if qty * price > avail_usd:            # nie über das Cash hinaus (kein Hebel)
            qty = int(avail_usd / price)
        if qty < 1:
            log.info("   %s: Cash erschöpft – übersprungen", sym)
            continue
        try:
            executor.place_market_order(sym, "BUY", qty, currency="USD")
            avail_usd -= qty * price
            executor.ib.sleep(0.8)
        except Exception as e:
            log.error("   Kauf %s fehlgeschlagen: %s", sym, e)
    log.info("Rebalance ausgeführt.")
    return orders


def liquidate_all(executor: IBKRExecutor, require_open: bool = True) -> int:
    """Stellt das Konto glatt: alle offenen Orders stornieren, alle Positionen
    per Markt schließen (long & short). Gibt die Zahl geschlossener Positionen
    zurück. Bei geschlossener Börse (require_open) werden Order-Storni
    durchgeführt, aber KEINE Markt-Orders platziert."""
    if require_open and not is_open(_US_REF):
        log.warning("US-Börse geschlossen – Glattstellen abgebrochen "
                    "(keine Market-Orders zur Unzeit).")
        return 0
    _sync_positions(executor)
    try:
        executor.ib.reqGlobalCancel()
        executor.ib.sleep(2)
    except Exception as e:
        log.error("reqGlobalCancel fehlgeschlagen: %s", e)
    n = 0
    for p in list(executor.ib.positions()):
        qty = int(p.position)
        if qty == 0:
            continue
        sym = p.contract.symbol
        cur = p.contract.currency or "USD"
        try:
            if qty > 0:
                executor.place_market_order(sym, "SELL", qty, currency=cur)
            else:
                executor.place_market_order(sym, "BUY", -qty, currency=cur)
            n += 1
            executor.ib.sleep(0.8)
        except Exception as e:
            log.error("   Glattstellen %s fehlgeschlagen: %s", sym, e)
    log.info("Glattgestellt: %d Positionen → Cash.", n)
    return n


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description="Live-Rebalancer (systematischer Kern)")
    ap.add_argument("--dry", action="store_true", help="Nur Orders anzeigen")
    ap.add_argument("--rebalance", action="store_true", help="Konto aufs Ziel umschichten")
    ap.add_argument("--liquidate", action="store_true", help="Alles glattstellen (Cash)")
    ap.add_argument("--force", action="store_true",
                    help="Markt-offen-Schutz umgehen (nur bewusst nutzen)")
    args = ap.parse_args()
    req_open = not args.force
    conn = get_connection()
    cfg = load_settings(conn)
    ex = IBKRExecutor()
    ex.connect(client_id=REBAL_CLIENT_ID)
    try:
        if args.liquidate:
            liquidate_all(ex, require_open=req_open)
        if args.dry:
            rebalance(ex, conn, cfg, log_only=True)
        if args.rebalance:
            rebalance(ex, conn, cfg, log_only=False, require_open=req_open)
        if not (args.liquidate or args.dry or args.rebalance):
            rebalance(ex, conn, cfg, log_only=True)
    finally:
        ex.disconnect()


if __name__ == "__main__":
    main()
