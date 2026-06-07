"""src/tradingcal.py
Trading-/News-Kalender per Gemini: die wichtigsten anstehenden Markt-Events der
nächsten ~14 Tage (Notenbank, Makro-Daten, Earnings der gehaltenen/Universums-
Werte) mit kurzer Klartext-Auswirkung. Ein gebündelter LLM-Call (Suche an).

CLI:
    python -m src.tradingcal --scan
    python -m src.tradingcal --show
"""
import argparse
import json
import logging
import re
from datetime import date

from .db import (get_connection, ensure_calendar_table, replace_calendar,
                 fetch_calendar)
from .catalysts import _generate
from .deepdive import available
from .universe import RESEARCH_UNIVERSE

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [tradingcal] %(levelname)s: %(message)s")
log = logging.getLogger("tradingcal")


def _build_prompt() -> str:
    today = date.today().isoformat()
    universe = ", ".join(RESEARCH_UNIVERSE)
    return (
        f"Heutiges Datum: {today}. Recherchiere mit aktuellen Web-Quellen die "
        f"wichtigsten ANSTEHENDEN Markt-Events der nächsten 14 Tage für "
        f"US-Aktien:\n"
        f"- Notenbank (FOMC/Fed-Reden), Makro-Daten (CPI, PCE, Jobs/NFP, BIP)\n"
        f"- relevante Earnings aus dieser Liste: {universe}\n\n"
        f"Gib AUSSCHLIESSLICH ein JSON-Array zurück, je Objekt:\n"
        f'{{"date":"YYYY-MM-DD","title":"CPI Mai","category":"Makro|Notenbank|'
        f'Earnings","scope":"Markt oder Ticker","impact":"ein knapper Satz auf '
        f'Deutsch, was es für Kurse bedeutet"}}\n'
        f"Sortiere nach Datum, max. 18 Einträge, nur wirklich Marktbewegendes. "
        f"Kein Text außerhalb des JSON."
    )


def _parse(text: str) -> list[tuple]:
    if not text:
        return []
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", t, flags=re.DOTALL)
    if m:
        t = m.group(0)
    try:
        data = json.loads(t)
    except Exception as e:
        log.warning("JSON nicht parsebar: %s", e)
        return []
    out = []
    for d in data if isinstance(data, list) else []:
        try:
            out.append((str(d.get("date", ""))[:24], str(d.get("title", ""))[:120],
                        str(d.get("category", ""))[:24], str(d.get("scope", ""))[:40],
                        str(d.get("impact", ""))[:300]))
        except Exception:
            continue
    return out


def scan(conn) -> int:
    if not available():
        log.warning("Kein GEMINI_API_KEY – Kalender-Scan übersprungen.")
        return 0
    ensure_calendar_table(conn)
    rows = _parse(_generate(_build_prompt()) or "")
    if rows:
        replace_calendar(conn, rows)
    log.info("Kalender-Events gespeichert: %d", len(rows))
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="Trading-/News-Kalender (Gemini)")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    conn = get_connection()
    if args.scan:
        scan(conn)
    if args.show or not args.scan:
        ensure_calendar_table(conn)
        for e in fetch_calendar(conn):
            log.info("  %-12s [%-9s] %-26s – %s", e["event_date"],
                     e["category"], e["title"], e["impact"])


if __name__ == "__main__":
    main()
