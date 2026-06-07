"""src/indicators.py
Gemeinsame technische Indikatoren.

Diese Logik ist die EINZIGE Quelle der Wahrheit für den technischen Score.
Sowohl die Live-Signal-Engine (src/signal_engine.py) als auch das Backtesting
(src/backtest.py) benutzen sie – so können Backtest und Echtbetrieb niemals
auseinanderlaufen.

Score-Definition (Wert pro Handelstag, -1.0 .. +1.0):
  - Trend: Kurs deutlich über 20-Tage-SMA → +1, deutlich darunter → -1,
    innerhalb einer Pufferzone (±SMA_BAND) → 0   (Gewicht 70%)
  - RSI(14): >70 überkauft → -0.5, <30 überverkauft → +0.5, sonst 0  (Gewicht 30%)

Die Pufferzone (Hysterese/Deadband) verhindert Whipsaw: Wenn der Kurs nur knapp
um den SMA pendelt, bleibt der Trend neutral und es entsteht kein Handelssignal.
Über die Umgebungsvariable SMA_BAND einstellbar (Default 2%).
"""
import os

import numpy as np
import pandas as pd

SMA_WINDOW = 20
RSI_PERIOD = 14
# Pufferzone um den SMA (Anteil). 0.02 = ±2%. 0 = harte Linie (altes Verhalten).
SMA_BAND = float(os.getenv("SMA_BAND", "0.02"))


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Relative Strength Index (0..100) als Zeitreihe."""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def technical_score_series(close: pd.Series,
                           sma_window: int = SMA_WINDOW,
                           rsi_period: int = RSI_PERIOD,
                           sma_band: float = SMA_BAND) -> pd.Series:
    """Technischer Score (-1..+1) für JEDEN Handelstag der Kursreihe.
    Während der Aufwärmphase (zu wenig Historie für SMA/RSI) ist der Wert NaN.
    `sma_band` ist die Pufferzone um den SMA gegen Whipsaw (Anteil, z.B. 0.02)."""
    close = close.astype(float)
    sma = close.rolling(sma_window).mean()
    r = rsi(close, rsi_period)

    # Deadband: erst deutlich über/unter dem SMA gilt der Trend als +1/-1,
    # innerhalb von ±sma_band bleibt er neutral (0) → kein Pendeln an der Linie.
    upper = sma * (1 + sma_band)
    lower = sma * (1 - sma_band)
    trend = pd.Series(
        np.where(close > upper, 1.0, np.where(close < lower, -1.0, 0.0)),
        index=close.index,
    )
    rsi_score = pd.Series(
        np.where(r > 70, -0.5, np.where(r < 30, 0.5, 0.0)),
        index=close.index,
    )

    score = trend * 0.7 + rsi_score * 0.3
    # Aufwärmphase ungültig machen, solange SMA oder RSI noch nicht definiert sind
    score[sma.isna() | r.isna()] = np.nan
    return score
