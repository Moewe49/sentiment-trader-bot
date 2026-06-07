"""src/db.py
Dünne DB-Schicht. Kapselt Verbindungsaufbau und das Einfügen von News.
Bewusst simpel gehalten (psycopg2) – kein ORM, damit du jeden SQL-Befehl siehst.
"""
import logging
import time

import psycopg2
from psycopg2.extras import execute_values

from .config import config

log = logging.getLogger("db")


def get_connection(retries: int = 10, delay: float = 3.0):
    """Stellt eine DB-Verbindung her. Wartet beim Start, bis die DB bereit ist
    (wichtig in Docker: der DB-Container braucht ein paar Sekunden Vorlauf)."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(config.db_dsn)
            conn.autocommit = True
            log.info("DB-Verbindung hergestellt.")
            return conn
        except psycopg2.OperationalError as e:
            last_err = e
            log.warning("DB noch nicht bereit (Versuch %d/%d) ...", attempt, retries)
            time.sleep(delay)
    raise RuntimeError(f"DB-Verbindung fehlgeschlagen: {last_err}")


def insert_news(conn, rows: list[dict]) -> int:
    """Fügt mehrere News-Zeilen ein. Duplikate (gleicher dedup_hash am selben Tag)
    werden dank ON CONFLICT stillschweigend ignoriert.
    Gibt die Anzahl tatsächlich NEU eingefügter Zeilen zurück."""
    if not rows:
        return 0

    sql = """
        INSERT INTO news (published_at, source, title, summary, url, dedup_hash)
        VALUES %s
        ON CONFLICT (dedup_hash, published_at) DO NOTHING
    """
    values = [
        (r["published_at"], r["source"], r["title"],
         r["summary"], r["url"], r["dedup_hash"])
        for r in rows
    ]

    with conn.cursor() as cur:
        before = _row_count(cur)
        execute_values(cur, sql, values)
        after = _row_count(cur)
    return after - before


def _row_count(cur) -> int:
    cur.execute("SELECT count(*) FROM news")
    return cur.fetchone()[0]


def fetch_unprocessed(conn, limit: int = 100) -> list[dict]:
    """Holt die neuesten noch nicht verarbeiteten News (processed = FALSE)."""
    sql = """
        SELECT id, published_at, source, title, summary
        FROM news
        WHERE processed = FALSE
        ORDER BY published_at DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def ensure_signals_table(conn):
    """Legt die signals-Tabelle an, falls sie noch nicht existiert.
    Wird beim Start der Signal-Engine aufgerufen – so funktioniert es auch
    bei bestehenden DB-Volumes, in denen init.sql nicht erneut läuft."""
    sql = """
        CREATE TABLE IF NOT EXISTS signals (
            id         BIGSERIAL PRIMARY KEY,
            ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
            etf        TEXT NOT NULL,
            action     TEXT NOT NULL,
            score      DOUBLE PRECISION NOT NULL,
            sentiment  DOUBLE PRECISION,
            technical  DOUBLE PRECISION,
            quantity   INTEGER,
            executed   BOOLEAN NOT NULL DEFAULT FALSE,
            reason     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts DESC);
    """
    with conn.cursor() as cur:
        cur.execute(sql)


