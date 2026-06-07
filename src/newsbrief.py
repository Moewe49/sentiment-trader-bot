"""src/newsbrief.py
Kompakter, verständlicher News-Brief je Aktie (Gemini): WAS passiert gerade und
WAS es wahrscheinlich für den Kurs bedeutet – statt nur Roh-Headlines.

Gecacht in der research-Tabelle (kind='newsbrief'), Neuerzeugung nur, wenn der
letzte Brief älter als `max_age_hours` ist → günstig (Call nur bei Bedarf).
"""
import logging
from datetime import datetime, timezone

from .db import (ensure_research_table, fetch_research_one, save_research,
                 fetch_news_for_ticker)
from .deepdive import available
from .catalysts import _generate

log = logging.getLogger("newsbrief")


def _prompt(ticker: str, news: list) -> str:
    heads = "\n".join(
        f"- [{(n.get('published_at').strftime('%d.%m %H:%M') if n.get('published_at') else '')}] "
        f"{n.get('title','')} ({n.get('source','')})"
        for n in news)
    return (
        f"Hier die jüngsten Schlagzeilen zu {ticker}:\n{heads}\n\n"
        f"Fasse auf Deutsch knapp und konkret zusammen (kein Wiederholen der "
        f"Headlines, keine Floskeln):\n"
        f"**Was passiert:** 2–3 Sätze zur aktuellen Lage.\n"
        f"**Was es bedeutet:** 1–2 Sätze – wahrscheinliche Kurswirkung "
        f"(bullish/bearish/neutral) und warum.\n"
        f"Wenn die Nachrichtenlage dünn/irrelevant ist, sag das ehrlich."
    )


def brief(conn, ticker: str, max_age_hours: float = 6.0) -> str:
    """Gibt den (gecachten oder frisch erzeugten) News-Brief als Text zurück."""
    ensure_research_table(conn)
    row = fetch_research_one(conn, ticker, "newsbrief")
    if row and row[1]:
        age = (datetime.now(timezone.utc) - row[1]).total_seconds() / 3600.0
        if age < max_age_hours and row[0]:
            return row[0]
    if not available():
        return "Kein Gemini-Key aktiv – keine Zusammenfassung möglich."
    news = fetch_news_for_ticker(conn, ticker, 14)
    if not news:
        return "Keine getaggten News zu diesem Wert."
    text = _generate(_prompt(ticker, news))
    if not text:
        return "Zusammenfassung konnte nicht erzeugt werden."
    try:
        save_research(conn, ticker, "newsbrief", text)
    except Exception as e:
        log.warning("Newsbrief-Cache fehlgeschlagen (%s): %s", ticker, e)
    return text
