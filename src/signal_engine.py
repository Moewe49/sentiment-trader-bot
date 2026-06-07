"""src/signal_engine.py
Signal-Engine: kombiniert Sentiment-Scores mit technischen Indikatoren und
erzeugt daraus konkrete Trade-Signale für den Executor.

Universum: feste Kernliste (src/universe.py CORE_TICKERS) PLUS alle Ticker, zu
denen es zuletzt News gab ("beides kombiniert").

Takt: läuft stündlich während der US-Börsenzeit (Mo–Fr, 9:30–16:00 New Yorker
Zeit). Außerhalb der Börsenzeit passiert nichts.

Strategie pro Wert: Sentiment + Trend-Following (SMA20) + RSI(14)
  - Kombinierter Score > +0.30  →  KAUFEN
  - Kombinierter Score < -0.30  →  VERKAUFEN
  - Sonst                       →  HALTEN / NICHTS TUN
"""
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from .config import config
from .db import (get_connection, ensure_signals_table, insert_signal,
                 ensure_signals_outcome_columns, ensure_news_tickers_column,
                 fetch_recent_tickers, get_ticker_sentiment,
                 ensure_watchlist_table, fetch_watchlist,
                 ensure_trim_table, get_trim_state, set_trim_state,
                 clear_trim_state)
from .executor import IBKRExecutor
from .fundamentals import get_fundamentals, FIELDS as FUND_FIELDS
from .indicators import rsi, technical_score_series
from .markets import any_market_open, is_open, ib_symbol, currency_of, yf_symbol_for
from .settings import load_settings
from .universe import CORE_TICKERS

log = logging.getLogger("signal")

# --- Signal-Schwellwerte --------------------------------------------------
BUY_THRESHOLD  =  0.30   # kombinierter Score > +0.30 → KAUFEN
SELL_THRESHOLD = -0.30   # kombinierter Score < -0.30 → VERKAUFEN
POSITION_SIZE  =  0.08   # max 8% des Portfolios pro Position

# Referenz-Zeitzone nur noch für das Tages-Verlustlimit (Tagesgrenze).
NY = ZoneInfo("America/New_York")


def fetch_fx(currencies) -> dict:
    """Wechselkurse 'Einheiten je 1 EUR' für die gegebenen Währungen
    (z.B. EURUSD=X, EURSEK=X). EUR selbst = 1.0. Für die Positionsgrößen-
    Umrechnung von EUR-Budget in die jeweilige Aktienwährung."""
    rates = {"EUR": 1.0}
    need = sorted({c for c in currencies if c and c != "EUR"})
    if not need:
        return rates
    pairs = [f"EUR{c}=X" for c in need]
    try:
        data = yf.download(pairs, period="5d", interval="1d",
                           group_by="ticker", progress=False, threads=False)
    except Exception as e:
        log.warning("FX-Abruf fehlgeschlagen: %s", e)
        return rates
    for c, pair in zip(need, pairs):
        try:
            s = (data["Close"] if len(pairs) == 1 else data[pair]["Close"]).dropna()
            if len(s):
                rates[c] = float(s.iloc[-1])
        except Exception:
            pass
    return rates


# -------------------------------------------------------------------------
# Welche Werte werden bewertet?
# -------------------------------------------------------------------------
def build_universe(conn) -> list[str]:
    """Kernliste vereinigt mit (a) Tickern, zu denen es zuletzt News gab, und
    (b) den im Dashboard manuell hinzugefügten Werten (Watchlist)."""
    recent = set(fetch_recent_tickers(conn, hours=24))
    try:
        manual = set(fetch_watchlist(conn))
    except Exception as e:
        log.warning("Watchlist nicht lesbar: %s", e)
        manual = set()
    extra = sorted((recent | manual) - set(CORE_TICKERS))
    return list(CORE_TICKERS) + extra


# -------------------------------------------------------------------------
# Technische Indikatoren via yfinance
# -------------------------------------------------------------------------
# Letzter erfolgreich geladener Kurs je Ticker – überbrückt kurzzeitige
# yfinance-Aussetzer (dann wird mit dem letzten bekannten Stand weitergerechnet).
_price_cache: dict = {}


