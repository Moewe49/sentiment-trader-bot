# Sentiment Trader

Vollständiger Paper-Trading-Bot, der komplett in Docker läuft:

- **RSS-Aggregator** zieht laufend Finanz-News (englisch + deutsch) in die DB.
- **Sentiment-Worker** bewertet jede Headline (FinBERT für EN, deutsches Modell
  für DE) und ordnet sie konkreten Tickern und Sektoren zu.
- **Signal-Engine** kombiniert Sentiment + Technik (SMA20 + RSI14) und handelt
  **stündlich während der US-Börsenzeit** Einzelaktien (NVDA, AMD, AAPL …) und
  Sektor-ETFs.
- **IB Gateway** läuft mit **automatischem Login** als eigener Container —
  kein manuelles Einloggen mehr nötig.
- **Dashboard** unter http://localhost:8080 zeigt Portfolio + Signal-Historie.

Alles ist auf **kostenlos/Paper-Trading** ausgelegt: lokale Modelle auf der CPU,
yfinance für Kursdaten, IBKR Paper-Account.

---

## Voraussetzungen (einmalig installieren)

1. **Docker Desktop** — https://www.docker.com/products/docker-desktop/
   Nach Installation prüfen:
   ```bash
   docker --version
   docker compose version
   ```
2. **Git** (optional, für Versionierung) — https://git-scm.com/

Mehr brauchst du nicht. Python läuft komplett im Container.

---

## Schritt 1 — IBKR Paper-Konto

> Freischaltung dauert 1–3 Tage. Das Konto bleibt kostenlos.

1. Auf https://www.interactivebrokers.eu Konto eröffnen (privat).
2. Nach Freischaltung im Client Portal **Paper Trading Account** aktivieren
   (Settings → Paper Trading Account).
3. Benutzername + Passwort des Paper-Accounts notieren — die trägst du gleich
   in die `.env` ein. **Mehr nicht**: Das IB Gateway läuft als Container und
   loggt sich damit selbst ein. Du musst es **nicht** auf dem PC installieren.

---

## Schritt 2 — Projekt einrichten (5 Min)

1. Entpacke/kopiere diesen Ordner an einen festen Ort, z.B. `~/sentiment-trader`.
2. `.env` aus der Vorlage erstellen und ausfüllen:
   ```bash
   cp .env.example .env
   ```
   In der `.env` setzen:
   - `DB_PASSWORD` — ein eigenes, sicheres Passwort für die Datenbank.
   - `TWS_USERID` / `TWS_PASSWORD` — Zugangsdaten deines IBKR-**Paper**-Accounts.
     Damit loggt sich das IB Gateway automatisch ein.
   - `TRADING_MODE=paper` (Standard) — niemals versehentlich `live`.

   > **Sicherheit:** Die `.env` enthält Klartext-Passwörter. Niemals committen
   > oder weitergeben. `.env` steht in `.gitignore`.

---

## Schritt 3 — Alles starten (1 Befehl)

```bash
docker compose up -d --build
```

Das passiert dabei (alle Container, `restart: unless-stopped`):
- `db`         — TimescaleDB, `db/init.sql` legt `news` + `signals` an
- `redis`      — Cache (für spätere Erweiterungen)
- `ib-gateway` — IB Gateway mit Auto-Login (IBC), Paper-API im Docker-Netz
- `rss`        — liest alle 60s die Feeds
- `sentiment`  — bewertet neue News (FinBERT / deutsches Modell)
- `signal`     — handelt stündlich während der US-Börsenzeit
- `executor`   — schickt Orders an IB Gateway
- `evaluator`  — bewertet alte Signale nach (Forward-Returns, Trefferquote)
- `dashboard`  — http://localhost:8080

> **Erster Start dauert länger:** Das `ib-gateway`-Image und die Sentiment-
> Modelle (~440 MB je Modell) werden einmalig heruntergeladen.

**Logs live ansehen** (hier siehst du, wie News reinkommen):
```bash
docker compose logs -f rss
```
Erwartete Ausgabe pro Durchlauf:
```
reuters_business       |  20 gelesen |  20 neu
ntv_wirtschaft         |  30 gelesen |  12 neu
...
Durchlauf fertig: 87 neue Artikel gesamt.
```
Beim zweiten Durchlauf stehen die meisten auf `0 neu` — das ist korrekt,
die Dedup-Logik greift.