def insert_signal(conn, sig: dict):
    """Speichert ein einzelnes Signal eines Durchlaufs inkl. ML-Rohmerkmale
    (rsi, sma_dist, volatility, n_headlines). Fehlende Keys → NULL/False."""
    sql = """
        INSERT INTO signals
            (etf, action, score, sentiment, technical, quantity, executed, reason,
             rsi, sma_dist, volatility, n_headlines,
             ps_ttm, ev_ebitda, gross_margin, rev_growth, value_growth)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            sig["etf"],
            sig["action"],
            sig["score"],
            sig.get("sentiment"),
            sig.get("technical"),
            sig.get("quantity"),
            sig.get("executed", False),
            sig.get("reason"),
            sig.get("rsi"),
            sig.get("sma_dist"),
            sig.get("volatility"),
            sig.get("n_headlines"),
            sig.get("ps_ttm"),
            sig.get("ev_ebitda"),
            sig.get("gross_margin"),
            sig.get("rev_growth"),
            sig.get("value_growth"),
        ))


def fetch_recent_signals(conn, limit: int = 15) -> list[dict]:
    """Holt die zuletzt erzeugten Signale (für die Dashboard-Historie)."""
    sql = """
        SELECT ts, etf, action, score, sentiment, technical, quantity, executed,
               reason, return_5d
        FROM signals
        ORDER BY ts DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def ensure_signals_outcome_columns(conn):
    """Ergänzt die Outcome-Spalten der signals-Tabelle, falls sie fehlen
    (für bestehende DBs, in denen init.sql nicht erneut läuft)."""
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS price_at_signal DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS price_1d        DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS price_5d        DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS return_1d       DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS return_5d       DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS hit             BOOLEAN;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS evaluated       BOOLEAN NOT NULL DEFAULT FALSE;
            -- ML-Rohmerkmale zum Signalzeitpunkt (für späteres Modelltraining)
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS rsi             DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS sma_dist        DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS volatility      DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS n_headlines     INTEGER;
            -- Fundamental-Features
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS ps_ttm          DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS ev_ebitda       DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS gross_margin    DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS rev_growth      DOUBLE PRECISION;
            ALTER TABLE signals ADD COLUMN IF NOT EXISTS value_growth    DOUBLE PRECISION;
        """)


def fetch_latest_fundamentals(conn) -> list[dict]:
    """Je Symbol die zuletzt geloggten Fundamentalwerte (für die Peer-Tabelle)."""
    sql = """
        SELECT DISTINCT ON (etf)
               etf, ps_ttm, ev_ebitda, gross_margin, rev_growth, value_growth
        FROM signals
        WHERE ps_ttm IS NOT NULL OR ev_ebitda IS NOT NULL OR rev_growth IS NOT NULL
        ORDER BY etf, ts DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def ensure_equity_table(conn):
    """Tabelle für den Verlauf des Kontowerts (Equity-Kurve)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS equity_curve (
                ts      TIMESTAMPTZ PRIMARY KEY,
                net_liq DOUBLE PRECISION NOT NULL,
                cash    DOUBLE PRECISION
            )
        """)


def insert_equity(conn, net_liq: float, cash: float = None):
    """Speichert einen Kontowert-Snapshot, auf die volle Stunde gerundet
    (max. 1 Punkt pro Stunde – ON CONFLICT verwirft Doppelte)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO equity_curve (ts, net_liq, cash) "
            "VALUES (date_trunc('hour', now()), %s, %s) "
            "ON CONFLICT (ts) DO UPDATE SET net_liq = EXCLUDED.net_liq, "
            "cash = EXCLUDED.cash",
            (net_liq, cash))


def ensure_realized_table(conn):
    """Tabelle für den täglichen realisierten G/V (für Tag/Woche/Monat/Jahr)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS realized_daily (
                day      DATE PRIMARY KEY,
                realized DOUBLE PRECISION NOT NULL
            )
        """)


def upsert_realized(conn, realized: float):
    """Speichert/aktualisiert den realisierten G/V des heutigen Tages
    (IB liefert RealizedPnL als Tageswert → wir sammeln ihn pro Tag)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO realized_daily (day, realized) VALUES (CURRENT_DATE, %s) "
            "ON CONFLICT (day) DO UPDATE SET realized = EXCLUDED.realized",
            (realized,))


def fetch_realized_periods(conn) -> dict:
    """Realisierter G/V summiert für Tag / Woche / Monat / Jahr / Gesamt."""
    sql = """
        SELECT
          COALESCE(SUM(realized) FILTER (WHERE day = CURRENT_DATE), 0),
          COALESCE(SUM(realized) FILTER (WHERE day >= date_trunc('week',  CURRENT_DATE)::date), 0),
          COALESCE(SUM(realized) FILTER (WHERE day >= date_trunc('month', CURRENT_DATE)::date), 0),
          COALESCE(SUM(realized) FILTER (WHERE day >= date_trunc('year',  CURRENT_DATE)::date), 0),
          COALESCE(SUM(realized), 0)
        FROM realized_daily
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        d, w, m, y, t = cur.fetchone()
    return {"day": float(d), "week": float(w), "month": float(m),
            "year": float(y), "total": float(t)}


def fetch_news_for_ticker(conn, ticker: str, limit: int = 12) -> list[dict]:
    """Letzte Schlagzeilen, die diesem Ticker zugeordnet wurden (+ Sentiment)."""
    sql = """
        SELECT published_at, sentiment, source, title, url
        FROM news
        WHERE processed = TRUE AND tickers IS NOT NULL AND %s = ANY(tickers)
        ORDER BY published_at DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ticker, limit))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# -------------------------------------------------------------------------