def fetch_prices(tickers: list[str]) -> dict:
    """Holt die Kurshistorie ALLER Ticker in EINEM Sammel-Request (statt 15+
    Einzelabrufe → vermeidet 'Too Many Requests'). Mit Retry/Backoff und
    Cache-Fallback. Gibt {ticker: Close-Serie} zurück."""
    if not tickers:
        return {}

    data = None
    for attempt in range(3):
        try:
            data = yf.download(tickers, period="60d", interval="1d",
                               group_by="ticker", auto_adjust=True,
                               progress=False, threads=False)
            if data is not None and not data.empty:
                break
        except Exception as e:
            log.warning("Kursabruf Versuch %d fehlgeschlagen: %s", attempt + 1, e)
        if attempt < 2:
            time.sleep(5 * (attempt + 1))   # 5s, dann 10s warten

    out: dict = {}
    if data is not None and not data.empty:
        if len(tickers) == 1:   # bei einem Ticker ist das DataFrame flach
            t = tickers[0]
            if "Close" in data:
                s = data["Close"].dropna()
                if len(s):
                    out[t] = s
        else:
            for t in tickers:
                try:
                    sub = data[t]
                    if not sub.empty and "Close" in sub:
                        s = sub["Close"].dropna()
                        if len(s):
                            out[t] = s
                except Exception:
                    pass

    # Cache pflegen + fehlende Ticker aus dem letzten bekannten Stand füllen
    for t in tickers:
        if t in out:
            _price_cache[t] = out[t]
        elif t in _price_cache:
            out[t] = _price_cache[t]

    return out


def get_technical_score(ticker: str, closes, sma_band: float | None = None) -> float | None:
    """Technischer Score -1..+1 aus Kurs vs. 20-Tage-SMA und RSI(14).
    `closes` ist die bereits geladene Schlusskurs-Serie (aus fetch_prices).
    `sma_band` überschreibt die Pufferzone (Hysterese), sonst Default."""
    try:
        if closes is None or len(closes) < 20:
            log.warning("  Nicht genug Kursdaten für %s", ticker)
            return None

        if sma_band is None:
            score = technical_score_series(closes).iloc[-1]
        else:
            score = technical_score_series(closes, sma_band=sma_band).iloc[-1]
        if pd.isna(score):
            log.warning("  Nicht genug Kursdaten für %s", ticker)
            return None

        sma20 = closes.rolling(20).mean().iloc[-1]
        price = closes.iloc[-1]
        rsi_now = rsi(closes).iloc[-1]
        log.info("  Technik  %s: %.3f  (Kurs %.2f / SMA %.2f / RSI %.1f)",
                 ticker, float(score), float(price), float(sma20), float(rsi_now))
        return float(score)

    except Exception as e:
        log.warning("  Fehler bei Kursdaten für %s: %s", ticker, e)
        return None


def compute_features(closes) -> dict:
    """Rohmerkmale zum Signalzeitpunkt fürs spätere ML-Training."""
    feats = {"rsi": None, "sma_dist": None, "volatility": None}
    try:
        if closes is None or len(closes) < 20:
            return feats
        price = float(closes.iloc[-1])
        sma = float(closes.rolling(20).mean().iloc[-1])
        if sma:
            feats["sma_dist"] = round((price - sma) / sma, 5)
        feats["rsi"] = round(float(rsi(closes).iloc[-1]), 2)
        ret = closes.pct_change().dropna()
        if len(ret) >= 20:
            feats["volatility"] = round(float(ret.tail(20).std()), 5)
    except Exception:
        pass
    return feats


