"""src/research.py
Qualitatives Deep-Dive-Research per Gemini (Google AI Studio, Free-Tier tauglich).

- Ohne GEMINI_API_KEY ist die Funktion inaktiv (der Bot läuft normal weiter).
- Das Modell wird automatisch gewählt: erst das konfigurierte, sonst Fallbacks
  (so ist egal, was der jeweilige Key freischaltet).
- Wenn das Modell Google-Search-Grounding unterstützt, wird es genutzt (aktuelle
  Quellen statt nur Trainingswissen); sonst sauberer Fallback ohne Suche.
- Aufrufe sind langsam/quota-begrenzt → Ergebnisse werden in der DB gecacht.
"""
import logging
from datetime import date

from .config import config

log = logging.getLogger("research")

_MODEL_CANDIDATES = [config.GEMINI_MODEL, "gemini-2.5-flash", "gemini-2.0-flash",
                     "gemini-2.5-flash-lite", "gemini-flash-latest"]

_client = None
_working_model = None

# Verfügbare Report-Typen (Label fürs Dashboard)
KINDS = {
    "deepdive": "Deep Dive",
    "risk": "Risiko (Skeptiker)",
}


def available() -> bool:
    """True, wenn ein Gemini-Key gesetzt ist."""
    return bool(config.GEMINI_API_KEY)


def _client_or_none():
    global _client
    if not available():
        return None
    if _client is None:
        try:
            from google import genai
            _client = genai.Client(api_key=config.GEMINI_API_KEY)
        except Exception as e:
            log.error("Gemini-Client konnte nicht initialisiert werden: %s", e)
            return None
    return _client


_STYLE = (
    "Schreibe auf Deutsch. WICHTIG: Beginne OHNE Einleitung und ohne Meta-Sätze "
    "– kein 'Als Analyst', 'Aus Sicht', 'Hier ist', keine Beschreibung deiner "
    "Vorgehensweise. Starte direkt mit dem Ergebnis. Hebe die wichtigsten "
    "Schlüsselbegriffe mit **fett** hervor (sparsam, nur das Entscheidende). "
    "Nutze kurze Abschnitts-Überschriften (## Überschrift). Sei konkret mit "
    "Zahlen; verlinke Quellen inline. Keine Floskeln."
)


def _prompt(kind: str, ticker: str) -> str:
    today = date.today().isoformat()
    if kind == "risk":
        return (
            f"Rolle (nur intern, nicht erwähnen): skeptischer Short-Seller. "
            f"Heutiges Datum: {today}. Recherchiere {ticker} mit aktuellen "
            f"Web-Quellen (Bear-Cases, SEC-Filings, News).\n{_STYLE}\n\n"
            f"Liefere eine prägnante Risikoanalyse mit diesen Abschnitten:\n"
            f"## Risiko-Level\nGenau ein Wort als Gesamteinschätzung: HOCH, "
            f"MITTEL oder NIEDRIG.\n"
            f"## Bilanz-/Accounting-Warnsignale\n"
            f"## Kunden-/Umsatzkonzentration\n"
            f"## Wettbewerbsbedrohungen\n"
            f"## Größtes Einzelrisiko\nGenau ein Satz."
        )
    # default: deepdive
    return (
        f"Rolle (nur intern, nicht erwähnen): nüchterner Aktienanalyst. "
        f"Heutiges Datum: {today}. Recherchiere {ticker} mit aktuellen "
        f"Web-Quellen (Filings, seriöse News).\n{_STYLE}\n\n"
        f"Liefere einen kompakten Deep-Dive mit diesen Abschnitten:\n"
        f"## Geschäftsmodell\nWomit wird Geld verdient, Kernprodukt einfach erklärt.\n"
        f"## Moat & Wettbewerb\nTop-3-Konkurrenten; echter technologischer Vorsprung?\n"
        f"## Katalysatoren (12 Monate)\nKonkrete Produktstarts, Zulassungen, Earnings.\n"
        f"## Asymmetrie-Check\nDownside-Boden vs. Upside-Ceiling – schief?\n"
        f"## Fazit\nEin Satz: Bullish/Neutral/Bearish + ein Grund."
    )


def generate(kind: str, ticker: str) -> str | None:
    """Erzeugt einen Report. None, wenn kein Key gesetzt ist; sonst Markdown-Text
    (oder eine Fehlermeldung als Text)."""
    global _working_model
    client = _client_or_none()
    if client is None:
        return None

    prompt = _prompt(kind, ticker)
    try:
        from google.genai import types
    except Exception as e:
        return f"⚠️ Gemini-SDK nicht verfügbar: {e}"

    # Reihenfolge der Modelle (Duplikate raus, working_model zuerst)
    models, seen = [], set()
    for m in ([_working_model] if _working_model else []) + _MODEL_CANDIDATES:
        if m and m not in seen:
            seen.add(m)
            models.append(m)

    last_err = None
    for model in models:
        for use_search in (True, False):
            try:
                if use_search:
                    cfg = types.GenerateContentConfig(
                        temperature=0.4,
                        tools=[types.Tool(google_search=types.GoogleSearch())])
                else:
                    cfg = types.GenerateContentConfig(temperature=0.4)
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=cfg)
                text = (getattr(resp, "text", "") or "").strip()
                if text:
                    _working_model = model
                    log.info("Gemini-Report '%s' für %s erzeugt (Modell %s%s).",
                             kind, ticker, model,
                             ", mit Suche" if use_search else "")
                    return text
            except Exception as e:
                last_err = e
                continue
    log.error("Gemini-Aufruf fehlgeschlagen (%s/%s): %s", kind, ticker, last_err)
    return f"⚠️ Research fehlgeschlagen: {last_err}"
