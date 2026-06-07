"""src/sentiment_worker.py
Zweites eigenes Modul: macht aus rohen Headlines echte Signale.

Was es tut, in einer Endlosschleife:
  1. Holt unverarbeitete News aus der DB (processed = FALSE)
  2. Ordnet jeder News passende Sektoren zu (src/sectors.py)
  3. Bewertet das Sentiment mit FinBERT  ->  Wert zwischen -1 (bearish) und +1 (bullish)
  4. Schreibt sentiment + sectors zurück und setzt processed = TRUE

Sprache: Englische Quellen werden mit FinBERT bewertet. Deutsche Quellen
(ntv, Handelsblatt, tagesschau) bekommen ihr Sentiment von einem eigenen
deutschen Modell. Beide Modelle laufen lokal auf der CPU (kostenlos) und
werden erst beim ersten Gebrauch geladen.
"""
import logging
import time

from .config import config
from .db import (get_connection, fetch_unprocessed, update_sentiment,
                ensure_news_tickers_column)
from .sectors import classify_sectors
from .universe import classify_tickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("sentiment")

# Englisches Finanz-Modell und deutsches Sentiment-Modell.
EN_MODEL = "ProsusAI/finbert"
DE_MODEL = "oliverguhr/german-sentiment-bert"

# Quellen, die deutschsprachig sind -> werden mit dem deutschen Modell bewertet.
GERMAN_SOURCES = {"ntv_wirtschaft", "handelsblatt", "tagesschau_wirtschaft"}

# Modelle werden erst beim ersten Gebrauch geladen (lazy), nicht beim Import.
_models: dict = {}      # model_name -> (tokenizer, model)
_torch = None


def _load_model(model_name: str):
    """Lädt ein Modell genau einmal. Beim allerersten Mal wird es
    (~440 MB) aus dem Internet heruntergeladen und dann zwischengespeichert."""
    global _torch
    if model_name not in _models:
        import torch
        from transformers import (AutoTokenizer,
                                   AutoModelForSequenceClassification)
        log.info("Lade Modell %s – beim ersten Start kann das etwas dauern ...",
                 model_name)
        _torch = torch
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSequenceClassification.from_pretrained(model_name)
        model.eval()
        log.info("Modell %s geladen. Labels: %s", model_name, model.config.id2label)
        _models[model_name] = (tokenizer, model)
    return _models[model_name]


def _score(text: str, model_name: str) -> float:
    """Gibt einen Wert von -1.0 (sehr negativ) bis +1.0 (sehr positiv) zurück.
    Berechnung: Wahrscheinlichkeit(positiv) minus Wahrscheinlichkeit(negativ).
    Die Zuordnung erfolgt über die Label-Namen, daher funktioniert sie für
    FinBERT (en) und das deutsche Modell gleichermaßen."""
    tokenizer, model = _load_model(model_name)
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    with _torch.no_grad():
        logits = model(**inputs).logits
    probs = _torch.softmax(logits, dim=-1)[0]
    id2label = model.config.id2label
    p = {id2label[i].lower(): float(probs[i]) for i in range(len(probs))}
    return round(p.get("positive", 0.0) - p.get("negative", 0.0), 4)


def score_sentiment(text: str) -> float:
    """Sentiment einer englischen Headline (FinBERT)."""
    return _score(text, EN_MODEL)


def score_sentiment_de(text: str) -> float:
    """Sentiment einer deutschen Headline (deutsches Modell)."""
    return _score(text, DE_MODEL)


def process_batch(conn, limit: int = 100) -> int:
    """Verarbeitet einen Schwung unverarbeiteter News. Gibt deren Anzahl zurück."""
    rows = fetch_unprocessed(conn, limit=limit)
    scored_en = scored_de = 0
    for r in rows:
        text = f"{r['title']}. {r['summary'] or ''}".strip()
        sectors = classify_sectors(text)
        tickers = classify_tickers(text)

        if r["source"] in GERMAN_SOURCES:
            sentiment = score_sentiment_de(text)
            scored_de += 1
        else:
            sentiment = score_sentiment(text)
            scored_en += 1

        update_sentiment(conn, r["id"], r["published_at"], sentiment,
                         sectors, tickers)

        if tickers:  # nur die für uns relevanten News loggen
            log.info("[%+.2f] %s | %s", sentiment, ",".join(tickers), r["title"][:60])

    if rows:
        log.info("Batch fertig: %d verarbeitet (%d englisch, %d deutsch)",
                 len(rows), scored_en, scored_de)
    return len(rows)


def main():
    log.info("Sentiment-Worker startet.")
    conn = get_connection()
    ensure_news_tickers_column(conn)
    while True:
        try:
            n = process_batch(conn)
        except Exception as exc:
            log.error("Fehler im Batch: %s", exc)
            n = 0
        # Wenn nichts zu tun war, etwas länger warten
        time.sleep(config.SENTIMENT_INTERVAL if n == 0 else 2)


if __name__ == "__main__":
    main()
