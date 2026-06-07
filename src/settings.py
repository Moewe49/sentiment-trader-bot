"""src/settings.py
Live einstellbare Strategie-Parameter.

Defaults kommen aus .env/config (bzw. festen Werten). Overrides werden im
Dashboard (⚙) gesetzt und in der DB-Tabelle 'settings' gespeichert. Die
Signal-Engine lädt die effektiven Werte zu Beginn JEDES Durchlaufs – Änderungen
greifen also ab dem nächsten stündlichen Lauf, ohne Neustart.
"""
import os

from .config import config
from .db import ensure_settings_table, get_settings_raw

# key -> (Label, Typ, Default)
#   Typen: "bool" | "int" | "float" | "pct" (Anteil 0..1, im UI als % gezeigt)
SCHEMA: dict = {
    "TRADING_ENABLED":    ("Handel aktiv (Kill-Switch)", "bool",  config.TRADING_ENABLED),
    "BUY_THRESHOLD":      ("Kauf-Schwelle (Score >)",    "float", 0.30),
    "SELL_THRESHOLD":     ("Verkauf-Schwelle (Score <)", "float", -0.30),
    "POSITION_SIZE":      ("Positionsgröße",             "pct",   0.08),
    "MAX_OPEN_POSITIONS": ("Max. offene Positionen",     "int",   config.MAX_OPEN_POSITIONS),
    "STOP_LOSS_PCT":      ("Stop-Loss",                  "pct",   config.STOP_LOSS_PCT),
    "TAKE_PROFIT_PCT":    ("Take-Profit",                "pct",   config.TAKE_PROFIT_PCT),
    "SMA_BAND":           ("SMA-Pufferzone (Hysterese)", "pct",   float(os.getenv("SMA_BAND", "0.02"))),
    "MAX_DAILY_LOSS":     ("Tages-Verlustlimit",         "pct",   config.MAX_DAILY_LOSS),
    "CYCLE_MINUTES":      ("Prüf-Takt (Minuten)",        "int",   config.CYCLE_MINUTES),
    # Fundamental-Filter (Default aus, damit Momentum-Logik unberührt bleibt)
    "FUND_FILTER":        ("Bewertungs-Filter aktiv",    "bool",  False),
    "MIN_REV_GROWTH":     ("Min. Umsatzwachstum",        "pct",   0.0),
    "MAX_VALUE_GROWTH":   ("Max. Value/Growth (0=aus)",  "float", 0.0),
    "EARNINGS_GUARD_DAYS": ("Earnings-Schutz (Tage, 0=aus)", "int", 0),
    # Entry-Filter gegen das Kaufen überdehnter Werte (ARM/DELL-Problem)
    "MAX_RSI_BUY":        ("Kauf nur wenn RSI <",        "float", 72.0),
    "MAX_SMA_DIST":       ("Max. Abstand über SMA",      "pct",   0.12),
    # Trailing-Stop volatilitätsbasiert: max(Stop-Loss, Mult × Tagesvola); 0 = fix
    "STOP_VOL_MULT":      ("Stop = Vola ×",              "float", 4.0),
    # Buy & Hold: bei kurzem Negativ-Score NICHT verkaufen, sondern durch den
    # Dip halten – der (breite) Trailing-Stop übernimmt den Exit.
    "HOLD_THROUGH_DIPS":  ("Durch Dips halten (kein Score-Verkauf)", "bool", True),
    # Gewinne stufenweise mitnehmen: bei +25/+50/+100 % je TRIM_FRACTION verkaufen.
    "TRIM_ENABLED":       ("Gewinne stufenweise mitnehmen", "bool", True),
    "TRIM_FRACTION":      ("Trim je Stufe (Anteil)",     "pct",   0.25),
    # Systematischer Kern LIVE: handelt das validierte Momentum-+-Regime-Ziel
    # (monatliches Rebalancing, keine Einzel-Stops). Aus = Altlogik aktiv.
    "SYSTEMATIC_LIVE":    ("Systematischer Kern LIVE", "bool", False),
    # Einmal-Schalter: beim nächsten US-Open EINMAL aufs Ziel umschichten
    # (live, mit korrektem Cash-Sequencing). Setzt sich danach selbst zurück.
    "SYSTEMATIC_DEPLOY":  ("Initial-Deploy beim nächsten Open", "bool", False),
}


def _cast(typ: str, raw, default):
    """Wandelt einen gespeicherten String in den passenden Typ; bei Fehler Default."""
    try:
        if typ == "bool":
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "an")
        if typ == "int":
            return int(float(raw))
        return float(raw)          # "float" und "pct" sind intern Fließkomma
    except (TypeError, ValueError):
        return default


def load_settings(conn) -> dict:
    """Effektive Einstellungen: DB-Override über Default. Immer vollständig."""
    raw = {}
    try:
        ensure_settings_table(conn)
        raw = get_settings_raw(conn)
    except Exception:
        pass
    out = {}
    for key, (label, typ, default) in SCHEMA.items():
        out[key] = _cast(typ, raw[key], default) if key in raw else default
    return out