# -------------------------------------------------------------------------
# Signal berechnen
# -------------------------------------------------------------------------
def compute_signal(symbol: str, sentiment: float | None,
                   technical: float | None,
                   buy_threshold: float = BUY_THRESHOLD,
                   sell_threshold: float = SELL_THRESHOLD) -> dict:
    """Kombiniert Sentiment und Technik zu einem Signal für einen Wert."""
    if sentiment is None and technical is None:
        return {"etf": symbol, "action": "HOLD", "score": 0.0,
                "sentiment": sentiment, "technical": technical,
                "reason": "Keine Daten"}

    # Gewichtung: 50% Sentiment, 50% Technik (fällt eine weg → 100% die andere)
    if sentiment is None:
        score = technical
        reason = "Nur Technik (kein Sentiment)"
    elif technical is None:
        score = sentiment
        reason = "Nur Sentiment (keine Kursdaten)"
    else:
        score = sentiment * 0.5 + technical * 0.5
        reason = f"Sentiment {sentiment:+.3f} + Technik {technical:+.3f}"

    if score > buy_threshold:
        action = "BUY"
    elif score < sell_threshold:
        action = "SELL"
    else:
        action = "HOLD"

    return {"etf": symbol, "action": action, "score": round(score, 4),
            "sentiment": sentiment, "technical": technical,
            "reason": reason}


# -------------------------------------------------------------------------
# Risk-Management
# -------------------------------------------------------------------------
# Tages-Basiswert fürs Verlustlimit (wird je US-Handelstag zurückgesetzt).
_day_baseline: dict = {"date": None, "equity": None, "halted": False}


def _account_equity_and_cash(executor: IBKRExecutor) -> tuple[float, float]:
    """(Kontowert NetLiquidation, ECHTES Bargeld TotalCashValue).
    Bewusst TotalCashValue statt AvailableFunds (= Buying Power inkl. Margin) –
    so investiert der Bot nur eigenes Geld, ohne Hebel/Kredit."""
    net = cash = 0.0
    try:
        for i in executor.ib.accountSummary():
            if i.currency not in ("", "EUR", "BASE"):
                continue                      # nur Basiswährung des Kontos
            if i.tag == "NetLiquidation":
                net = float(i.value)
            elif i.tag == "TotalCashValue":
                cash = float(i.value)
    except Exception as e:
        log.warning("Kontostand nicht lesbar: %s", e)
    return net, cash


def _risk_gate(equity: float, now_ny: datetime, s: dict) -> tuple[bool, str]:
    """Kill-Switch + Tages-Verlustlimit. Gibt (handeln_erlaubt, Grund)."""
    if not s["TRADING_ENABLED"]:
        return False, "Handel deaktiviert (Kill-Switch im Dashboard/.env)"

    today = now_ny.date()
    if _day_baseline["date"] != today:        # neuer Handelstag → Basis zurücksetzen
        _day_baseline.update(date=today,
                             equity=(equity if equity > 0 else None),
                             halted=False)

    if _day_baseline["halted"]:
        return False, "Tages-Verlustlimit bereits erreicht"

    if _day_baseline["equity"] is None and equity > 0:
        _day_baseline["equity"] = equity      # Basiswert nachziehen

    base = _day_baseline["equity"]
    max_daily_loss = s["MAX_DAILY_LOSS"]
    if max_daily_loss and base and equity > 0:
        loss = (base - equity) / base
        if loss >= max_daily_loss:
            _day_baseline["halted"] = True
            return False, (f"Tagesverlust {loss*100:.1f}% ≥ Limit "
                           f"{max_daily_loss*100:.0f}% – Handel gestoppt")
    return True, ""


