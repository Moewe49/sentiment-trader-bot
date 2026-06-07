-- db/init.sql
-- Wird beim ersten Start des DB-Containers automatisch ausgeführt.
-- Legt die TimescaleDB-Extension und unsere News-Tabelle als Hypertable an.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Rohe News-Headlines aus allen Quellen (RSS, Truth Social, X, ...)
CREATE TABLE IF NOT EXISTS news (
    id           BIGSERIAL,
    published_at TIMESTAMPTZ   NOT NULL,          -- Zeitstempel der Nachricht
    fetched_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    source       TEXT          NOT NULL,          -- z.B. 'reuters', 'ntv', 'truthsocial'
    title        TEXT          NOT NULL,
    summary      TEXT,
    url          TEXT          NOT NULL,
    -- SHA-256 aus URL+Titel: verhindert Duplikate über alle Quellen hinweg
    dedup_hash   TEXT          NOT NULL,
    -- Sentiment-Felder werden später vom Sentiment-Worker befüllt (jetzt NULL)
    sentiment    DOUBLE PRECISION,                -- -1.0 (bearish) ... +1.0 (bullish)
    sectors      TEXT[],                          -- z.B. {'XLE','XLK'}
    tickers      TEXT[],                          -- konkrete Werte, z.B. {'NVDA','AMD','XLK'}
    processed    BOOLEAN       NOT NULL DEFAULT FALSE,
    PRIMARY KEY (id, published_at)
);

-- In eine Hypertable umwandeln: TimescaleDB partitioniert automatisch nach Zeit.
SELECT create_hypertable('news', 'published_at', if_not_exists => TRUE);

-- Eindeutigkeit pro Tag-Partition: derselbe Artikel wird nie doppelt gespeichert.
CREATE UNIQUE INDEX IF NOT EXISTS idx_news_dedup
    ON news (dedup_hash, published_at);

-- Schneller Zugriff auf noch nicht verarbeitete News (für den Sentiment-Worker)
CREATE INDEX IF NOT EXISTS idx_news_unprocessed
    ON news (published_at DESC) WHERE processed = FALSE;

-- Protokoll aller erzeugten Trade-Signale (auch HOLD), damit sich die
-- Performance der Strategie später nachvollziehen lässt.
CREATE TABLE IF NOT EXISTS signals (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    etf        TEXT NOT NULL,
    action     TEXT NOT NULL,                  -- BUY / SELL / HOLD
    score      DOUBLE PRECISION NOT NULL,      -- kombinierter Score
    sentiment  DOUBLE PRECISION,               -- Sentiment-Anteil (kann NULL sein)
    technical  DOUBLE PRECISION,               -- Technik-Anteil (kann NULL sein)
    quantity   INTEGER,                        -- gehandelte Stückzahl (NULL bei HOLD)
    executed   BOOLEAN NOT NULL DEFAULT FALSE, -- Order tatsächlich platziert?
    reason     TEXT,
    -- Outcome-Tracking: wird vom Evaluator nachträglich befüllt, damit sich
    -- messen lässt, ob ein Signal richtig lag (Grundlage fürs spätere Lernen).
    price_at_signal DOUBLE PRECISION,          -- Tagesschlusskurs am Signaltag
    price_1d        DOUBLE PRECISION,          -- Schlusskurs 1 Börsentag später
    price_5d        DOUBLE PRECISION,          -- Schlusskurs 5 Börsentage später
    return_1d       DOUBLE PRECISION,          -- Forward-Return nach 1 Tag
    return_5d       DOUBLE PRECISION,          -- Forward-Return nach 5 Tagen
    hit             BOOLEAN,                   -- lag das Signal richtig? (BUY: +, SELL: -)
    evaluated       BOOLEAN NOT NULL DEFAULT FALSE, -- 5-Tage-Return endgültig berechnet?
    -- ML-Rohmerkmale zum Signalzeitpunkt (für späteres Modelltraining)
    rsi             DOUBLE PRECISION,
    sma_dist        DOUBLE PRECISION,          -- (Kurs - SMA20) / SMA20
    volatility      DOUBLE PRECISION,          -- 20-Tage-Volatilität der Tagesrenditen
    n_headlines     INTEGER,                   -- Anzahl Headlines hinter dem Sentiment
    -- Fundamental-Features (yfinance)
    ps_ttm          DOUBLE PRECISION,
    ev_ebitda       DOUBLE PRECISION,
    gross_margin    DOUBLE PRECISION,
    rev_growth      DOUBLE PRECISION,
    value_growth    DOUBLE PRECISION
);

