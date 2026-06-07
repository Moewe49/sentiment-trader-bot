"""src/feeds.py
Kuratierte Liste der RSS-Quellen.
Jede Quelle hat einen kurzen 'source'-Key (landet in der DB-Spalte 'source')
und die Feed-URL. Neue Quellen einfach hier ergänzen.

Hinweis: Feed-URLs ändern sich gelegentlich. Wenn ein Feed leer bleibt,
im Log nachsehen ('0 entries') und URL prüfen.
"""

FEEDS: dict[str, str] = {
    # --- International / Finanzen -----------------------------------------
    "reuters_business":  "https://www.reutersagency.com/feed/?best-topics=business-finance",
    "marketwatch_top":   "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "yahoo_finance":     "https://finance.yahoo.com/news/rssindex",
    "cnbc_finance":      "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
    "ft_companies":      "https://www.ft.com/companies?format=rss",

    # --- US-Politik / Makro (bewegt Sektoren stark) -----------------------
    "cnbc_economy":      "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "fed_press":         "https://www.federalreserve.gov/feeds/press_all.xml",

    # --- Deutschland ------------------------------------------------------
    "ntv_wirtschaft":    "https://www.n-tv.de/wirtschaft/rss",
    "handelsblatt":      "https://www.handelsblatt.com/contentexport/feed/finanzen",
    "tagesschau_wirtschaft": "https://www.tagesschau.de/wirtschaft/index~rss2.xml",

    # --- Regulierung / Filings -------------------------------------------
    "sec_press":         "https://www.sec.gov/news/pressreleases.rss",
}