# -------------------------------------------------------------------------
# Orders ausführen
# -------------------------------------------------------------------------
def execute_signals(signals: list[dict], executor: IBKRExecutor, s: dict):
    """Setzt BUY/SELL-Signale um – positions-bewusst und mit Risk-Limits
    (alle Grenzen kommen aus den live einstellbaren Settings `s`)."""
    now_ny = datetime.now(NY)
    equity, cash = _account_equity_and_cash(executor)
    log.info("Kontowert: %.2f | Cash (ohne Hebel): %.2f", equity, cash)

    allowed, reason = _risk_gate(equity, now_ny, s)
    if not allowed:
        log.warning("Handel ausgesetzt: %s", reason)
        for sig in signals:
            sig["quantity"] = 0
            sig["executed"] = False
        return

    try:
        positions = executor.get_positions()
    except Exception as e:
        log.error("Positionen nicht lesbar: %s", e)
        positions = {}
    # Symbole mit bereits offener (noch nicht gefüllter) Kauf-Order – verhindert
    # Doppelkäufe, wenn eine Order aus einem früheren Lauf noch hängt.
    try:
        pending_buys = executor.get_open_buy_symbols()
    except Exception as e:
        log.error("Offene Orders nicht lesbar: %s", e)
        pending_buys = set()
    open_count = sum(1 for q in positions.values() if q != 0)

    # Wechselkurse für alle vorkommenden Aktienwährungen (EUR-Budget → Stückzahl)
    fx = fetch_fx(currency_of(sig["etf"]) for sig in signals
                  if sig["action"] != "HOLD")

    for sig in signals:
        symbol = sig["etf"]              # yfinance-Symbol (mit evtl. Börsen-Suffix)
        action = sig["action"]
        ibsym = ib_symbol(symbol)        # so kennt IB den Wert (ohne Suffix)
        cur = currency_of(symbol)
        held = positions.get(ibsym, 0.0)
        sig["quantity"] = 0
        sig["executed"] = False

        if action == "HOLD":
            continue

        # Nur handeln, wenn die zugehörige Börse gerade offen ist
        if not is_open(symbol):
            log.info("  %s: Börse geschlossen – kein Handel jetzt", symbol)
            continue

        if action == "BUY":
            if held > 0:
                log.info("  %s: bereits investiert (%g) – kein Nachkauf", symbol, held)
                continue
            if ibsym in pending_buys:
                log.info("  %s: Kauf-Order läuft bereits – kein Doppelkauf", symbol)
                continue
            if open_count >= s["MAX_OPEN_POSITIONS"]:
                log.info("  %s: Exposure-Limit (%d offene Positionen) erreicht",
                         symbol, s["MAX_OPEN_POSITIONS"])
                continue
            # Optionaler Fundamental-/Bewertungs-Filter
            if s["FUND_FILTER"]:
                rg, vg = sig.get("rev_growth"), sig.get("value_growth")
                if rg is not None and rg < s["MIN_REV_GROWTH"]:
                    log.info("  %s: Umsatzwachstum %.0f%% < Minimum – kein Kauf",
                             symbol, rg * 100)
                    continue
                if s["MAX_VALUE_GROWTH"] and vg is not None and vg > s["MAX_VALUE_GROWTH"]:
                    log.info("  %s: Value/Growth %.2f über Limit – zu teuer, kein Kauf",
                             symbol, vg)
                    continue
            # Optionaler Earnings-Schutz (keine neue Position kurz vor Earnings)
            guard = s["EARNINGS_GUARD_DAYS"]
            ed = sig.get("_earnings_in_days")
            if guard and ed is not None and 0 <= ed <= guard:
                log.info("  %s: Earnings in %d Tagen – kein Kauf (Event-Risiko)",
                         symbol, ed)
                continue
            # Entry-Filter: keine überkauften / überdehnten Werte kaufen
            rsi_v = sig.get("rsi")
            if rsi_v is not None and s["MAX_RSI_BUY"] and rsi_v > s["MAX_RSI_BUY"]:
                log.info("  %s: RSI %.0f > %.0f – überkauft, kein Kauf",
                         symbol, rsi_v, s["MAX_RSI_BUY"])
                continue
            dist = sig.get("sma_dist")
            if dist is not None and s["MAX_SMA_DIST"] and dist > s["MAX_SMA_DIST"]:
                log.info("  %s: %.0f%% über SMA > Limit – überdehnt, kein Kauf",
                         symbol, dist * 100)
                continue
            price = sig.get("price")     # in Aktienwährung
            rate = fx.get(cur)           # Einheiten der Währung je 1 EUR
            if price is None or cash <= 0 or not rate:
                log.warning("  %s: kein Kurs/Kapital/Wechselkurs – übersprungen", symbol)
                continue
            # EUR-Budget in Aktienwährung umrechnen: Stück = Budget_EUR * rate / Kurs
            quantity = max(1, int(cash * s["POSITION_SIZE"] * rate / price))
            sig["quantity"] = quantity
            # Volatilitätsbasierter Stop: weiter bei volatilen Werten (gegen
            # Wick-outs), mindestens STOP_LOSS_PCT, gedeckelt bei 25 %.
            stop_pct = s["STOP_LOSS_PCT"]
            vol = sig.get("volatility")
            if s["STOP_VOL_MULT"] and vol:
                stop_pct = min(0.25, max(stop_pct, s["STOP_VOL_MULT"] * vol))
            try:
                executor.place_bracket_order(ibsym, quantity, price,
                                             stop_pct,
                                             s["TAKE_PROFIT_PCT"], currency=cur)
                sig["executed"] = True
                open_count += 1
                log.info("  %s → BUY | %d Stück @ ~%.2f %s | Score %+.3f",
                         symbol, quantity, price, cur, sig["score"])
            except Exception as e:
                log.error("  Kauf fehlgeschlagen für %s: %s", symbol, e)

        elif action == "SELL":
            if held <= 0:
                log.info("  %s: keine Position – SELL ignoriert (kein Leerverkauf)",
                         symbol)
                continue
            # Buy & Hold: nicht bei kurzem Negativ-Score am Markt rauswerfen.
            # Der (breite, vola-basierte) Trailing-Stop übernimmt den Dip-Exit;
            # so wird nicht ins Tief verkauft (ARM/DELL-Problem).
            if s.get("HOLD_THROUGH_DIPS"):
                log.info("  %s: Negativ-Score, aber 'Durch Dips halten' aktiv – "
                         "Trailing-Stop schützt, kein Verkauf", symbol)
                continue
            quantity = int(held)
            sig["quantity"] = quantity
            try:
                executor.cancel_orders_for(ibsym)   # Bracket-Klammer lösen
                executor.place_market_order(ibsym, "SELL", quantity, currency=cur)
                sig["executed"] = True
                open_count -= 1
                log.info("  %s → SELL (Position schließen) | %d Stück | Score %+.3f",
                         symbol, quantity, sig["score"])
            except Exception as e:
                log.error("  Verkauf fehlgeschlagen für %s: %s", symbol, e)


