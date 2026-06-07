"""src/evaluator.py
Bewertet im Nachhinein, ob die erzeugten Trade-Signale richtig lagen.

Warum: Der Bot speichert jedes Signal, lernt aber nur etwas, wenn er auch
weiß, was der Kurs danach gemacht hat. Dieser Worker hängt darum an jedes
Signal den tatsächlichen Forward-Return an (1 und 5 Börsentage später) und
markiert, ob das Signal "getroffen" hat (BUY → Kurs gestiegen, SELL → gefallen).

Das ergibt einen sauber gelabelten Datensatz – die Grundlage, um später
Schwellen zu tunen oder ein ML-Modell zu trainieren.

Auflösung: Tages-Schlusskurse von yfinance (kostenlos). Als Einstand gilt der
Schlusskurs am Signaltag; die Signal-Engine handelt nur während der US-
Börsenzeit, daher entspricht das UTC-Datum des Signals dem US-Handelstag.
"""
import logging
import time
from collections import defaultdict
from datetime import date, timedelta

from .db import (get_connection, ensure_signals_table,
                 ensure_signals_outcome_columns, fetch_unevaluated_signals,
                 update_signal_outcome, fetch_strategy_performance)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("evaluator")

EVAL_INTERVAL = 3600     # 1× pro Stunde reicht völlig
MIN_AGE_HOURS = 24       # erst bewerten, wenn ein 1-Tages-Return existieren kann
HORIZON_LONG = 5         # Börsentage für den "endgültigen" Return


def _download_closes(ticker: str, start: date, end: date):
    """Tages-Schlusskurse für einen Ticker (lazy yfinance-Import). None bei Fehler."""
    import yfinance as yf
    try:
        df = yf.download(ticker, start=start.isoformat(), end=end.isoformat(),
                         interval="1d", progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        return df["Close"].squeeze()
    except Exception as e:
        log.warning("  Kursabruf für %s fehlgeschlagen: %s", ticker, e)
        return None


def _compute_outcome(closes, sig: dict) -> dict | None:
    """Berechnet Einstand + Forward-Returns + hit für ein Signal.
    Gibt None zurück, wenn am Signaltag noch kein Kurs vorliegt."""
    sig_date = sig["ts"].date()
    dates = [d.date() if hasattr(d, "date") else d for d in closes.index]

    # Einstand = erster Handelstag am oder nach dem Signaltag
    entry_pos = next((i for i, d in enumerate(dates) if d >= sig_date), None)
    if entry_pos is None:
        return None

    entry = float(closes.iloc[entry_pos])
    if entry == 0:
        return None

    def _at(offset):
        pos = entry_pos + offset
        return float(closes.iloc[pos]) if pos < len(closes) else None

    price_1d = _at(1)
    price_5d = _at(HORIZON_LONG)
    return_1d = (price_1d - entry) / entry if price_1d is not None else None
    return_5d = (price_5d - entry) / entry if price_5d is not None else None

    # hit anhand des längsten verfügbaren Horizonts bewerten
    ref = return_5d if return_5d is not None else return_1d
    hit = None
    if ref is not None:
        if sig["action"] == "BUY":
            hit = ref > 0
        elif sig["action"] == "SELL":
            hit = ref < 0

    # endgültig, sobald der 5-Tages-Return feststeht
    evaluated = return_5d is not None

    return {
        "price_at_signal": round(entry, 4),
        "price_1d": round(price_1d, 4) if price_1d is not None else None,
        "price_5d": round(price_5d, 4) if price_5d is not None else None,
        "return_1d": round(return_1d, 6) if return_1d is not None else None,
        "return_5d": round(return_5d, 6) if return_5d is not None else None,
        "hit": hit,
        "evaluated": evaluated,
    }


def evaluate_pending(conn) -> int:
    """Bewertet alle fälligen Signale, gebündelt pro Ticker (spart Kursabrufe).
    Gibt die Anzahl aktualisierter Signale zurück."""
    rows = fetch_unevaluated_signals(conn, min_age_hours=MIN_AGE_HOURS)
    if not rows:
        return 0

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["etf"]].append(r)

    updated = 0
    today = date.today()
    for ticker, sigs in by_ticker.items():
        earliest = min(s["ts"].date() for s in sigs)
        closes = _download_closes(ticker, earliest - timedelta(days=5),
                                  today + timedelta(days=1))
        if closes is None or len(closes) == 0:
            continue
        for s in sigs:
            outcome = _compute_outcome(closes, s)
            if outcome is None:
                continue
            update_signal_outcome(conn, s["id"], **outcome)
            updated += 1

    return updated


def log_performance(conn):
    """Aktuelle Trefferquote/Returns ins Log schreiben (kurzer Überblick)."""
    perf = fetch_strategy_performance(conn)
    if not perf:
        log.info("Noch keine bewerteten Signale für eine Statistik.")
        return
    for p in perf:
        log.info("  %-4s  n=%d  Trefferquote=%.0f%%  Ø1T=%+.2f%%  Ø5T=%+.2f%%",
                 p["action"], p["n"],
                 (p["hit_rate"] or 0) * 100,
                 (p["avg_return_1d"] or 0) * 100,
                 (p["avg_return_5d"] or 0) * 100)


def main():
    log.info("Evaluator startet. Bewertet erzeugte Signale (Forward-Returns).")
    conn = get_connection()
    ensure_signals_table(conn)
    ensure_signals_outcome_columns(conn)
    while True:
        try:
            n = evaluate_pending(conn)
            if n:
                log.info("%d Signal(e) bewertet.", n)
                log_performance(conn)
        except Exception as e:
            log.error("Fehler bei der Bewertung: %s", e)
        time.sleep(EVAL_INTERVAL)


if __name__ == "__main__":
    main()
