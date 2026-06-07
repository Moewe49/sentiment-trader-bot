"""src/rss_aggregator.py
Das erste eigene Modul des Bots.

Was es tut:
  1. Liest in einer Endlosschleife alle Feeds aus src/feeds.py
  2. Normalisiert jeden Eintrag (Titel, Zeit, URL)
  3. Bildet einen Dedup-Hash, damit nichts doppelt landet
  4. Schreibt neue Einträge in die TimescaleDB
  5. Schläft POLL_INTERVAL Sekunden und wiederholt

Start (in Docker):     automatisch via docker-compose
Start (lokal/Test):    python -m src.rss_aggregator
"""
import hashlib
import logging
import time
from datetime import datetime, timezone

import feedparser

from .config import config
from .db import get_connection, insert_news
from .feeds import FEEDS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("rss")


def _parse_time(entry) -> datetime:
    """Holt den Zeitstempel aus dem Feed-Eintrag. Fällt auf 'jetzt' zurück,
    falls der Feed keine verwertbare Zeit liefert."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            # feedparser liefert ein time.struct_time in UTC
            return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _dedup_hash(url: str, title: str) -> str:
    """Stabiler Fingerabdruck eines Artikels (URL + Titel)."""
    raw = f"{url.strip()}|{title.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def fetch_feed(source: str, url: str) -> list[dict]:
    """Lädt einen einzelnen Feed und gibt eine Liste normalisierter Zeilen zurück."""
    parsed = feedparser.parse(url, agent=config.USER_AGENT)

    if parsed.bozo and not parsed.entries:
        # bozo = Feed war fehlerhaft/nicht erreichbar
        log.warning("Feed '%s' fehlerhaft oder leer: %s", source, parsed.bozo_exception)
        return []

    rows = []
    for e in parsed.entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        rows.append({
            "published_at": _parse_time(e),
            "source": source,
            "title": title,
            "summary": (e.get("summary") or "").strip()[:2000],
            "url": link,
            "dedup_hash": _dedup_hash(link, title),
        })
    return rows


def run_once(conn) -> int:
    """Ein kompletter Durchlauf über alle Feeds. Gibt Anzahl neuer Artikel zurück."""
    total_new = 0
    for source, url in FEEDS.items():
        try:
            rows = fetch_feed(source, url)
            new = insert_news(conn, rows)
            total_new += new
            log.info("%-22s | %3d gelesen | %3d neu", source, len(rows), new)
        except Exception as exc:  # einzelner Feed-Fehler darf den Lauf nicht killen
            log.error("Fehler bei Feed '%s': %s", source, exc)
    return total_new


def main():
    log.info("RSS-Aggregator startet. Poll-Intervall: %ds", config.POLL_INTERVAL)
    conn = get_connection()
    while True:
        start = time.monotonic()
        new = run_once(conn)
        log.info("Durchlauf fertig: %d neue Artikel gesamt.", new)
        elapsed = time.monotonic() - start
        time.sleep(max(0, config.POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