# -------------------------------------------------------------------------
# Gewinne stufenweise mitnehmen ("Trimmen")
# -------------------------------------------------------------------------
# Gewinn-Stufen, bei denen je TRIM_FRACTION der Basis-Stückzahl verkauft wird.
TRIM_LEVELS = [0.25, 0.50, 1.00]


def reprotect_all(conn, executor: IBKRExecutor, s: dict):
    """Beim Start: ALLE offenen Schutz-Orders global stornieren und jede
    gehaltene Position frisch unter der Engine-Verbindung (clientId=2)
    absichern. So gehören alle Stops der Engine → spätere Stornos (beim
    Trimmen) greifen zuverlässig und es entstehen keine doppelten Stops
    aus früheren Wartungsläufen (Oversell-Schutz)."""
    try:
        poss = [p for p in executor.ib.positions() if p.position > 0]
    except Exception as e:
        log.error("Start-Re-Protect: Positionen nicht lesbar: %s", e)
        return
    if not poss:
        return
    log.info("Start: %d Positionen werden neu abgesichert (Engine-Ownership) …",
             len(poss))
    try:
        executor.ib.reqGlobalCancel()
        executor.ib.sleep(4)
    except Exception as e:
        log.error("  reqGlobalCancel fehlgeschlagen: %s", e)
    yfs = {p.contract.symbol: yf_symbol_for(p.contract.symbol,
                                            p.contract.currency or "USD")
           for p in poss}
    prices = fetch_prices(sorted(set(yfs.values())))
    for p in poss:
        ibsym = p.contract.symbol
        cur = p.contract.currency or "USD"
        qty = int(p.position)
        avg = float(p.avgCost or 0)
        if qty <= 0 or avg <= 0:
            continue
        closes = prices.get(yfs[ibsym])
        stop_pct = s["STOP_LOSS_PCT"]
        vol = compute_features(closes).get("volatility") if closes is not None else None
        if s["STOP_VOL_MULT"] and vol:
            stop_pct = min(0.25, max(stop_pct, s["STOP_VOL_MULT"] * vol))
        try:
            executor.protect_position(ibsym, qty, stop_pct,
                                      s["TAKE_PROFIT_PCT"], avg, currency=cur)
            executor.ib.sleep(0.4)
        except Exception as e:
            log.error("  Re-Protect %s fehlgeschlagen: %s", ibsym, e)