# Trade-Verlauf: tatsächliche Ausführungen (IB-Fills), persistent gesammelt
# -------------------------------------------------------------------------
def ensure_trades_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
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
            )
        """)


def upsert_fill(conn, f: dict):
    """Speichert eine Ausführung (idempotent über exec_id)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trades (exec_id, ts, symbol, side, qty, price, currency, "
            "realized, commission) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (exec_id) DO NOTHING",
            (f["exec_id"], f["ts"], f["symbol"], f["side"], f["qty"], f["price"],
             f.get("currency"), f.get("realized"), f.get("commission")))


def fetch_trades(conn, limit: int = 100) -> list[dict]:
    """Jüngste Ausführungen (für die Trade-Verlauf-Tabelle)."""
    sql = """
        SELECT ts, symbol, side, qty, price, currency, realized
        FROM trades ORDER BY ts DESC LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_equity(conn, limit: int = 500) -> list[dict]:
    """Equity-Punkte (älteste→neueste) für den Dashboard-Chart."""
    sql = """
        SELECT ts, net_liq FROM (
            SELECT ts, net_liq FROM equity_curve ORDER BY ts DESC LIMIT %s
        ) t ORDER BY ts ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        return [{"ts": r[0], "net_liq": float(r[1])} for r in cur.fetchall()]


def fetch_unevaluated_signals(conn, min_age_hours: int = 24) -> list[dict]:
    """Signale, die noch nicht endgültig bewertet wurden und alt genug sind,
    dass mindestens der 1-Tages-Return vorliegen kann. Älteste zuerst."""
    sql = """
        SELECT id, ts, etf, action
        FROM signals
        WHERE evaluated = FALSE
          AND ts < now() - make_interval(hours => %s)
        ORDER BY ts ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (min_age_hours,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def update_signal_outcome(conn, signal_id: int, price_at_signal, price_1d,
                          price_5d, return_1d, return_5d, hit, evaluated: bool):
    """Schreibt das nachträglich ermittelte Ergebnis eines Signals zurück."""
    sql = """
        UPDATE signals
        SET price_at_signal = %s, price_1d = %s, price_5d = %s,
            return_1d = %s, return_5d = %s, hit = %s, evaluated = %s
        WHERE id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (price_at_signal, price_1d, price_5d,
                          return_1d, return_5d, hit, evaluated, signal_id))


def fetch_strategy_performance(conn) -> list[dict]:
    """Trefferquote und Ø-Returns je Aktion (BUY/SELL) über alle bewerteten
    Signale. Dient dem Dashboard, um zu zeigen, ob die Strategie trägt."""
    sql = """
        SELECT action,
               COUNT(*)                                   AS n,
               AVG(CASE WHEN hit THEN 1.0 ELSE 0.0 END)   AS hit_rate,
               AVG(return_1d)                             AS avg_return_1d,
               AVG(return_5d)                             AS avg_return_5d
        FROM signals
        WHERE return_1d IS NOT NULL
          AND action IN ('BUY', 'SELL')
        GROUP BY action
        ORDER BY action
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def ensure_news_tickers_column(conn):
    """Ergänzt die Spalte news.tickers, falls sie noch fehlt (für bestehende DBs,
    in denen init.sql nicht erneut läuft)."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS tickers TEXT[]")


