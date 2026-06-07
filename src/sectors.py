"""src/sectors.py
Ordnet News-Texten Sektoren (= ETFs) zu, anhand von Stichwörtern.
Bewusst simpel (Stichwort-Treffer) – das ist für den Start völlig ausreichend
und du siehst genau, WARUM eine Nachricht einem Sektor zugeordnet wurde.

Du kannst Stichwörter jederzeit ergänzen. Kleinschreibung reicht,
der Vergleich ist case-insensitive.
"""

SECTORS: dict[str, dict] = {
    # --- Technologie & Chips  -> ETF XLK / SOXX ---------------------------
    "XLK": {
        "name": "Technologie & Chips",
        "keywords": [
            "semiconductor", "halbleiter", "chip", "chips", "nvidia", "amd",
            "intel", "tsmc", "asml", "broadcom", "apple", "microsoft", "google",
            "alphabet", "meta", "software", "cloud", "data center", "rechenzentrum",
            "gpu", "foundry", "artificial intelligence", "künstliche intelligenz",
            " ai ", " ki ", "tech ", "techaktien",
        ],
    },
    # --- Energie & Öl  -> ETF XLE -----------------------------------------
    "XLE": {
        "name": "Energie & Öl",
        "keywords": [
            "oil", "öl", "crude", "opec", "brent", "wti", "barrel", "gas",
            "erdgas", "petroleum", "exxon", "chevron", "shell", " bp ",
            "totalenergies", "energy", "energie", "drilling", "bohrung",
            "refinery", "raffinerie", "pipeline",
        ],
    },
    # --- Finanzen & Banken  -> ETF XLF ------------------------------------
    "XLF": {
        "name": "Finanzen & Banken",
        "keywords": [
            "bank", "banks", "banken", "federal reserve", " fed ", "ezb", "ecb",
            "interest rate", "zins", "zinsen", "rate hike", "rate cut",
            "jpmorgan", "goldman", "morgan stanley", "citigroup", "wells fargo",
            "deutsche bank", "lending", "kredit", "mortgage", "hypothek",
            "fdic", "basel", "financial", "yield", "rendite", "treasury",
        ],
    },
}


def classify_sectors(text: str) -> list[str]:
    """Gibt die Liste der ETF-Kürzel zurück, deren Stichwörter im Text vorkommen.
    Beispiel: 'Nvidia steigt nach starkem Chip-Absatz' -> ['XLK']"""
    if not text:
        return []
    # Leerzeichen vorn/hinten anfügen, damit Treffer wie ' fed ' / ' ai ' sauber greifen
    haystack = f" {text.lower()} "
    hits = []
    for etf, cfg in SECTORS.items():
        if any(kw in haystack for kw in cfg["keywords"]):
            hits.append(etf)
    return hits