-- Verlauf des Kontowerts (Equity-Kurve fürs Dashboard)
CREATE TABLE IF NOT EXISTS equity_curve (
    ts      TIMESTAMPTZ PRIMARY KEY,
    net_liq DOUBLE PRECISION NOT NULL,
    cash    DOUBLE PRECISION
);

-- Täglicher realisierter G/V (für Umschaltung Tag/Woche/Monat/Jahr)
CREATE TABLE IF NOT EXISTS realized_daily (
    day      DATE PRIMARY KEY,
    realized DOUBLE PRECISION NOT NULL
);

-- Tatsächliche Ausführungen (IB-Fills) für den Trade-Verlauf
CREATE TABLE IF NOT EXISTS trades (
    exec_id    TEXT PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        DOUBLE PRECISION,
    price      DOUBLE PRECISION,
    currency   TEXT,
    realized   DOUBLE PRECISION,
    commission DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts DESC);

-- Schneller Zugriff auf noch nicht (endgültig) bewertete Signale (für den Evaluator)
CREATE INDEX IF NOT EXISTS idx_signals_unevaluated
    ON signals (ts ASC) WHERE evaluated = FALSE;

-- Vom Nutzer im Dashboard manuell hinzugefügte Werte. Werden von der
-- Signal-Engine zusätzlich zur Kernliste + News-Tickern gehandelt.
CREATE TABLE IF NOT EXISTS watchlist (
    ticker   TEXT PRIMARY KEY,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Im Dashboard live einstellbare Strategie-Parameter (Schlüssel/Wert).
-- Fehlt ein Schlüssel, gilt der Default aus .env/Code.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Historischer Kursspeicher (OHLCV) fürs Research/Backtesting/ML.
CREATE TABLE IF NOT EXISTS prices_daily (
    ticker TEXT NOT NULL,
    day    DATE NOT NULL,
    open   DOUBLE PRECISION,
    high   DOUBLE PRECISION,
    low    DOUBLE PRECISION,
    close  DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    PRIMARY KEY (ticker, day)
);
SELECT create_hypertable('prices_daily', 'day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_prices_daily_day ON prices_daily (day DESC);

-- Trim-State: welche Gewinn-Stufen (+25/+50/+100%) je Position schon
-- abverkauft wurden (Buy & Hold mit stufenweiser Gewinnmitnahme).
CREATE TABLE IF NOT EXISTS trim_state (
    symbol      TEXT PRIMARY KEY,
    base_qty    DOUBLE PRECISION,
    levels_done TEXT NOT NULL DEFAULT ''
);

-- Shadow-Modus: validierter systematischer Kern berechnet live sein Ziel-
-- Portfolio + hypothetische Equity-Kurve, OHNE zu handeln (Forward-Track).
CREATE TABLE IF NOT EXISTS shadow_portfolio (
    rebal_day DATE PRIMARY KEY,
    regime_on BOOLEAN NOT NULL,
    tickers   TEXT[] NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_equity (
    day        DATE PRIMARY KEY,
    level      DOUBLE PRECISION NOT NULL,
    ret        DOUBLE PRECISION,
    exposure   DOUBLE PRECISION,
    n_holdings INTEGER
);

-- Per LLM (Gemini) aus News extrahierte Katalysator-Signale.
CREATE TABLE IF NOT EXISTS catalysts (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ticker     TEXT NOT NULL,
    ctype      TEXT,
    direction  TEXT,
    conviction DOUBLE PRECISION,
    horizon    TEXT,
    rationale  TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalysts_created ON catalysts (created_at DESC);

-- Täglicher signierter Katalysator-Score je Ticker (Forward-Feature fürs ML).
CREATE TABLE IF NOT EXISTS catalyst_daily (
    day    DATE NOT NULL,
    ticker TEXT NOT NULL,
    score  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (day, ticker)
);

-- Trading-/News-Kalender: anstehende Events (per Gemini) mit Auswirkung.
CREATE TABLE IF NOT EXISTS calendar_events (
    id         BIGSERIAL PRIMARY KEY,
    event_date TEXT,
    title      TEXT,
    category   TEXT,
    scope      TEXT,
    impact     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Deep-Dive-/Risiko-Berichte mit Historie (mehrere je Aktie, einzeln löschbar).
CREATE TABLE IF NOT EXISTS reports (
    id         BIGSERIAL PRIMARY KEY,
    ticker     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reports_ticker ON reports (ticker, created_at DESC);

-- Cache für per Gemini erzeugte Reports (newsbrief etc., je ticker+kind einer).
CREATE TABLE IF NOT EXISTS research (
    ticker     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, kind)
);