# -------------------------------------------------------------------------
# Watchlist: vom Nutzer im Dashboard manuell hinzugefügte Werte
# -------------------------------------------------------------------------
def ensure_watchlist_table(conn):
    """Legt die watchlist-Tabelle an, falls sie fehlt (bestehende DBs)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                ticker   TEXT PRIMARY KEY,
                added_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


def add_watchlist_ticker(conn, ticker: str):
    """Fügt einen Wert zur Watchlist hinzu (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO watchlist (ticker) VALUES (%s) ON CONFLICT (ticker) DO NOTHING",
            (ticker,))


def remove_watchlist_ticker(conn, ticker: str):
    """Entfernt einen Wert aus der Watchlist."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM watchlist WHERE ticker = %s", (ticker,))


def fetch_watchlist(conn) -> list[str]:
    """Alle manuell hinzugefügten Werte (alphabetisch)."""
    with conn.cursor() as cur:
        cur.execute("SELECT ticker FROM watchlist ORDER BY ticker")
        return [r[0] for r in cur.fetchall()]


# -------------------------------------------------------------------------
# Research: historischer Kursspeicher (prices_daily) für Backtests/ML.
# Point-in-time OHLCV je (ticker, day); auto_adjust=True → adj. Kurse.
# -------------------------------------------------------------------------
def ensure_prices_daily_table(conn):
    """Legt die prices_daily-Tabelle an (idempotent). Als TimescaleDB-
    Hypertable, falls die Extension vorhanden ist – sonst normale Tabelle."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prices_daily (
                ticker TEXT NOT NULL,
                day    DATE NOT NULL,
                open   DOUBLE PRECISION,
                high   DOUBLE PRECISION,
                low    DOUBLE PRECISION,
                close  DOUBLE PRECISION,
                volume DOUBLE PRECISION,
                PRIMARY KEY (ticker, day)
            )
        """)
        try:
            cur.execute(
                "SELECT create_hypertable('prices_daily', 'day', "
                "if_not_exists => TRUE, migrate_data => TRUE)")
        except Exception:
            pass   # ohne TimescaleDB einfach normale Tabelle
        cur.execute("CREATE INDEX IF NOT EXISTS idx_prices_daily_day "
                    "ON prices_daily (day DESC)")


def upsert_prices_daily(conn, rows: list[tuple]):
    """Bulk-Upsert von (ticker, day, open, high, low, close, volume)."""
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO prices_daily
                (ticker, day, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (ticker, day) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high,
                low = EXCLUDED.low, close = EXCLUDED.close,
                volume = EXCLUDED.volume
        """, rows)


def fetch_prices_history(conn, tickers: list[str] = None,
                         start=None, end=None):
    """Holt OHLCV-Historie als Liste von Dicts (sortiert ticker, day).
    Optional gefiltert auf `tickers` und Zeitraum [start, end]."""
    clauses, params = [], []
    if tickers:
        clauses.append("ticker = ANY(%s)")
        params.append(list(tickers))
    if start:
        clauses.append("day >= %s")
        params.append(start)
    if end:
        clauses.append("day <= %s")
        params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticker, day, open, high, low, close, volume "
            "FROM prices_daily" + where + " ORDER BY ticker, day", params)
        cols = ("ticker", "day", "open", "high", "low", "close", "volume")
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def prices_daily_coverage(conn):
    """Übersicht: je Ticker (Anzahl Tage, erster, letzter Tag) – zum Prüfen
    des Backfills."""
    with conn.cursor() as cur:
        cur.execute("SELECT ticker, COUNT(*), MIN(day), MAX(day) "
                    "FROM prices_daily GROUP BY ticker ORDER BY ticker")
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]


# -------------------------------------------------------------------------
# Feature-Store (features_daily): point-in-time Merkmale je (ticker, day).
# Spalten zentral hier definiert, damit Builder + Reader nie auseinanderlaufen.
# -------------------------------------------------------------------------
FEATURE_COLUMNS = [
    "r_21", "r_63", "r_126", "r_252", "mom_12_1",
    "vol_20", "vol_60", "rsi_14",
    "sma20_dist", "sma50_dist", "dist_52w_high", "vol_ratio",
]