---

## Schritt 4 — In die Datenbank schauen

```bash
docker compose exec db psql -U trader -d sentiment_trader
```
Dann z.B.:
```sql
-- Wie viele News insgesamt?
SELECT count(*) FROM news;

-- Die 10 neuesten Headlines:
SELECT published_at, source, title
FROM news ORDER BY published_at DESC LIMIT 10;

-- News pro Quelle:
SELECT source, count(*) FROM news GROUP BY source ORDER BY 2 DESC;
```
Mit `\q` verlässt du psql.

---

## Schritt 5 — Stoppen / Aufräumen

```bash
docker compose down            # stoppt alles, Daten BLEIBEN erhalten
docker compose down -v         # stoppt + LÖSCHT alle Daten (Volumes)
```

---

## Projektstruktur

```
sentiment-trader/
├── docker-compose.yml      # Orchestriert alle 8 Services
├── Dockerfile              # Python-Image für die Worker
├── requirements.txt        # Python-Abhängigkeiten
├── .env.example            # Vorlage für Secrets (kopieren -> .env)
├── db/
│   └── init.sql            # Legt news-Hypertable + signals-Tabelle an
├── scripts/                # Unsichtbarer Autostart (Windows)
│   ├── start-bot.ps1       # Wartet auf Docker, fährt Container hoch
│   └── start-bot-hidden.vbs# Startet das PS1-Skript ohne Fenster
└── src/
    ├── config.py           # Liest Einstellungen aus .env
    ├── settings.py         # Live im Dashboard einstellbare Strategie-Parameter
    ├── feeds.py            # Kuratierte RSS-Quellenliste
    ├── sectors.py          # Sektor-ETFs + Stichwörter
    ├── universe.py         # Einzelaktien-Ticker + News-Tagging + Kernliste
    ├── markets.py          # Börsen-Mapping (Suffix→Währung/Zeit) für Auslandswerte
    ├── fundamentals.py     # Bewertungskennzahlen + Value/Growth (yfinance)
    ├── research.py         # Gemini Deep-Dive/Risiko-Reports (optional)
    ├── db.py               # DB-Verbindung, News, Signale, Ticker-Sentiment
    ├── rss_aggregator.py   # Liest Feeds (Endlosschleife)
    ├── sentiment_worker.py # Bewertet News (FinBERT / deutsches Modell)
    ├── indicators.py       # SMA/RSI/technischer Score (Live + Backtest)
    ├── signal_engine.py    # Stündliche Trade-Signale + Risk-Management
    ├── backtest.py         # Strategie auf historischen Kursen testen
    ├── evaluator.py        # Bewertet Signale nach (Forward-Returns, Trefferquote)
    ├── executor.py         # Order-Anbindung ans IB Gateway (inkl. Bracket-Orders)
    └── dashboard.py        # Web-Dashboard (Port 8080)
```

---

## Modul 2 — Sentiment-Worker (NEU)

Bewertet die gesammelten News mit **FinBERT** und ordnet sie Sektoren zu.
Läuft als eigener Container (`sentiment`) automatisch mit.

- **Sentiment-Wert**: -1.0 (sehr bearish) ... +1.0 (sehr bullish), landet in der Spalte `sentiment`
- **Sektoren**: `XLK` (Tech & Chips), `XLE` (Energie & Öl), `XLF` (Finanzen & Banken), landet in `sectors`
- **Sprache**: Englische Quellen bewertet FinBERT, deutsche Quellen ein eigenes
  deutsches Modell (`oliverguhr/german-sentiment-bert`). Beide laufen lokal auf
  der CPU und werden beim ersten Gebrauch automatisch heruntergeladen.

Ergebnisse ansehen (in der DB):
```bash
docker compose exec db psql -U trader -d sentiment_trader -c \
"SELECT sentiment, sectors, title FROM news WHERE sectors <> '{}' ORDER BY published_at DESC LIMIT 15;"
```

Sektoren/Stichwörter anpassen: einfach `src/sectors.py` bearbeiten und
`docker compose up -d --build` erneut ausführen.

---

## Backtesting (kostenlos, ohne Broker)

