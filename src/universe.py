"""src/universe.py
Handelbares Universum: einzelne Aktien + Sektor-ETFs.

Zwei Dinge werden hier definiert:
  1. SYMBOL_KEYWORDS – ordnet jedem Ticker Stichwörter zu, damit News automatisch
     dem richtigen Wert zugeordnet werden (z.B. "Nvidia steigt" -> NVDA).
  2. CORE_TICKERS    – die feste Kernliste, die IMMER bewertet wird, auch wenn
     gerade keine News zu einem Wert vorliegen.

Die Signal-Engine handelt die Vereinigung aus CORE_TICKERS und allen Tickern,
zu denen es zuletzt News gab ("beides kombiniert").

Neue Werte hinzufügen: einfach hier eintragen. Kleinschreibung genügt, der
Vergleich ist case-insensitive. Tipp: kurze Tokens mit Leerzeichen umschließen
(z.B. " aaoi "), damit sie nicht zufällig in anderen Wörtern matchen.
"""
from .sectors import SECTORS

# --- Einzelaktien: Ticker -> Stichwörter ---------------------------------
STOCK_KEYWORDS: dict[str, list[str]] = {
    # Tech & Halbleiter
    "NVDA": ["nvidia"],
    "AMD":  ["amd", "advanced micro devices"],
    "AAPL": ["apple"],
    "MSFT": ["microsoft"],
    "GOOGL": ["google", "alphabet"],
    "META": ["meta platforms", "facebook"],
    "AMZN": ["amazon"],
    "TSLA": ["tesla"],
    "INTC": ["intel"],
    "AVGO": ["broadcom"],
    "TSM":  ["tsmc", "taiwan semiconductor"],
    "MU":   ["micron"],
    "QCOM": ["qualcomm"],
    "ASML": ["asml"],
    "AAOI": ["applied optoelectronics", " aaoi "],
    # Energie
    "XOM":  ["exxon"],
    "CVX":  ["chevron"],
    # Finanzen
    "JPM":  ["jpmorgan", "jp morgan", "j.p. morgan"],
    "GS":   ["goldman sachs"],
    "MS":   ["morgan stanley"],
    "C":    ["citigroup"],
    "WFC":  ["wells fargo"],
    "BAC":  ["bank of america"],
    # Software / Internet / Consumer Tech
    "NFLX": ["netflix"],
    "DIS":  ["disney"],
    "ORCL": ["oracle"],
    "CRM":  ["salesforce"],
    "ADBE": ["adobe"],
    "CSCO": ["cisco"],
    "IBM":  ["ibm", "international business machines"],
    "PLTR": ["palantir"],
    "UBER": ["uber"],
    "ABNB": ["airbnb"],
    "SHOP": ["shopify"],
    "PYPL": ["paypal"],
    "COIN": ["coinbase"],
    "SOFI": ["sofi"],
    "BABA": ["alibaba"],
    # Halbleiter / Hardware
    "DELL": ["dell"],
    "HPQ":  ["hewlett-packard", " hp inc"],
    "ARM":  [" arm holdings", " arm ltd"],
    "SMCI": ["super micro", "supermicro"],
    "WDC":  ["western digital"],
    "STX":  ["seagate"],
    # Zahlungsverkehr / Konsum
    "V":    [" visa "],
    "MA":   ["mastercard"],
    "KO":   ["coca-cola", "coca cola"],
    "PEP":  ["pepsico", "pepsi"],
    "WMT":  ["walmart"],
    "COST": ["costco"],
    "MCD":  ["mcdonald"],
    "NKE":  [" nike "],
    "SBUX": ["starbucks"],
    # Auto / Industrie / Energie
    "BA":   ["boeing"],
    "F":    [" ford "],
    "GM":   ["general motors"],
    "RIVN": ["rivian"],
    # Gesundheit
    "PFE":  ["pfizer"],
    "MRNA": ["moderna"],
    "LLY":  ["eli lilly"],
    "JNJ":  ["johnson & johnson", "johnson and johnson"],
}

# --- Sektor-ETFs als handelbare Symbole (Stichwörter aus sectors.py) -----
ETF_KEYWORDS: dict[str, list[str]] = {
    etf: cfg["keywords"] for etf, cfg in SECTORS.items()
}

# Alle Symbole mit ihren Stichwörtern (Aktien + ETFs)
SYMBOL_KEYWORDS: dict[str, list[str]] = {**STOCK_KEYWORDS, **ETF_KEYWORDS}

# --- Feste Kernliste: wird immer bewertet --------------------------------
CORE_TICKERS: list[str] = [
    "NVDA", "AMD", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AAOI",
    "XLK", "XLE", "XLF",
]

# --- Research-Universum: feste, breite Liquiditäts-Liste für das ML-Ranking
# (alle definierten Einzelaktien). Bewusst getrennt von CORE_TICKERS, damit der
# Querschnitts-Backtest/Training auf einem stabilen, bekannten Set arbeitet.
RESEARCH_UNIVERSE: list[str] = sorted(STOCK_KEYWORDS.keys())

# --- Makro-/Cross-Asset-Proxies für die Korrelationsanalyse (liquide ETFs,
# die yfinance zuverlässig liefert). Label → Ticker.
MACRO: dict[str, str] = {
    "Gold": "GLD", "Silber": "SLV", "US-Dollar": "UUP", "US-Anleihen": "TLT",
}
MACRO_TICKERS: list[str] = list(MACRO.values())


def classify_tickers(text: str) -> list[str]:
    """Gibt alle Ticker zurück, deren Stichwörter im Text vorkommen.
    Beispiel: 'Nvidia und AMD legen zu' -> ['NVDA', 'AMD', 'XLK']"""
    if not text:
        return []
    haystack = f" {text.lower()} "
    hits = []
    for sym, keywords in SYMBOL_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            hits.append(sym)
    return hits