def ensure_features_table(conn):
    cols = ",\n                ".join(f"{c} DOUBLE PRECISION" for c in FEATURE_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS features_daily (
                ticker TEXT NOT NULL,
                day    DATE NOT NULL,
                {cols},
                PRIMARY KEY (ticker, day)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_features_daily_day "
                    "ON features_daily (day)")


def upsert_features(conn, rows: list[tuple]):
    """Bulk-Upsert. rows = (ticker, day, <FEATURE_COLUMNS in Reihenfolge>)."""
    if not rows:
        return
    cols = ["ticker", "day"] + FEATURE_COLUMNS
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in FEATURE_COLUMNS)
    sql = (f"INSERT INTO features_daily ({', '.join(cols)}) VALUES %s "
           f"ON CONFLICT (ticker, day) DO UPDATE SET {set_clause}")
    with conn.cursor() as cur:
        execute_values(cur, sql, rows)


def ensure_shadow_tables(conn):
    """Tabellen für den Shadow-Modus: Ziel-Portfolio je Rebalancing + die
    hypothetische (nicht gehandelte) Equity-Kurve des systematischen Kerns."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shadow_portfolio (
                rebal_day DATE PRIMARY KEY,
                regime_on BOOLEAN NOT NULL,
                tickers   TEXT[] NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shadow_equity (
                day       DATE PRIMARY KEY,
                level     DOUBLE PRECISION NOT NULL,
                ret       DOUBLE PRECISION,
                exposure  DOUBLE PRECISION,
                n_holdings INTEGER
            )
        """)


def upsert_shadow_portfolio(conn, rebal_day, regime_on: bool, tickers: list[str]):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO shadow_portfolio (rebal_day, regime_on, tickers) "
            "VALUES (%s, %s, %s) ON CONFLICT (rebal_day) DO UPDATE SET "
            "regime_on = EXCLUDED.regime_on, tickers = EXCLUDED.tickers",
            (rebal_day, regime_on, list(tickers)))


def latest_shadow_portfolio(conn):
    """(rebal_day, regime_on, tickers) der jüngsten Rebalancierung oder None."""
    with conn.cursor() as cur:
        cur.execute("SELECT rebal_day, regime_on, tickers FROM shadow_portfolio "
                    "ORDER BY rebal_day DESC LIMIT 1")
        return cur.fetchone()


def count_shadow_rebalances(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM shadow_portfolio")
        return int(cur.fetchone()[0])


def upsert_shadow_equity(conn, day, level: float, ret: float,
                         exposure: float, n_holdings: int):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO shadow_equity (day, level, ret, exposure, n_holdings) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (day) DO UPDATE SET "
            "level = EXCLUDED.level, ret = EXCLUDED.ret, "
            "exposure = EXCLUDED.exposure, n_holdings = EXCLUDED.n_holdings",
            (day, level, ret, exposure, n_holdings))


def latest_shadow_equity(conn):
    """(day, level) des jüngsten Equity-Punkts oder None."""
    with conn.cursor() as cur:
        cur.execute("SELECT day, level FROM shadow_equity ORDER BY day DESC LIMIT 1")
        return cur.fetchone()


def fetch_shadow_equity(conn):
    """Komplette Shadow-Equity-Kurve (aufsteigend) für die Anzeige."""
    with conn.cursor() as cur:
        cur.execute("SELECT day, level, ret, exposure, n_holdings "
                    "FROM shadow_equity ORDER BY day")
        return cur.fetchall()


def fetch_features(conn, tickers: list[str] = None, start=None, end=None):
    """Features als Liste von Dicts (ticker, day, <features>)."""
    clauses, params = [], []
    if tickers:
        clauses.append("ticker = ANY(%s)"); params.append(list(tickers))
    if start:
        clauses.append("day >= %s"); params.append(start)
    if end:
        clauses.append("day <= %s"); params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    cols = ["ticker", "day"] + FEATURE_COLUMNS
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM features_daily{where} "
                    f"ORDER BY day, ticker", params)
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# -------------------------------------------------------------------------
# Trim-State: welche Gewinn-Stufen (+25/+50/+100%) je Position schon
# abverkauft wurden, damit nicht mehrfach getrimmt wird.
# -------------------------------------------------------------------------
def ensure_trim_table(conn):
    """Legt die trim_state-Tabelle an, falls sie fehlt."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trim_state (
                symbol      TEXT PRIMARY KEY,        -- IB-Symbol (ohne Suffix)
                base_qty    DOUBLE PRECISION,        -- Basis-Stückzahl beim Erstkontakt
                levels_done TEXT NOT NULL DEFAULT '' -- z.B. '0.25,0.5'
            )
        """)


