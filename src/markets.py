"""src/markets.py
Multi-Börsen-Unterstützung.

Ein Wert wird im System als yfinance-Symbol geführt – mit Börsen-Suffix für
Nicht-US-Werte, z.B.:
  NVDA      -> US-Aktie (SMART, USD)
  SIVE.ST   -> Stockholm (SEK)
  SAP.DE    -> XETRA (EUR)
  SHEL.L    -> London (GBP)

Hier wird aus dem Suffix abgeleitet: Handelswährung, Zeitzone und Handelszeiten.
Für die IB-Order genügt SMART-Routing + die richtige Währung; IB findet damit
den passenden Kontrakt (per qualifyContracts im Executor).
"""
from collections import Counter, namedtuple
from datetime import datetime
from zoneinfo import ZoneInfo

Market = namedtuple("Market", "name currency tz open_min close_min")

# Suffix (wie bei yfinance) -> Markt. "" = USA (Default, wie bisher).
# open_min/close_min = Minuten seit Mitternacht in der Börsen-Zeitzone.
MARKETS: dict = {
    "":   Market("USA",        "USD", "America/New_York", 9 * 60 + 30, 16 * 60),
    "ST": Market("Stockholm",  "SEK", "Europe/Stockholm", 9 * 60,      17 * 60 + 30),
    "DE": Market("XETRA",      "EUR", "Europe/Berlin",    9 * 60,      17 * 60 + 30),
    "F":  Market("Frankfurt",  "EUR", "Europe/Berlin",    9 * 60,      17 * 60 + 30),
    "L":  Market("London",     "GBP", "Europe/London",    8 * 60,      16 * 60 + 30),
    "PA": Market("Paris",      "EUR", "Europe/Paris",     9 * 60,      17 * 60 + 30),
    "AS": Market("Amsterdam",  "EUR", "Europe/Amsterdam", 9 * 60,      17 * 60 + 30),
    "BR": Market("Brüssel",    "EUR", "Europe/Brussels",  9 * 60,      17 * 60 + 30),
    "MI": Market("Mailand",    "EUR", "Europe/Rome",      9 * 60,      17 * 60 + 30),
    "MC": Market("Madrid",     "EUR", "Europe/Madrid",    9 * 60,      17 * 60 + 30),
    "SW": Market("Zürich",     "CHF", "Europe/Zurich",    9 * 60,      17 * 60 + 30),
    "VI": Market("Wien",       "EUR", "Europe/Vienna",    9 * 60,      17 * 60 + 30),
    "OL": Market("Oslo",       "NOK", "Europe/Oslo",      9 * 60,      16 * 60 + 30),
    "CO": Market("Kopenhagen", "DKK", "Europe/Copenhagen", 9 * 60,     17 * 60),
    "HE": Market("Helsinki",   "EUR", "Europe/Helsinki",  10 * 60,     18 * 60 + 30),
    "LS": Market("Lissabon",   "EUR", "Europe/Lisbon",    8 * 60,      16 * 60 + 30),
    "TO": Market("Toronto",    "CAD", "America/Toronto",  9 * 60 + 30, 16 * 60),
    "HK": Market("Hongkong",   "HKD", "Asia/Hong_Kong",   9 * 60 + 30, 16 * 60),
    "T":  Market("Tokio",      "JPY", "Asia/Tokyo",       9 * 60,      15 * 60),
    "AX": Market("Sydney",     "AUD", "Australia/Sydney", 10 * 60,     16 * 60),
}

DEFAULT = MARKETS[""]


def split_symbol(sym: str) -> tuple[str, str]:
    """'SIVE.ST' -> ('SIVE', 'ST');  'NVDA' -> ('NVDA', '').
    Ein Punkt zählt nur dann als Börsen-Suffix, wenn er ein bekannter Markt ist
    (so bleibt z.B. 'BRK.B' eine US-Aktie)."""
    if "." in sym:
        base, suf = sym.rsplit(".", 1)
        if suf.upper() in MARKETS and suf != "":
            return base, suf.upper()
    return sym, ""


def market_of(sym: str) -> Market:
    """Markt-Infos für ein Symbol."""
    return MARKETS.get(split_symbol(sym)[1], DEFAULT)


def ib_symbol(sym: str) -> str:
    """Das Symbol ohne Börsen-Suffix (so, wie IB es kennt)."""
    return split_symbol(sym)[0]


def currency_of(sym: str) -> str:
    return market_of(sym).currency


def _market_open(m: Market) -> bool:
    local = datetime.now(ZoneInfo(m.tz))
    if local.weekday() >= 5:                 # Sa/So
        return False
    mins = local.hour * 60 + local.minute
    return m.open_min <= mins <= m.close_min


def is_open(sym: str) -> bool:
    """True, wenn die Börse dieses Symbols gerade regulär offen ist
    (Mo–Fr in der jeweiligen Lokalzeit; Feiertage werden ignoriert)."""
    return _market_open(market_of(sym))


def any_market_open() -> bool:
    """True, wenn irgendeine der unterstützten Börsen gerade offen ist."""
    return any(_market_open(m) for m in MARKETS.values())


# --- Rückwärts: aus IB-Symbol + Währung wieder das yfinance-Symbol bauen -----
# (fürs Dashboard, um Kurse gehaltener Auslandswerte bei yfinance zu holen)
_ccy_counts = Counter(m.currency for m in MARKETS.values())
# Währungen, die nur an EINER Börse vorkommen → eindeutiges Suffix
_CCY_UNIQUE = {m.currency: suf for suf, m in MARKETS.items()
               if suf and _ccy_counts[m.currency] == 1}
# EUR ist mehrdeutig → über die IB-Primärbörse auflösen
_EXCH_SUFFIX = {
    "IBIS": "DE", "IBIS2": "DE", "FWB": "F", "SBF": "PA", "AEB": "AS",
    "BVME": "MI", "BM": "MC", "WBAG": "VI", "ENEXT.BE": "BR", "LSE": "L",
    "SFB": "ST", "EBS": "SW", "TSE": "TO", "SEHK": "HK", "ASX": "AX",
}


def yf_symbol_for(ib_sym: str, currency: str | None = None,
                  primary_exchange: str = "") -> str:
    """Baut aus IB-Symbol + Währung (+ ggf. Primärbörse) wieder das
    yfinance-Symbol. US (USD) bleibt ohne Suffix; SIVE/SEK → SIVE.ST usw."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return ib_sym
    suf = _CCY_UNIQUE.get(cur) or _EXCH_SUFFIX.get((primary_exchange or "").upper())
    return f"{ib_sym}.{suf}" if suf else ib_sym
