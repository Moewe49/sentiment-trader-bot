"""src/fundamentals.py
Fundamentaldaten (kostenlos via yfinance) als zusätzliche Dimension neben
Sentiment + Technik.

Liefert pro Wert die gängigen Relativ-Bewertungskennzahlen und einen
Value/Growth-Score (P/S TTM ÷ Umsatzwachstum % – niedriger = mehr Wachstum pro
Bewertungs-Dollar, eine Art einfaches Umsatz-PEG). Außerdem die Tage bis zum
nächsten Earnings-Termin (für den Event-Schutz).

Fundamentaldaten ändern sich langsam → 24-Stunden-Cache, damit yfinance nicht
unnötig oft (und rate-limitiert) abgefragt wird.
"""
import logging
import time
from datetime import date

log = logging.getLogger("fundamentals")

_TTL = 24 * 3600
_cache: dict = {}        # ticker -> (timestamp, data)

# Welche Kennzahlen als ML-Features gespeichert werden:
FIELDS = ("ps_ttm", "ev_ebitda", "gross_margin", "rev_growth", "value_growth")


def _round(x, n):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def _fetch(ticker: str) -> dict:
    out = {k: None for k in FIELDS}
    out["earnings_in_days"] = None
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info or {}
        ps = info.get("priceToSalesTrailing12Months")
        rg = info.get("revenueGrowth")          # Anteil, z.B. 0.85 = +85%
        out["ps_ttm"] = _round(ps, 3)
        out["ev_ebitda"] = _round(info.get("enterpriseToEbitda"), 3)
        out["gross_margin"] = _round(info.get("grossMargins"), 4)
        out["rev_growth"] = _round(rg, 4)
        if ps and rg and rg > 0:
            out["value_growth"] = _round(float(ps) / (float(rg) * 100.0), 4)

        # Tage bis zum nächsten Earnings-Termin
        try:
            cal = t.calendar
            ed = None
            if isinstance(cal, dict):
                v = cal.get("Earnings Date")
                ed = v[0] if isinstance(v, (list, tuple)) and v else v
            if isinstance(ed, date):
                out["earnings_in_days"] = (ed - date.today()).days
        except Exception:
            pass
    except Exception as e:
        log.warning("Fundamentaldaten für %s nicht abrufbar: %s", ticker, e)
    return out


def get_fundamentals(ticker: str) -> dict:
    """Fundamentaldaten mit 24h-Cache. Gibt immer ein vollständiges Dict
    (fehlende Werte = None)."""
    now = time.time()
    hit = _cache.get(ticker)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    data = _fetch(ticker)
    _cache[ticker] = (now, data)
    return data