def get_trim_state(conn, symbol: str):
    """(base_qty, set(levels_done)) für ein Symbol; (None, set()) wenn unbekannt."""
    with conn.cursor() as cur:
        cur.execute("SELECT base_qty, levels_done FROM trim_state WHERE symbol = %s",
                    (symbol,))
        row = cur.fetchone()
    if not row:
        return None, set()
    base_qty, done = row
    levels = {float(x) for x in done.split(",") if x.strip()}
    return base_qty, levels


def set_trim_state(conn, symbol: str, base_qty: float, levels_done: set):
    """Speichert Basis-Stückzahl + erledigte Stufen für ein Symbol."""
    done = ",".join(str(x) for x in sorted(levels_done))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trim_state (symbol, base_qty, levels_done) VALUES (%s, %s, %s) "
            "ON CONFLICT (symbol) DO UPDATE SET base_qty = EXCLUDED.base_qty, "
            "levels_done = EXCLUDED.levels_done",
            (symbol, base_qty, done))


def clear_trim_state(conn, symbol: str):
    """Entfernt den Trim-State (z.B. wenn die Position vollständig zu ist)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM trim_state WHERE symbol = %s", (symbol,))


# -------------------------------------------------------------------------
# Settings: im Dashboard live einstellbare Strategie-Parameter (Key/Value)
# -------------------------------------------------------------------------
def ensure_settings_table(conn):
    """Legt die settings-Tabelle an (Schlüssel/Wert), falls sie fehlt."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def get_settings_raw(conn) -> dict:
    """Alle gespeicherten Overrides als {key: value(str)}."""
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        return {k: v for k, v in cur.fetchall()}


def set_settings(conn, items: dict):
    """Schreibt/aktualisiert mehrere Einstellungen (Werte als String)."""
    if not items:
        return
    with conn.cursor() as cur:
        for k, v in items.items():
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (k, str(v)))


# -------------------------------------------------------------------------
# Research-Cache: per Gemini erzeugte Deep-Dive-/Risiko-Reports
# -------------------------------------------------------------------------
def ensure_research_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS research (
                ticker     TEXT NOT NULL,
                kind       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (ticker, kind)
            )
        """)


def save_research(conn, ticker: str, kind: str, content: str):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research (ticker, kind, content, created_at) "
            "VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (ticker, kind) DO UPDATE SET "
            "content = EXCLUDED.content, created_at = EXCLUDED.created_at",
            (ticker, kind, content))


def fetch_research(conn, limit: int = 20) -> list[dict]:
    """Zuletzt erzeugte Reports (neueste zuerst) für die Anzeige."""
    sql = """
        SELECT ticker, kind, content, created_at
        FROM research ORDER BY created_at DESC LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_research_one(conn, ticker: str, kind: str):
    """(content, created_at) eines gecachten Reports oder None."""
    with conn.cursor() as cur:
        cur.execute("SELECT content, created_at FROM research "
                    "WHERE ticker = %s AND kind = %s", (ticker, kind))
        return cur.fetchone()


# -------------------------------------------------------------------------
# Deep-Dive-/Risiko-Berichte mit HISTORIE (mehrere je Aktie, einzeln löschbar).
# Getrennt vom research-Cache (newsbrief), damit jeder Lauf erhalten bleibt.
# -------------------------------------------------------------------------
def ensure_reports_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id         BIGSERIAL PRIMARY KEY,
                ticker     TEXT NOT NULL,
                kind       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reports_ticker "
                    "ON reports (ticker, created_at DESC)")
        # Einmalige Migration: bestehende Deep-Dives/Risiko aus research
        # übernehmen, falls reports noch leer ist.
        cur.execute("SELECT COUNT(*) FROM reports")
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO reports (ticker, kind, content, created_at) "
                "SELECT ticker, kind, content, created_at FROM research "
                "WHERE kind IN ('deepdive','risk')")