def manage_positions(conn, executor: IBKRExecutor, s: dict, prices: dict):
    """Buy-&-Hold mit Gewinnmitnahme: erreicht eine Position +25/+50/+100 %,
    wird je ein Anteil (TRIM_FRACTION) der ursprünglichen Stückzahl verkauft
    und der Trailing-Stop auf die Restmenge neu gesetzt. Pro Lauf max. eine
    Stufe je Wert (sanftes Heraustreten)."""
    if not s.get("TRIM_ENABLED"):
        return
    frac = s["TRIM_FRACTION"]
    if not frac:
        return
    try:
        pos_objs = [p for p in executor.ib.positions() if p.position > 0]
    except Exception as e:
        log.error("Trim: Positionen nicht lesbar: %s", e)
        return
    ensure_trim_table(conn)

    for p in pos_objs:
        ibsym = p.contract.symbol
        cur = p.contract.currency or "USD"
        held = float(p.position)
        avg = float(p.avgCost or 0)          # Ø-Kosten je Stück (Aktienwährung)
        if held <= 0 or avg <= 0:
            continue
        yfs = yf_symbol_for(ibsym, cur)
        closes = prices.get(yfs)
        if closes is None or not len(closes):
            continue
        price = float(closes.iloc[-1])
        gain = (price - avg) / avg

        base_qty, done = get_trim_state(conn, ibsym)
        if base_qty is None:                  # erster Kontakt → Basis merken
            base_qty = held
            set_trim_state(conn, ibsym, base_qty, done)

        pending = [lvl for lvl in TRIM_LEVELS if lvl not in done]
        if not pending or gain < pending[0]:
            continue
        if not is_open(yfs):                  # nur verkaufen, wenn Börse offen
            continue

        lvl = pending[0]
        qty = min(int(round(base_qty * frac)), int(held))
        if qty < 1:                           # zu klein zum Trimmen → Stufe abhaken
            done.add(lvl)
            set_trim_state(conn, ibsym, base_qty, done)
            continue
        try:
            executor.cancel_orders_for(ibsym)
            executor.ib.sleep(0.5)
            executor.place_market_order(ibsym, "SELL", qty, currency=cur)
            executor.ib.sleep(1.2)
            remaining = int(held) - qty
            done.add(lvl)
            set_trim_state(conn, ibsym, base_qty, done)
            log.info("  ✂️  %s: +%.0f%% erreicht → Trim %d Stück (Rest %d)",
                     ibsym, lvl * 100, qty, remaining)
            if remaining > 0:                 # Restmenge sofort neu absichern
                stop_pct = s["STOP_LOSS_PCT"]
                vol = compute_features(closes).get("volatility")
                if s["STOP_VOL_MULT"] and vol:
                    stop_pct = min(0.25, max(stop_pct, s["STOP_VOL_MULT"] * vol))
                executor.protect_position(ibsym, remaining, stop_pct,
                                          s["TAKE_PROFIT_PCT"], avg, currency=cur)
            else:
                clear_trim_state(conn, ibsym)
        except Exception as e:
            log.error("  Trim fehlgeschlagen für %s: %s", ibsym, e)