Testet die **technische** Strategie (Trend + RSI) auf historischen Kursdaten
von yfinance – exakt dieselbe Score-Logik wie der Live-Bot (`src/indicators.py`).
So siehst du, ob die Basis trägt, bevor echtes (Paper-)Geld läuft. Das Sentiment
fließt hier bewusst nicht ein (dafür bräuchte es jahrelange Headline-Historie).

```bash
# Standard: XLK, XLE, XLF über 5 Jahre
docker compose run --rm signal python -m src.backtest

# eigene Auswahl + Robustheits-Checks
docker compose run --rm signal python -m src.backtest --tickers SPY QQQ --period 10y --mc --wf
docker compose run --rm signal python -m src.backtest --buy 0.3 --sell -0.3 --slippage-bps 5
```

Ausgabe: Rendite vs. Buy & Hold, CAGR, max. Drawdown, Sharpe, Anzahl Trades,
Trefferquote – pro Ticker und als gleichgewichtetes Portfolio.

**Robustheit (gegen Selbstbetrug):**
- `--slippage-bps` – realistische Handelskosten je Wechsel (Default 5 bp = 0,05 %).
- `--mc` – **Monte-Carlo**: zieht die Trades viele Male mit Zurücklegen und zeigt
  die Streuung von Endrendite und Drawdown + Verlustwahrscheinlichkeit
  („Können oder Glück?").
- `--wf` – **Walk-Forward**: Schwellen rollierend auf ~9 Monaten tunen, auf den
  folgenden ~3 (ungesehenen) Monaten testen. Das ist die **ehrlichste** Zahl,
  weil nur auf nie-gesehenen Daten gemessen wird (deckt Overfitting auf).

---

## Dashboard

Live-Portfolio + zuletzt erzeugte Signale unter http://localhost:8080
(startet automatisch mit `docker compose up`). Zeigt die Signal-Historie aus
der DB auch dann, wenn das IB Gateway gerade nicht verbunden ist.

- **Positions- und Signal-Liste** sind **filterbar (nach Symbol) und scrollbar**.
- **Eigene Werte** lassen sich direkt hinzufügen (Watchlist) – siehe unten.
- **Kontowert-Verlauf:** Equity-Kurve (Inline-SVG) oben – zeigt, ob der Bot über
  die Zeit Geld macht (stündlicher Snapshot in Tabelle `equity_curve`).
- **Fundamental-Vergleich:** Peer-Tabelle (P/S, EV/EBITDA, Bruttomarge,
  Umsatzwachstum, Value/Growth) aus yfinance. Diese Kennzahlen werden auch pro
  Signal als ML-Features geloggt und können als optionaler **Bewertungs-Filter**
  + **Earnings-Schutz** aktiviert werden (⚙ Einstellungen).
- **Deep-Dive-Research (Gemini, optional):** Ticker eintippen → Knopf „Deep Dive"
  oder „Risiko" erzeugt per Gemini (Google AI Studio, Free-Tier) einen
  qualitativen Bericht (Geschäftsmodell, Moat, Katalysatoren, Asymmetrie bzw.
  Skeptiker-Risiko). Ergebnisse werden gecacht. Aktiv, sobald `GEMINI_API_KEY`
  in der `.env` gesetzt ist; Modell wird automatisch gewählt (`GEMINI_MODEL`,
  Default `gemini-2.5-flash`).
- **⚙ Einstellungen** (Zahnrad oben rechts): die wichtigsten Strategie-Parameter
  **live im Dashboard** ändern – Kauf-/Verkauf-Schwelle, Positionsgröße,
  max. Positionen, Stop-Loss/Take-Profit, SMA-Pufferzone, Tages-Verlustlimit und
  der **Kill-Switch** (Handel an/aus). Änderungen landen in der DB-Tabelle
  `settings` und greifen ab dem nächsten stündlichen Durchlauf – **kein Neustart,
  kein `.env`-Editieren nötig**. Fehlt ein Wert, gilt der Default aus `.env`/Code.

---

## Wann & was wird gehandelt?

- **Takt:** Die Signal-Engine läuft **stündlich**, aber nur während der
  regulären US-Börsenzeit (Mo–Fr, 9:30–16:00 New Yorker Zeit). Außerhalb
  passiert nichts. Pro Stunde maximal ein Durchlauf.
- **Universum:** Feste Kernliste (`src/universe.py` → `CORE_TICKERS`:
  NVDA, AMD, AAPL, MSFT, GOOGL, AMZN, META, TSLA, AAOI, XLK, XLE, XLF)
  **plus** alle Ticker, zu denen es in den letzten 24 h News gab.
- **Entscheidung pro Wert:** kombinierter Score aus 50 % Sentiment + 50 %
  Technik (SMA20 + RSI14).
  - Score > **+0.30** → BUY
  - Score < **−0.30** → SELL (nur zum Schließen, kein Leerverkauf)
  - sonst → HOLD (nichts tun)
- **Pufferzone gegen Whipsaw (Hysterese):** Der Trend-Anteil zählt erst als
  bullish/bearish, wenn der Kurs **deutlich** über/unter dem SMA20 liegt
  (±`SMA_BAND`, Default 2 %). Pendelt der Kurs nur knapp um die Linie, bleibt der
  Trend neutral → kein Hin-und-Her-Handeln. Über `.env` (`SMA_BAND`) einstellbar.
- **Positionsgröße:** 8 % des verfügbaren Kapitals pro Position
  (`POSITION_SIZE`, im Dashboard ⚙ einstellbar).

### Mehrere Börsen / Auslandsaktien

Der Bot handelt **weltweit** – nicht nur US. Ein Wert wird über sein
yfinance-Kürzel geführt; **US ohne Suffix**, Ausland **mit Börsen-Suffix**:

| Beispiel | Börse | Währung |
|---|---|---|
| `NVDA` | USA | USD |
| `SIVE.ST` | Stockholm | SEK |
| `SAP.DE` | XETRA | EUR |
| `SHEL.L` | London | GBP |
| `7203.T` | Tokio | JPY |

Unterstützte Suffixe stehen in [`src/markets.py`](src/markets.py) (`.ST .DE .L
.PA .AS .SW .MI .MC .TO .HK .T .AX` u. a.). Der Bot leitet daraus Währung,
Handelszeiten und das IB-Routing ab:
- **Handelszeiten pro Börse:** jeder Wert wird nur zu **seinen** Börsenzeiten
  gehandelt (Stockholm ≠ New York). Die Engine läuft stündlich, solange
  *irgendeine* Börse offen ist.
- **Positionsgröße in Fremdwährung:** das EUR-Budget wird per Wechselkurs
  (z. B. `EURSEK=X`) in die Aktienwährung umgerechnet.
- **Dashboard:** alle Positionen werden in **EUR** angezeigt (je Position über
  ihren eigenen Wechselkurs).

> **Grenzen:** (1) Für manche Auslandsbörsen braucht dein IBKR-Konto eine (oft
> kostenlose, aber zu aktivierende) **Marktdaten-Subscription** – sonst kann IB
> Orders ablehnen. (2) **Sentiment** gibt es nur für Werte mit Stichwörtern in
> `universe.py` (v. a. US/DE-News); Auslandswerte laufen sonst **nur auf Technik**.

**Neue Werte handelbar machen – zwei Wege:**

1. **Schnell, im Dashboard (empfohlen):** Unter „Eigene Werte handeln" das
   Kürzel eintippen (z. B. `NVDA`) und auf „Hinzufügen" klicken. Der Wert landet
   in der Watchlist (DB-Tabelle `watchlist`) und wird ab dem **nächsten
   stündlichen Durchlauf** mitbewertet und -gehandelt. Mit dem `×` am Chip
   wieder entfernen. Kein Neustart nötig.
2. **Dauerhaft im Code:** in `src/universe.py` bei `STOCK_KEYWORDS` eintragen
   (Ticker → Stichwörter, damit auch News automatisch zugeordnet werden) und ggf.
   in `CORE_TICKERS` aufnehmen, dann `docker compose up -d --build`.

> Das gehandelte Universum ist die Vereinigung aus `CORE_TICKERS`, frisch
> besprochenen News-Tickern **und** der Dashboard-Watchlist.

---

## Lernen & Erfolgskontrolle (Outcome-Tracking)

Damit der Bot beim Papertraden nicht nur handelt, sondern auch **messbar wird,
ob seine Signale richtig lagen**, läuft der `evaluator`-Container mit. Er hängt
an jedes gespeicherte Signal nachträglich an, was der Kurs danach gemacht hat:

- **Forward-Return** nach 1 und 5 Börsentagen (Tagesschlusskurse von yfinance).
- **Treffer ja/nein**: BUY gilt als Treffer, wenn der Kurs gestiegen ist,
  SELL, wenn er gefallen ist.

Die Auswertung erscheint im **Dashboard** unter „Strategie-Performance"
(Trefferquote + Ø-Return je Aktion) und in der Signal-Tabelle als „Return 5T".
Erste Zahlen gibt es ~1 Tag nach dem ersten Signal; der 5-Tage-Wert nach gut
einer Woche.

> Das ist die Datengrundlage fürs spätere Tuning der Schwellen bzw. ein
> ML-Modell: ein sauber gelabelter Datensatz aus Signal-Features + tatsächlichem
> Ergebnis. Solange wenig bewertete Signale vorliegen, sind die Quoten aber
> wenig aussagekräftig — erst Daten sammeln.

**ML-Rohmerkmale:** Zu jedem Signal werden zusätzlich die Eingangsgrößen
gespeichert (`rsi`, `sma_dist`, `volatility`, `n_headlines` in der `signals`-
Tabelle). Zusammen mit `return_1d/5d` ergibt das den Trainingsdatensatz für ein
späteres Modell (z. B. LightGBM/Logistic Regression mit Walk-forward-Validation).

Direkt in der DB nachsehen:
```bash
docker compose exec db psql -U trader -d sentiment_trader -c \
"SELECT action, count(*), round(avg((hit::int))*100) AS trefferquote_pct,
        round(avg(return_5d)::numeric*100,2) AS avg_5d_pct
 FROM signals WHERE return_1d IS NOT NULL GROUP BY action;"
```

---

## Risk-Management (wichtig vor echtem Geld)

Die Order-Ausführung ist **positions-bewusst** und durch mehrere Limits
abgesichert (alles über `.env` einstellbar, sinnvolle Defaults):

| Schutz | Verhalten | `.env`-Variable (Default) |
|---|---|---|
| Kein Nachkauf | Bei bestehender Position wird **nicht** weiter zugekauft | — |
| Kein Leerverkauf | SELL **schließt nur** eine vorhandene Position, eröffnet nie einen Short | — |
| Exposure-Cap | Max. Anzahl gleichzeitig offener Positionen | `MAX_OPEN_POSITIONS=10` |
| Trailing-Stop / Take-Profit | Jeder Kauf wird abgesichert: **Trailing-Stop** (zieht mit steigendem Kurs nach) + Take-Profit, als OCA-Gruppe | `STOP_LOSS_PCT=0.08`, `TAKE_PROFIT_PCT=0.15` |
| Tages-Verlustlimit | Fällt der Kontowert an einem Tag zu stark, stoppt der Handel bis zum nächsten US-Handelstag | `MAX_DAILY_LOSS=0.05` |
| Kill-Switch | Schaltet **jede** Order ab, ohne Container zu stoppen | `TRADING_ENABLED=yes` |

**Not-Aus zur Laufzeit:** In der `.env` `TRADING_ENABLED=no` setzen und
`docker compose up -d signal` — der Bot erzeugt dann weiter Signale (und der
Evaluator lernt weiter), platziert aber keine Orders mehr.

> **Hinweis Bracket-Orders:** Die Stop-/Take-Profit-Klammer wird bei IB
> serverseitig überwacht (kein eigener Monitoring-Loop). Bitte beim ersten Lauf
> im Dashboard / über VNC kurz verifizieren, dass die Klammern korrekt anliegen,
> bevor du auf ein Echtgeld-Konto wechselst.

---

## Automatisch & unsichtbar starten, wenn der PC angeht

Der Bot startet bei jedem Login von selbst – **komplett im Hintergrund, ohne
sichtbares Fenster**. Das ist bereits eingerichtet:

1. **Docker Desktop startet automatisch** (Engine), aber **ohne** dass sich das
   Dashboard-Fenster öffnet. Gesetzt in `%APPDATA%\Docker\settings-store.json`
   (`AutoStart: true`, `OpenUIOnStartupDisabled: true`) plus eine minimierte
   Autostart-Verknüpfung als Absicherung.
2. **Versteckter Start des Bots:** Im Autostart-Ordner liegt
   `SentimentTraderBot.lnk`. Diese ruft `scripts\start-bot-hidden.vbs` auf, die
   wiederum `scripts\start-bot.ps1` **ohne jegliches Fenster** ausführt. Das
   Skript wartet, bis die Docker-Engine bereit ist, und fährt dann alle Container
   hoch (`docker compose up -d`).
3. **Container bleiben oben:** Alle Services haben `restart: unless-stopped`.
4. **IB Gateway** loggt sich dank IBC selbst ein (Zugangsdaten aus der `.env`).
5. **Dashboard öffnet sich automatisch:** Sobald der Webserver antwortet, öffnet
   das Startskript http://localhost:8080 im Standardbrowser. Abschaltbar über die
   Umgebungsvariable `OPEN_DASHBOARD=0`.

> **Sichtbar ist** nur das Dashboard (gewollt) und das kleine Docker-Symbol im
> System-Tray (unten rechts bei der Uhr) – kein Konsolenfenster, kein Aufblitzen.
> Ein vollständiges Ausblenden des Tray-Symbols ist bei Docker Desktop nicht
> vorgesehen.

**Erster Start dauert einige Minuten** (unsichtbar): Beim allerersten Mal baut
`docker compose` die Images. Danach geht es schnell. Nach **Code-Änderungen**
einmalig manuell `docker compose up -d --build` ausführen.

**Was lief beim Start?** (unsichtbares Skript schreibt ein Log)
```powershell
Get-Content "C:\Users\nikla\Downloads\sentiment-trader_4\sentiment-trader\scripts\startup.log"
```

**Status / Logs der Container prüfen:**
```bash
docker compose ps                 # läuft alles?
docker compose logs -f signal     # was handelt die Engine gerade?
docker compose logs -f ib-gateway # Login erfolgreich?
```

**Autostart wieder abschalten:** die beiden Verknüpfungen aus dem Autostart-
Ordner löschen (`Win`+`R` → `shell:startup`) – `SentimentTraderBot.lnk` und
`Docker Desktop (Hintergrund).lnk`. Den Handel allein stoppst du sanfter über
den Kill-Switch (`TRADING_ENABLED=no`, siehe Risk-Management).

---

## Was als Nächstes kommt (noch NICHT gebaut)

- `truthsocial.py` — Trump-Posts via `curl_cffi` / ScrapeCreators
- Auswertung der gespeicherten Signale (Trefferquote der echten Live-Signale)

---

## Troubleshooting

| Problem | Lösung |
|---|---|
| `db`-Container startet nicht | Port 5432 schon belegt? In docker-compose auf `5433:5432` ändern |
| Ein Feed zeigt immer `0 gelesen` | Feed-URL in `src/feeds.py` veraltet — im Browser prüfen, ggf. ersetzen |
| Änderung am Code wird nicht aktiv | `docker compose up -d --build` (mit `--build`!) |
| `init.sql` lief nicht | Greift nur bei LEEREM Volume. `docker compose down -v` und neu starten |
| Dashboard zeigt nur Nullen | IB Gateway noch nicht eingeloggt. `docker compose logs ib-gateway` prüfen; der Bot verbindet sich automatisch neu |
| IB Gateway loggt sich nicht ein | `TWS_USERID`/`TWS_PASSWORD` in `.env` korrekt? Paper-Account verwendet? `TRADING_MODE=paper`? |
| Falsche Handelszeit / Engine läuft nie | Engine nutzt US-Börsenzeit (New York). Container läuft in UTC – das ist korrekt, `tzdata` rechnet um |
| Keine Käufe trotz Signal / „kein Kurs" | yfinance-Rate-Limit. Behoben durch Sammel-Abruf (1 Request/Lauf), `curl_cffi` (Browser-Tarnung) und Kurs-Cache. Bei anhaltendem Limit kurz warten – der Cache überbrückt einzelne Aussetzer |
| Zu viele Positionen auf einmal | `MAX_OPEN_POSITIONS` (Default 10) und `POSITION_SIZE` (8 %) in `.env`/Code senken |
| Ins Gateway reinschauen | In `.env` `VNC_SERVER_PASSWORD` setzen, neu starten, dann VNC-Client auf `localhost:5900` |
| Bot startet nicht beim Boot | `scripts\startup.log` prüfen. Liegen die Verknüpfungen in `shell:startup`? Steht in Docker-Desktop `AutoStart`? |
| Trades trotz Start nicht | `TRADING_ENABLED=yes`? Börse offen (15:30–22:00 DE)? IB Gateway eingeloggt? `TWS_PASSWORD` gesetzt? |