def save_report(conn, ticker: str, kind: str, content: str):
    """Hängt einen neuen Bericht an (überschreibt NICHT – Historie)."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO reports (ticker, kind, content) "
                    "VALUES (%s, %s, %s)", (ticker, kind, content))


def fetch_reports(conn, limit: int = 100) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, ticker, kind, content, created_at FROM reports "
                    "ORDER BY created_at DESC LIMIT %s", (limit,))
        cols = ("id", "ticker", "kind", "content", "created_at")
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def delete_report(conn, report_id: int):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reports WHERE id = %s", (report_id,))


def delete_reports_for(conn, ticker: str):
    """Löscht ALLE Berichte einer Aktie (ganzer Ordner)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reports WHERE ticker = %s", (ticker,))


def update_sentiment(conn, news_id, published_at, sentiment,
                     sectors: list[str], tickers: list[str] | None = None):
    """Schreibt Sentiment + Sektoren + Ticker zurück und markiert die News als
    verarbeitet. sentiment darf None sein (z.B. wenn kein Score ermittelbar)."""
    sql = """
        UPDATE news
        SET sentiment = %s, sectors = %s, tickers = %s, processed = TRUE
        WHERE id = %s AND published_at = %s
    """
    with conn.cursor() as cur:
        # psycopg2 wandelt eine Python-Liste automatisch in ein Postgres-Array um
        cur.execute(sql, (sentiment, sectors, tickers or [],
                          news_id, published_at))


def fetch_recent_tickers(conn, hours: int = 24) -> list[str]:
    """Alle Ticker, zu denen es in den letzten `hours` Stunden News gab.
    Dient der Signal-Engine, um zusätzlich zur Kernliste auch frisch
    besprochene Werte zu handeln."""
    sql = """
        SELECT DISTINCT unnest(tickers) AS ticker
        FROM news
        WHERE processed = TRUE
          AND tickers IS NOT NULL
          AND published_at >= now() - make_interval(hours => %s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (hours,))
        return [row[0] for row in cur.fetchall() if row[0]]


def get_ticker_sentiment(conn, ticker: str, hours: int = 24,
                         min_count: int = 3, half_life_hours: float = 8.0):
    """Aktualitäts-gewichteter Sentiment-Score für einen Ticker über die
    letzten `hours` Stunden: neuere Headlines zählen mehr (exponentielle
    Halbwertszeit `half_life_hours`). Gibt (gewichteter_avg, count) zurück;
    avg ist None bei zu wenig Daten (< min_count)."""
    sql = """
        SELECT
            SUM(sentiment * w) / NULLIF(SUM(w), 0) AS wavg,
            COUNT(*) AS n
        FROM (
            SELECT sentiment,
                   power(0.5, EXTRACT(EPOCH FROM (now() - published_at))
                              / 3600.0 / %s) AS w
            FROM news
            WHERE processed = TRUE
              AND sentiment IS NOT NULL
              AND %s = ANY(tickers)
              AND published_at >= now() - make_interval(hours => %s)
        ) t
    """
    with conn.cursor() as cur:
        cur.execute(sql, (half_life_hours, ticker, hours))
        wavg, count = cur.fetchone()
    if not count or count < min_count:
        return None, (count or 0)
    return float(wavg), count


# -------------------------------------------------------------------------
# Katalysatoren: per LLM (Gemini) aus News extrahierte Event-Signale.
# -------------------------------------------------------------------------
def fetch_news_for_scan(conn, hours: int = 48, universe: list = None,
                        per_ticker: int = 6, max_total: int = 140) -> dict:
    """Jüngste Headlines je Ticker (für den LLM-Katalysator-Scan).
    Gibt {ticker: [(title, sentiment, 'YYYY-MM-DD HH:MM'), …]} zurück."""
    sql = """
        SELECT t AS ticker, n.title, n.sentiment, n.published_at
        FROM news n, unnest(n.tickers) AS t
        WHERE n.published_at >= now() - make_interval(hours => %s)
          AND n.title IS NOT NULL
        ORDER BY n.published_at DESC
    """
    out, total = {}, 0
    uni = set(universe) if universe else None
    with conn.cursor() as cur:
        cur.execute(sql, (hours,))
        for ticker, title, sent, pa in cur.fetchall():
            if uni is not None and ticker not in uni:
                continue
            lst = out.setdefault(ticker, [])
            if len(lst) >= per_ticker:
                continue
            ts = pa.strftime("%Y-%m-%d %H:%M") if pa else ""
            lst.append((title, sent, ts))
            total += 1
            if total >= max_total:
                break
    return out


def ensure_catalysts_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalysts (
                id         BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                ticker     TEXT NOT NULL,
                ctype      TEXT,
                direction  TEXT,                 -- bullish / bearish / neutral
                conviction DOUBLE PRECISION,      -- 0..1
                horizon    TEXT,
                rationale  TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_catalysts_created "
                    "ON catalysts (created_at DESC)")


def insert_catalysts(conn, rows: list[tuple]):
    """rows = (ticker, ctype, direction, conviction, horizon, rationale)."""
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(cur,
            "INSERT INTO catalysts (ticker, ctype, direction, conviction, "
            "horizon, rationale) VALUES %s", rows)


def fetch_recent_catalysts(conn, hours: int = 96, limit: int = 40) -> list[dict]:
    """Neuester Katalysator je Ticker (innerhalb `hours`), nach Überzeugung."""
    sql = """
        SELECT DISTINCT ON (ticker) ticker, ctype, direction, conviction,
               horizon, rationale, created_at
        FROM catalysts
        WHERE created_at >= now() - make_interval(hours => %s)
        ORDER BY ticker, created_at DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (hours,))
        cols = ("ticker", "ctype", "direction", "conviction", "horizon",
                "rationale", "created_at")
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    rows.sort(key=lambda d: (d.get("conviction") or 0), reverse=True)
    return rows[:limit]


