# Research-Notizen: systematische Strategie (Phase 1)

Ehrlich validierte Befunde aus der Harness (`src/research/`). Universum:
60 Liquiditäts-Aktien (`RESEARCH_UNIVERSE`), 10 Jahre Tagesdaten, Kosten 10 bps,
purged/embargoed Out-of-Sample, kein Look-ahead.

## Werkzeuge
```
python -m src.research.ingest   --backfill|--update|--coverage   # D1 Kursspeicher
python -m src.research.features --build                          # D2 Features
python -m src.research.labels   --stats                          # D3 Labels
python -m src.research.eval     --score <feat> [--regime] [--vol-target x]  # D4/D5/D7
python -m src.research.ml       --hold 21 --top-q 0.3            # D6 ML vs Momentum
```

## Befunde

### 1. Momentum funktioniert, Mean-Reversion nicht (D5)
- 12-1-Momentum & 6-Monats-Momentum schlagen den EW-Markt klar (Sharpe ~1.3–1.5).
- Kurzfrist-RSI-Mean-Reversion ist **schlechter** als simples Halten (Sharpe 0.92)
  bei 95 % Turnover. Verworfen.

### 2. ML (GBM) schlägt simples Momentum NICHT (D6)
- OOS Information Coefficient: Momentum 0.031 | **Momentum-Blend 0.038** | ML-GBM **0.002**.
- Das GBM fand auf reinen Preis-Features keinen generalisierbaren Zusatz-Edge
  (overfittet, höherer Turnover, schlechterer Drawdown).
- **Lehre:** Feature-Qualität > Modellkomplexität. ML lohnt erst mit
  *orthogonalen* Daten (Fundamentals-Historie, Sentiment, Makro, Katalysator/LLM),
  nicht mit einem größeren Modell. Kein Tortur-Tuning (= Overfitting).

### 3. Regime-Filter drittelt den Drawdown (D7)
Score = **Momentum-Blend** (Mittel der perzentilisierten Ränge von
r_63/r_126/r_252/mom_12_1), Top 30 %, monatlich:

| Variante | CAGR | Sharpe | MaxDD |
|---|---|---|---|
| pur | 39.9 % | 1.44 | −31.0 % |
| **+ Regime (Markt < SMA200 → Cash)** | **38.4 %** | **1.51** | **−20.7 %** |
| + Vol-Ziel 18 % | 32.6 % | 1.38 | −24.6 % |
| + Regime & Vol-Ziel | 32.9 % | 1.44 | −19.0 % |

→ **Regime-Filter ist der klare Gewinn** (Sharpe ↑, DD ⅓ kleiner, kaum CAGR-Kosten).
Vol-Targeting kostet zu viel Rendite (kein Hebel erlaubt) → optional/aus.

## Empfohlener systematischer Kern (Stand Phase 1)
**Momentum-Blend, Top 30 % gleichgewichtet, monatliches Rebalancing,
+ SMA200-Regime-Filter (in Abschwüngen Cash).** Benchmark, den künftiges ML
out-of-sample schlagen muss: **Sharpe ~1.51, MaxDD ~−21 %**.

## Wichtige Einschränkung
Universum = heute existierende, handverlesene Gewinner → **Survivorship Bias**.
Absolute Renditen (z. B. +2474 %) sind dadurch zu optimistisch. Belastbar ist der
**relative** Vorsprung ggü. EW-Markt + die **Risikoverbesserung** durch den
Regime-Filter (mechanisch robust, regime-unabhängig). Vor echter Geldnutzung:
point-in-time-Universum inkl. delisteter Werte ergänzen.

## Offene nächste Schritte
- **Schritt 3 (braucht Nutzer-Entscheidung):** den Kern live (Paper) schalten →
  ehrlicher Forward-Track-Record.
- **Schritt 4 (braucht GEMINI_API_KEY):** Katalysator-/LLM-Eimer + News-/
  Trading-Kalender — *das* ist die orthogonale Information, die ML später
  sinnvoll machen könnte.