# -------------------------------------------------------------------------
# Ein kompletter Durchlauf
# -------------------------------------------------------------------------
def run_cycle(conn, executor: IBKRExecutor):
    """Bewertet das gesamte Universum und handelt actionable Signale."""
    cfg = load_settings(conn)        # live einstellbare Parameter (Dashboard ⚙)
    universe = build_universe(conn)
    log.info("=== Signal-Durchlauf startet (%d Werte) ===", len(universe))

    # Alle Kurse in EINEM Request laden (vermeidet yfinance-Rate-Limits).
    prices = fetch_prices(universe)
    log.info("Kursdaten für %d/%d Werten geladen.", len(prices), len(universe))
    signals = []

    for symbol in universe:
        sentiment, n = get_ticker_sentiment(conn, symbol)
        if sentiment is not None:
            log.info("--- %s ---  Sentiment %.3f (aus %d Headlines)",
                     symbol, sentiment, n)
        closes = prices.get(symbol)
        technical = get_technical_score(symbol, closes, sma_band=cfg["SMA_BAND"])
        sig = compute_signal(symbol, sentiment, technical,
                             cfg["BUY_THRESHOLD"], cfg["SELL_THRESHOLD"])
        # aktuellen Kurs für die Kauf-Stückzahl mitgeben (kein Extra-Abruf nötig)
        if closes is not None and len(closes):
            sig["price"] = float(closes.iloc[-1])
        # ML-Rohmerkmale anhängen (werden mitgespeichert)
        sig.update(compute_features(closes))
        sig["n_headlines"] = n
        # Fundamentaldaten (24h-gecacht) als Features + für Filter/Earnings-Schutz
        fund = get_fundamentals(symbol)
        for k in FUND_FIELDS:
            sig[k] = fund.get(k)
        sig["_earnings_in_days"] = fund.get("earnings_in_days")
        signals.append(sig)

    # Zusammenfassung nur für nicht-HOLD-Signale (sonst zu viel Rauschen)
    actionable = [s for s in signals if s["action"] != "HOLD"]
    log.info("=== Zusammenfassung: %d Trade-Signale ===", len(actionable))
    for sig in actionable:
        log.info("  %s: %s (%+.3f)  %s",
                 sig["etf"], sig["action"], sig["score"], sig["reason"])

    if cfg.get("SYSTEMATIC_LIVE"):
        # Systematischer Kern aktiv → Altlogik (Sentiment-Trades, Trim, Stops)
        # ist abgeschaltet; gehandelt wird ausschließlich beim Rebalancing.
        log.info("  SYSTEMATIC_LIVE aktiv – Altlogik aus (Handel nur beim Rebalancing).")
        # Einmal-Deploy: sobald die US-Börse offen ist, EINMAL sauber umschichten
        # (live → korrektes Cash-Sequencing). Danach Flag zurücksetzen.
        if cfg.get("SYSTEMATIC_DEPLOY") and is_open("NVDA"):
            try:
                from .research.live import rebalance as _deploy_rebalance
                from .db import set_settings
                log.info("  Initial-Deploy: US-Börse offen → Rebalance auf Ziel-Portfolio.")
                _deploy_rebalance(executor, conn, cfg)
                set_settings(conn, {"SYSTEMATIC_DEPLOY": "no"})
                log.info("  Initial-Deploy abgeschlossen – Flag zurückgesetzt.")
            except Exception as e:
                log.error("  Initial-Deploy fehlgeschlagen: %s", e)
    else:
        if actionable:
            execute_signals(signals, executor, cfg)
        else:
            log.info("  Kein Trade — alle Signale: HOLD")
        # Buy-&-Hold-Pflege: Gewinne stufenweise mitnehmen (unabhängig von Signalen)
        try:
            manage_positions(conn, executor, cfg, prices)
        except Exception as e:
            log.error("Positions-Pflege (Trim) fehlgeschlagen: %s", e)

    # Jedes Signal protokollieren (auch HOLD), für spätere Performance-Auswertung
    for sig in signals:
        try:
            insert_signal(conn, sig)
        except Exception as e:
            log.error("  Signal konnte nicht gespeichert werden (%s): %s",
                      sig.get("etf"), e)

    log.info("=== Durchlauf fertig ===")