def ensure_catalyst_daily_table(conn):
    """Täglicher signierter Katalysator-Score je Ticker (Forward-Feature-Log
    fürs spätere ML: +Überzeugung bullish, −Überzeugung bearish, 0 neutral)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS catalyst_daily (
                day    DATE NOT NULL,
                ticker TEXT NOT NULL,
                score  DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (day, ticker)
            )
        """)


def upsert_catalyst_daily(conn, day, scores: dict):
    if not scores:
        return
    rows = [(day, t, float(s)) for t, s in scores.items()]
    with conn.cursor() as cur:
        execute_values(cur,
            "INSERT INTO catalyst_daily (day, ticker, score) VALUES %s "
            "ON CONFLICT (day, ticker) DO UPDATE SET score = EXCLUDED.score",
            rows)


def fetch_catalyst_daily(conn, start=None):
    """(day, ticker, score)-Liste für die ML-Feature-Verknüpfung."""
    sql = "SELECT day, ticker, score FROM catalyst_daily"
    params = []
    if start:
        sql += " WHERE day >= %s"; params.append(start)
    sql += " ORDER BY day, ticker"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


# -------------------------------------------------------------------------
# Trading-/News-Kalender: anstehende Events (per Gemini), mit Auswirkung.
# -------------------------------------------------------------------------
def ensure_calendar_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id         BIGSERIAL PRIMARY KEY,
                event_date TEXT,
                title      TEXT,
                category   TEXT,
                scope      TEXT,
                impact     TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


def replace_calendar(conn, rows: list[tuple]):
    """Ersetzt den Kalender komplett (forward-looking, je Scan neu).
    rows = (event_date, title, category, scope, impact)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM calendar_events")
        if rows:
            execute_values(cur,
                "INSERT INTO calendar_events (event_date, title, category, "
                "scope, impact) VALUES %s", rows)


def fetch_calendar(conn, limit: int = 30) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT event_date, title, category, scope, impact "
                    "FROM calendar_events ORDER BY event_date, id LIMIT %s",
                    (limit,))
        cols = ("event_date", "title", "category", "scope", "impact")
        return [dict(zip(cols, r)) for r in cur.fetchall()]
