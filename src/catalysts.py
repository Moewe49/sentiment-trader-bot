"""src/catalysts.py
Katalysator-Scanner: ein gebündelter Gemini-Call analysiert die jüngsten News
über das Universum und extrahiert strukturierte Event-Signale je Wert
(Typ, Richtung, Überzeugung, Horizont, Begründung) – Serenity-Stil, automatisiert.

Bewusst sparsam (Kosten!): EIN LLM-Call pro Lauf, nur für Werte mit aktuellen
News. Liefert erstmal Intelligenz fürs Dashboard und als spätere ML-Feature-
Quelle – KEIN Auto-Trading.

CLI:
    python -m src.catalysts --scan        # einen Scan ausführen
    python -m src.catalysts --show         # gespeicherte Katalysatoren zeigen
"""
import argparse
import json
import logging
import re
from datetime import date

from .config import config
from datetime import date as _date

from .db import (get_connection, ensure_catalysts_table, insert_catalysts,
                 fetch_recent_catalysts, fetch_news_for_scan,
                 ensure_catalyst_daily_table, upsert_catalyst_daily)
from .deepdive import available, _client_or_none, _MODEL_CANDIDATES
from .universe import RESEARCH_UNIVERSE

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [catalysts] %(levelname)s: %(message)s")
log = logging.getLogger("catalysts")

DIRECTIONS = {"bullish", "bearish", "neutral"}


def _build_prompt(news_by_ticker: dict) -> str:
    today = date.today().isoformat()
    lines = []
    for tkr, items in news_by_ticker.items():
        heads = "; ".join(f"[{ts}] {title}" for title, _s, ts in items[:6])
        lines.append(f"{tkr}: {heads}")
    blob = "\n".join(lines)
    return (
        f"Du bist ein erfahrener Buy-Side-Analyst (Stil: Lieferketten-/Engpass-"
        f"Recherche). Heutiges Datum: {today}.\n"
        f"Unten je Aktie die jüngsten Schlagzeilen. Identifiziere NUR Werte mit "
        f"einem echten, handelbaren Katalysator (Produktstart, Zulassung, "
        f"Großauftrag, Lieferketten-Engpass, Guidance, M&A, regulatorisch). "
        f"Ignoriere Rauschen/Allgemeinplätze.\n\n"
        f"Gib AUSSCHLIESSLICH ein JSON-Array zurück, je Objekt:\n"
        f'{{"ticker":"NVDA","type":"Produktstart","direction":"bullish",'
        f'"conviction":0.0-1.0,"horizon":"Tage|Wochen|Monate","rationale":'
        f'"ein knapper Satz auf Deutsch"}}\n'
        f"Maximal 15 Objekte, nur die überzeugendsten. Kein Text außerhalb des JSON.\n\n"
        f"NEWS:\n{blob}"
    )


def _parse(text: str) -> list[dict]:
    """Extrahiert das JSON-Array aus der Modellantwort (robust gegen ```-Fences)."""
    if not text:
        return []
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
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
            tkr = str(d.get("ticker", "")).upper().strip()
            if not tkr:
                continue
            direction = str(d.get("direction", "neutral")).lower().strip()
            if direction not in DIRECTIONS:
                direction = "neutral"
            conv = float(d.get("conviction", 0) or 0)
            conv = max(0.0, min(1.0, conv))
            out.append((tkr, str(d.get("type", ""))[:80], direction, conv,
                        str(d.get("horizon", ""))[:24],
                        str(d.get("rationale", ""))[:400]))
        except Exception:
            continue
    return out


def _generate(prompt: str) -> str | None:
    """Ein Gemini-Call mit Modell-Fallback (Google-Suche an für mehr Tiefe)."""
    client = _client_or_none()
    if client is None:
        return None
    try:
        from google.genai import types
    except Exception as e:
        log.error("Gemini-SDK fehlt: %s", e)
        return None
    last_err = None
    for model in [m for m in _MODEL_CANDIDATES if m]:
        for use_search in (True, False):
            try:
                cfg = types.GenerateContentConfig(
                    temperature=0.3,
                    tools=([types.Tool(google_search=types.GoogleSearch())]
                           if use_search else None))
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=cfg)
                text = (getattr(resp, "text", "") or "").strip()
                if text:
                    return text
            except Exception as e:
                last_err = e
                continue
    log.error("Gemini-Aufruf fehlgeschlagen: %s", last_err)
    return None


def scan(conn, universe: list = None, hours: int = 48) -> int:
    """Ein Scan-Durchlauf: News holen → LLM → Katalysatoren speichern.
    Gibt die Zahl gespeicherter Katalysatoren zurück."""
    if not available():
        log.warning("Kein GEMINI_API_KEY – Katalysator-Scan übersprungen.")
        return 0
    ensure_catalysts_table(conn)
    news = fetch_news_for_scan(conn, hours=hours,
                               universe=universe or RESEARCH_UNIVERSE)
    if not news:
        log.info("Keine aktuellen News (%dh) – nichts zu analysieren.", hours)
        return 0
    log.info("Analysiere News zu %d Werten (Gemini, 1 Call) …", len(news))
    text = _generate(_build_prompt(news))
    rows = _parse(text or "")
    if rows:
        insert_catalysts(conn, rows)
        # Forward-Feature-Log: signierter Score je Ticker (stärkster Katalysator)
        ensure_catalyst_daily_table(conn)
        scores = {}
        for tkr, _ctype, direction, conv, _hor, _why in rows:
            s = conv if direction == "bullish" else (-conv if direction == "bearish" else 0.0)
            if abs(s) > abs(scores.get(tkr, 0.0)):
                scores[tkr] = s
        upsert_catalyst_daily(conn, _date.today(), scores)
    log.info("Katalysatoren gespeichert: %d (Feature-Scores: %d)",
             len(rows), len({r[0] for r in rows}))
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="LLM-Katalysator-Scanner")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--hours", type=int, default=48)
    args = ap.parse_args()
    conn = get_connection()
    if args.scan:
        scan(conn, hours=args.hours)
    if args.show or not args.scan:
        ensure_catalysts_table(conn)
        for c in fetch_recent_catalysts(conn):
            log.info("  %-6s %-8s %.2f %-7s %s", c["ticker"], c["direction"],
                     c["conviction"] or 0, c["horizon"] or "", c["rationale"])


if __name__ == "__main__":
    main()