def main():
    log.info("Signal-Engine startet. Takt im Dashboard ⚙ einstellbar (Default %d Min), "
             "läuft solange irgendeine Börse offen ist (handelt je Wert nur zu dessen Zeiten).",
             config.CYCLE_MINUTES)
    conn = get_connection()
    ensure_signals_table(conn)
    ensure_signals_outcome_columns(conn)
    ensure_news_tickers_column(conn)
    ensure_watchlist_table(conn)
    ensure_trim_table(conn)
    from .db import ensure_shadow_tables
    ensure_shadow_tables(conn)
    executor = IBKRExecutor()
    executor.connect(client_id=2)   # clientId=2: kein Konflikt mit Executor (clientId=1)

    # Alle bestehenden Positionen unter Engine-Ownership neu absichern,
    # damit spätere Stornos (Trim) greifen und keine Alt-Stops doppelt laufen.
    # Im systematischen Modus gibt es bewusst KEINE Einzel-Stops → übersprungen.
    try:
        _cfg0 = load_settings(conn)
        if not _cfg0.get("SYSTEMATIC_LIVE"):
            reprotect_all(conn, executor, _cfg0)
        else:
            log.info("SYSTEMATIC_LIVE aktiv – kein Start-Re-Protect (keine Einzel-Stops).")
    except Exception as e:
        log.error("Start-Re-Protect fehlgeschlagen: %s", e)

    # Optional: sofort einmal laufen (zum Testen), unabhängig von der Börsenzeit
    if os.getenv("RUN_ON_START", "").lower() in ("1", "true", "yes"):
        log.info("RUN_ON_START gesetzt → einmaliger Sofort-Durchlauf.")
        try:
            run_cycle(conn, executor)
        except Exception as e:
            log.error("Fehler im Sofort-Durchlauf: %s", e)

    last_slot = None                  # verhindert Doppellauf im selben Zeitfenster
    last_ingest_day = None            # täglicher Kurs-Backfill ins prices_daily
    while True:
        # Einmal pro Kalendertag den historischen Kursspeicher aktualisieren
        today = datetime.now(NY).date()
        if last_ingest_day != today:
            try:
                from .research.ingest import ingest as _ingest
                from .universe import RESEARCH_UNIVERSE as _RU, MACRO_TICKERS as _MT
                n = _ingest(conn, _RU + _MT, period="10d")
                log.info("prices_daily aktualisiert: %d Zeilen.", n)
            except Exception as e:
                log.warning("Kurs-Update (prices_daily) fehlgeschlagen: %s", e)
            # Shadow-Modus: systematischen Kern fortschreiben (ohne Handel).
            # Bei armiertem SYSTEMATIC_LIVE wird an Rebalancing-Tagen real gehandelt.
            try:
                from .research.shadow import update_shadow
                did_rebal = update_shadow(conn)
                cfg_live = load_settings(conn)
                if cfg_live.get("SYSTEMATIC_LIVE") and did_rebal:
                    from .research.live import rebalance as _rebalance
                    log.info("SYSTEMATIC_LIVE: Rebalancing-Tag – Konto wird umgeschichtet …")
                    _rebalance(executor, conn, cfg_live)
            except Exception as e:
                log.warning("Shadow/Live-Update fehlgeschlagen: %s", e)
            # Katalysator-Scan + Trading-Kalender (je 1 Gemini-Call/Tag, günstig)
            try:
                from .catalysts import scan as _catscan
                _catscan(conn)
            except Exception as e:
                log.warning("Katalysator-Scan fehlgeschlagen: %s", e)
            try:
                from .tradingcal import scan as _calscan
                _calscan(conn)
            except Exception as e:
                log.warning("Kalender-Scan fehlgeschlagen: %s", e)
            last_ingest_day = today
        # Takt live aus den Einstellungen (im Dashboard ⚙ änderbar)
        try:
            interval = max(1, int(load_settings(conn)["CYCLE_MINUTES"]))
        except Exception:
            interval = config.CYCLE_MINUTES
        now = datetime.now(NY)
        slot = (now.date(), now.hour, now.minute // interval)

        if any_market_open() and slot != last_slot:
            try:
                run_cycle(conn, executor)
            except Exception as e:
                log.error("Fehler im Durchlauf: %s", e)
            last_slot = slot

        time.sleep(30)


if __name__ == "__main__":
    main()
