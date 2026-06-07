"""src/config.py
Zentrale Konfiguration. Liest alle Einstellungen aus Umgebungsvariablen (.env).
So liegen niemals Passwörter oder API-Keys im Code.
"""
import os


class Config:
    # --- Datenbank ---------------------------------------------------------
    # Im Docker-Netz heißt der DB-Host 'db' (= Service-Name in docker-compose).
    # Lokal (ohne Docker) auf 'localhost' setzen via .env.
    DB_HOST: str = os.getenv("DB_HOST", "db")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_USER: str = os.getenv("DB_USER", "trader")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "changeme")
    DB_NAME: str = os.getenv("DB_NAME", "sentiment_trader")

    # --- RSS-Aggregator ----------------------------------------------------
    # Wie oft alle Feeds neu abgefragt werden (Sekunden).
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "60"))
    # User-Agent: höflich gegenüber den Feed-Servern, vermeidet manche Blocks.
    USER_AGENT: str = os.getenv(
        "USER_AGENT",
        "SentimentTraderBot/0.1 (+research; contact: you@example.com)",
    )

    # --- Sentiment-Worker --------------------------------------------------
    # Wartezeit (Sekunden), wenn gerade keine neuen News zu verarbeiten sind.
    SENTIMENT_INTERVAL: int = int(os.getenv("SENTIMENT_INTERVAL", "30"))

    @property
    def db_dsn(self) -> str:
        """Postgres-Verbindungsstring für psycopg2."""
        return (
            f"host={self.DB_HOST} port={self.DB_PORT} "
            f"user={self.DB_USER} password={self.DB_PASSWORD} "
            f"dbname={self.DB_NAME}"
        )

    # --- IBKR / IB Gateway ------------------------------------------------
    # host.docker.internal = der Windows-PC, auf dem IB Gateway läuft
    IBKR_HOST: str = os.getenv("IBKR_HOST", "host.docker.internal")
    IBKR_PORT: int = int(os.getenv("IBKR_PORT", "4001"))
    IBKR_CLIENT_ID: int = int(os.getenv("IBKR_CLIENT_ID", "1"))

    # --- Sentiment-Worker --------------------------------------------------
    SENTIMENT_INTERVAL: int = int(os.getenv("SENTIMENT_INTERVAL", "30"))

    # --- Signal-Engine -----------------------------------------------------
    # Prüf-Takt in Minuten (wie oft das Universum bewertet/gehandelt wird).
    # Take-Profit/Trailing-Stop liegen broker-seitig und feuern ohnehin in
    # Echtzeit – das hier betrifft Einstiege + signalbasierte Ausstiege.
    CYCLE_MINUTES: int = max(1, int(os.getenv("CYCLE_MINUTES", "15")))

    # --- Gemini (optionales Deep-Dive-Research) ---------------------------
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() \
        or "gemini-2.5-flash"

    # --- Risk-Management / Kill-Switch ------------------------------------
    # Globaler Not-Aus: auf "no"/"0"/"false" setzen, um JEDE Order zu
    # unterbinden, ohne den Container zu stoppen. Default: Handel erlaubt.
    TRADING_ENABLED: bool = os.getenv("TRADING_ENABLED", "yes").lower() \
        in ("1", "true", "yes")
    # Höchstzahl gleichzeitig offener Positionen (Diversifikation / Exposure-Cap).
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
    # Tages-Verlustlimit als Anteil des Kontowerts. Wird er an einem Tag
    # überschritten, handelt der Bot bis Mitternacht (NY) nicht mehr. 0 = aus.
    MAX_DAILY_LOSS: float = float(os.getenv("MAX_DAILY_LOSS", "0.05"))
    # Broker-seitige Absicherung pro Kauf (Bracket-Order). 0 = deaktiviert.
    STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.08"))   # -8 %
    TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "0.15"))  # +15 %


config = Config()
