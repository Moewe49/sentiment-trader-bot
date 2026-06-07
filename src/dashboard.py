"""src/dashboard.py
Live-Übersicht des Paper-Portfolios.

Verbindet sich READ-ONLY mit dem IB Gateway, liest Positionen + Kontowerte
und schreibt daraus alle paar Sekunden eine HTML-Seite. Ein simpler Webserver
liefert sie unter http://localhost:8080 aus; die Seite lädt sich selbst
automatisch neu.

Greift NICHT in den Trading-Bot ein: eigene clientId (Default 7) und
readonly-Verbindung. IB Gateway erlaubt mehrere gleichzeitige API-Verbindungen,
solange jede eine andere clientId hat.
"""
import asyncio
import html as _html
import logging
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from ib_async import IB, Stock, MarketOrder

from .db import (get_connection, ensure_signals_table, fetch_recent_signals,
                 ensure_signals_outcome_columns, fetch_strategy_performance,
                 ensure_watchlist_table, fetch_watchlist,
                 add_watchlist_ticker, remove_watchlist_ticker,
                 ensure_settings_table, set_settings,
                 ensure_equity_table, insert_equity, fetch_equity,
                 fetch_latest_fundamentals,
                 ensure_research_table, save_research, fetch_research,
                 ensure_reports_table, save_report, fetch_reports, delete_report,
                 delete_reports_for,
                 ensure_realized_table, upsert_realized, fetch_realized_periods,
                 fetch_news_for_ticker, ensure_trades_table, upsert_fill, fetch_trades,
                 ensure_shadow_tables, fetch_shadow_equity,
                 latest_shadow_portfolio, count_shadow_rebalances,
                 ensure_catalysts_table, fetch_recent_catalysts,
                 ensure_calendar_table, fetch_calendar)
import json
from .settings import SCHEMA, load_settings
from .markets import yf_symbol_for, market_of, is_open, split_symbol
from . import deepdive as research
from .research.macro import correlations as macro_correlations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [dashboard] %(levelname)s: %(message)s",
)
log = logging.getLogger("dashboard")

# --- Konfiguration (kommt aus .env, hat aber sinnvolle Defaults) ----------
IB_HOST     = os.getenv("IBKR_HOST", "host.docker.internal")
IB_PORT     = int(os.getenv("IBKR_PORT", "4001"))
CLIENT_ID   = int(os.getenv("DASHBOARD_CLIENT_ID", "7"))
WEB_PORT    = int(os.getenv("DASHBOARD_PORT", "8080"))
REFRESH_SEC = int(os.getenv("DASHBOARD_REFRESH", "20"))
# eigene clientId für manuelle Verkäufe (≠ Engine/Executor/Dashboard-Stream)
SELL_CLIENT_ID = int(os.getenv("DASHBOARD_SELL_CLIENT_ID", "9"))
# Anzeige-Zeitzone (Container läuft in UTC) – Default deutsche Zeit
LOCAL_TZ    = ZoneInfo(os.getenv("DISPLAY_TZ", "Europe/Berlin"))

WEB_DIR  = "/app/web"
HTML_OUT = os.path.join(WEB_DIR, "index.html")

# Kontowerte, die wir aus dem Konto ziehen
WANT_TAGS = {"NetLiquidation", "TotalCashValue", "RealizedPnL", "UnrealizedPnL"}

# Gemeinsamer Zustand (IB-Daten kommen aus dem IB-Thread); die HTML-Seite wird
# sowohl periodisch als auch SOFORT nach jeder Änderung neu geschrieben.
_STATE = {"holdings": [], "account": {}, "fills": []}
_MACRO = {"data": [], "ts": 0.0}
_RENDER_LOCK = threading.Lock()
_CONN_HOLDER = {"conn": None}
_TABLES_READY = {"ok": False}


# -------------------------------------------------------------------------
# Formatierung (deutsches Zahlenformat: 1.234,56 €)
# -------------------------------------------------------------------------
def euro(x: float) -> str:
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

# Währungssymbole für die Positions-Anzeige (Handelswährung)
_CUR_SYM = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CHF": "CHF",
            "CAD": "C$", "AUD": "A$", "HKD": "HK$", "SEK": "SEK",
            "NOK": "NOK", "DKK": "DKK"}

def money(x: float, cur: str = "USD") -> str:
    """Betrag im deutschen Zahlenformat mit der jeweiligen Handelswährung."""
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {_CUR_SYM.get(cur, cur)}"


# yfinance-Suffix -> TradingView-Börsenpräfix (für den eingebetteten Chart)
_TV_EXCH = {"": "", "ST": "OMXSTO", "DE": "XETR", "F": "FWB", "L": "LSE",
            "PA": "EURONEXT", "AS": "EURONEXT", "BR": "EURONEXT", "MI": "MIL",
            "MC": "BME", "SW": "SIX", "VI": "VIE", "OL": "OSL", "CO": "OMXCOP",
            "HE": "OMXHEX", "LS": "EURONEXT", "TO": "TSX", "HK": "HKEX",
            "T": "TSE", "AX": "ASX"}


def tv_symbol(yf_sym: str) -> str:
    """Baut aus einem yfinance-Symbol das TradingView-Symbol (BÖRSE:TICKER)."""
    base, suf = split_symbol(yf_sym)
    pre = _TV_EXCH.get(suf, "")
    return (pre + ":" + base) if pre else base


def symlink(label: str, yf_sym: str) -> str:
    """Klickbarer Symbolname, der das Chart-Fenster öffnet."""
    base = split_symbol(yf_sym)[0]      # News sind unter dem reinen Ticker getaggt
    return (f'<span class="symlink" data-tv="{tv_symbol(yf_sym)}" '
            f'data-news="{base}" data-label="{label}">{label}</span>')


def market_display(ib_sym: str, currency: str) -> tuple:
    """(Börsenname, Handelszeit in DE-Zeit 'HH:MM–HH:MM', offen?) für ein Symbol.
    Die Börsen-Lokalzeiten werden in die Anzeige-Zeitzone (DE) umgerechnet."""
    from datetime import time as _time
    yf = yf_symbol_for(ib_sym, currency)
    m = market_of(yf)
    today = datetime.now(LOCAL_TZ).date()
    ex_tz = ZoneInfo(m.tz)

    def to_de(mins):
        dt = datetime.combine(today, _time(mins // 60, mins % 60), tzinfo=ex_tz)
        return dt.astimezone(LOCAL_TZ).strftime("%H:%M")

    return m.name, f"{to_de(m.open_min)}–{to_de(m.close_min)}", is_open(yf)

def pct(x: float) -> str:
    return f"{x:+.2f} %".replace(".", ",")

def stueck(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


# -------------------------------------------------------------------------
# Daten aus IB lesen
# -------------------------------------------------------------------------
def get_account(ib: IB) -> dict:
    """Kontowerte als {tag: float} – per Snapshot (accountSummary).
    Bewusst NICHT reqAccountUpdates/accountValues, weil dieser Stream nach
    Reconnects mit gleicher clientId hängt. accountSummary ist ein einfacher
    Request/Response (wie im Executor) und liefert die Werte in Kontowährung."""
    out: dict[str, float] = {}
    for row in ib.accountSummary():
        if row.tag in WANT_TAGS:
            try:
                out[row.tag] = float(row.value)
            except (TypeError, ValueError):
                pass
    return out


def get_holdings(ib: IB) -> list[dict]:
    """Offene Positionen per Snapshot. Kurs je Position holt IB direkt
    (funktioniert für jede Börse/Währung); alle Werte werden in EUR umgerechnet,
    passend zu den Konto-Kacheln. Fällt auf den Einstand zurück, wenn gerade
    kein Kurs verfügbar ist."""
    poss = [p for p in ib.positions() if p.position]
    if not poss:
        return []

    # yfinance-Symbol je Position rekonstruieren (US bleibt bare, Ausland mit
    # Suffix) und Kurse holen – zuverlässiger als IB-Marktdaten für Auslandswerte.
    yf_of = {}
    for p in poss:
        c = p.contract
        yf_of[c.conId] = yf_symbol_for(
            c.symbol, c.currency,
            getattr(c, "primaryExchange", "") or getattr(c, "exchange", ""))
    px_native = _yf_last_prices(sorted(set(yf_of.values())))

    # Offene Schutz-Orders je Symbol: Stop (TRAIL/STP) + Ziel (LMT)
    # reqAllOpenOrders → auch Orders anderer clientIds (Engine/Executor) sichtbar
    prot = {}
    try:
        for t in ib.reqAllOpenOrders():
            if t.order.action != "SELL":
                continue
            d = prot.setdefault(t.contract.symbol, {"stop": None, "target": None})
            ot = t.order.orderType
            if ot in ("TRAIL", "STP"):
                d["stop"] = t.order.trailStopPrice or t.order.auxPrice or d["stop"]
            elif ot == "LMT":
                d["target"] = t.order.lmtPrice or d["target"]
    except Exception as e:
        log.warning("Offene Orders (Dashboard) nicht lesbar: %s", e)

    holdings = []
    for p in poss:
        c = p.contract
        cur = c.currency or "USD"
        avg = p.avgCost or 0.0                      # in Handelswährung
        native = px_native.get(yf_of.get(c.conId))
        price = native if native else avg
        qty = p.position
        pr = prot.get(c.symbol, {})
        holdings.append({
            "symbol": c.symbol, "currency": cur, "qty": qty, "avg": avg,
            "price": price, "value": qty * price, "pnl": (price - avg) * qty,
            "stop": pr.get("stop"), "target": pr.get("target"),
            "protected": pr.get("stop") is not None,
        })
    return holdings


def get_fills(ib) -> list:
    """Heutige Ausführungen als einfache Dicts (für den persistenten Trade-Log)."""
    out = []
    try:
        for f in ib.fills():
            e, cr = f.execution, f.commissionReport
            out.append({
                "exec_id": e.execId, "ts": e.time, "symbol": f.contract.symbol,
                "side": "BUY" if e.side in ("BOT", "BUY") else "SELL",
                "qty": float(e.shares), "price": float(e.price),
                "currency": f.contract.currency,
                "realized": getattr(cr, "realizedPNL", None),
                "commission": getattr(cr, "commission", None),
            })
    except Exception as e:
        log.warning("Fills (Dashboard) nicht lesbar: %s", e)
    return out


def _yf_last_prices(symbols: list[str]) -> dict:
    """Letzte Schlusskurse (in Instrumentwährung) je yfinance-Symbol."""
    if not symbols:
        return {}
    import yfinance as yf
    out = {}
    try:
        data = yf.download(symbols, period="2d", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False, threads=False)
    except Exception as e:
        log.warning("Kursabruf (Dashboard) fehlgeschlagen: %s", e)
        return out
    for s in symbols:
        try:
            ser = (data["Close"] if len(symbols) == 1 else data[s]["Close"]).dropna()
            if len(ser):
                out[s] = float(ser.iloc[-1])
        except Exception:
            pass
    return out


def _fx_rates(currencies) -> dict:
    """Wechselkurse 'Einheiten je 1 EUR' (EUR{cur}=X via yfinance). EUR = 1.0."""
    rates = {"EUR": 1.0}
    need = sorted({c for c in currencies if c and c != "EUR"})
    if not need:
        return rates
    import yfinance as yf
    pairs = [f"EUR{c}=X" for c in need]
    try:
        data = yf.download(pairs, period="5d", interval="1d",
                           group_by="ticker", progress=False, threads=False)
        for c, pair in zip(need, pairs):
            try:
                s = (data["Close"] if len(pairs) == 1 else data[pair]["Close"]).dropna()
                if len(s):
                    rates[c] = float(s.iloc[-1])
            except Exception:
                pass
    except Exception as e:
        log.warning("FX-Abruf (Dashboard) fehlgeschlagen: %s", e)
    return rates


# -------------------------------------------------------------------------
# HTML bauen
# -------------------------------------------------------------------------
def render_signals(signals: list[dict]) -> str:
    """Baut die Tabelle der zuletzt erzeugten Signale (aus der DB)."""
    if not signals:
        return ('<tr><td colspan="7" class="empty">'
                'Noch keine Signale erzeugt.</td></tr>')

    rows = []
    for s in signals:
        ts = s["ts"].astimezone(LOCAL_TZ).strftime("%d.%m. %H:%M") if s.get("ts") else "—"
        ts_sort = s["ts"].timestamp() if s.get("ts") else ""
        action = s["action"]
        acls = {"BUY": "pos", "SELL": "neg"}.get(action, "")
        sent = f'{s["sentiment"]:+.2f}' if s.get("sentiment") is not None else "—"
        sent_sort = s["sentiment"] if s.get("sentiment") is not None else ""
        score = f'{s["score"]:+.2f}' if s.get("score") is not None else "—"
        score_sort = s["score"] if s.get("score") is not None else ""
        qty = stueck(s["quantity"]) if s.get("quantity") else "—"
        qty_sort = s.get("quantity") if s.get("quantity") else ""
        exec_mark = "✓" if s.get("executed") else "—"

        # Forward-Return (5 Tage) aus dem Outcome-Tracking, falls schon bewertet
        r5 = s.get("return_5d")
        if r5 is None:
            ret_html = '<td class="num" data-sort="">…</td>'
        else:
            rcls = "pos" if r5 >= 0 else "neg"
            ret_html = f'<td class="num {rcls}" data-sort="{r5}">{r5 * 100:+.2f} %</td>'

        rows.append(
            f'<tr data-sym="{s["etf"]}">'
            f'<td data-sort="{ts_sort}">{ts}</td>'
            f'<td class="sym">{symlink(s["etf"], s["etf"])}</td>'
            f'<td class="num {acls}">{action}</td>'
            f'<td class="num" data-sort="{score_sort}">{score}</td>'
            f'<td class="num" data-sort="{sent_sort}">{sent}</td>'
            f'<td class="num" data-sort="{qty_sort}">{qty} {exec_mark}</td>'
            f'{ret_html}'
            "</tr>"
        )
    return "\n".join(rows)


def render_performance(perf: list[dict]) -> str:
    """Baut die Karten zur Strategie-Performance (Trefferquote + Ø-Return)."""
    if not perf:
        return ('<div class="card"><div class="lbl">Performance</div>'
                '<div class="big">noch keine bewerteten Signale</div>'
                '<div class="sub">Auswertung startet ~1 Tag nach dem ersten Signal</div>'
                '</div>')

    cards = []
    for p in perf:
        hr = (p.get("hit_rate") or 0) * 100
        r5 = (p.get("avg_return_5d") or 0) * 100
        hr_cls = "pos" if hr >= 50 else "neg"
        r5_cls = "pos" if r5 >= 0 else "neg"
        cards.append(
            f'<div class="card"><div class="lbl">{p["action"]} · {p["n"]} bewertet</div>'
            f'<div class="big {hr_cls}">{hr:.0f} %</div>'
            f'<div class="sub">Trefferquote · Ø5T <span class="{r5_cls}">'
            f'{r5:+.2f} %</span></div></div>'
        )
    return "\n".join(cards)


def render_watchlist(watchlist: list[str]) -> str:
    """Baut die Chips der manuell hinzugefügten Werte (mit Entfernen-Knopf)."""
    if not watchlist:
        return '<span class="wl-empty">Noch keine eigenen Werte hinzugefügt.</span>'
    chips = []
    for t in watchlist:
        chips.append(
            '<form class="chip" method="post" action="/watchlist/remove">'
            f'<input type="hidden" name="ticker" value="{t}">'
            f'{symlink(t, t)}'
            '<button type="submit" title="Entfernen">&times;</button>'
            '</form>'
        )
    return "\n".join(chips)


def md_to_html(text: str) -> str:
    """Leichtgewichtiger Markdown→HTML-Renderer (für Gemini-Berichte):
    Überschriften, Aufzählungen, **fett** (farbig hervorgehoben), Links.
    Kein rohes Markdown mit Sternchen mehr im Dashboard."""
    out = []
    for ln in (text or "").split("\n"):
        s = ln.strip()
        e = _html.escape(s)
        if s.startswith("### "):
            out.append(f'<div class="mdh">{_html.escape(s[4:])}</div>')
        elif s.startswith("## "):
            out.append(f'<div class="mdh">{_html.escape(s[3:])}</div>')
        elif s.startswith("# "):
            out.append(f'<div class="mdh">{_html.escape(s[2:])}</div>')
        elif s.startswith(("- ", "* ", "• ")):
            out.append(f'<div class="mdli">{_html.escape(s[2:])}</div>')
        elif s == "":
            out.append('<div class="mdsp"></div>')
        else:
            out.append(f'<div class="mdp">{e}</div>')
    h = "\n".join(out)
    h = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", h)
    h = re.sub(r"__(.+?)__", r"<b>\1</b>", h)
    h = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', h)
    return h


# Tendenz → (CSS-Klasse, Label)
_STANCE_UI = {
    "bullish": ("buy", "▲ Bullish"),
    "bearish": ("sell", "▼ Bearish"),
    "neutral": ("neutral", "● Neutral"),
}
_STANCE_WORDS = (
    ("bullish", "bullish"), ("bullisch", "bullish"), ("optimistisch", "bullish"),
    ("bearish", "bearish"), ("bärisch", "bearish"), ("baerisch", "bearish"),
    ("pessimistisch", "bearish"),
    ("neutral", "neutral"),
)


def _stance(content: str) -> str:
    """Erkennt die Tendenz eines Berichts (bullish/bearish/neutral) – bevorzugt
    aus dem Fazit-Abschnitt, sonst aus dem ganzen Text."""
    t = (content or "").lower()
    seg = t.rsplit("fazit", 1)[-1] if "fazit" in t else t
    for kw, st in _STANCE_WORDS:
        if kw in seg:
            return st
    for kw, st in _STANCE_WORDS:
        if kw in t:
            return st
    return "neutral"


def _stance_badge(content: str) -> str:
    cls, label = _STANCE_UI[_stance(content)]
    return f'<span class="sbadge {cls}">{label}</span>'


# Risiko-Level → (CSS-Klasse, Label)
_RISK_UI = {
    "hoch":    ("sell", "⚠ Hohes Risiko"),
    "mittel":  ("neutral", "Mittleres Risiko"),
    "niedrig": ("buy", "Geringes Risiko"),
}
_RISK_WORDS = (
    ("hoch", "hoch"), ("high", "hoch"), ("erhöht", "hoch"),
    ("niedrig", "niedrig"), ("gering", "niedrig"), ("low", "niedrig"),
    ("mittel", "mittel"), ("medium", "mittel"), ("moderat", "mittel"),
)


def _risk_badge(content: str) -> str:
    """Risiko-Level aus dem Bericht (bevorzugt aus '## Risiko-Level')."""
    t = (content or "").lower()
    lvl = "mittel"
    if "risiko-level" in t:
        seg = t.split("risiko-level", 1)[1][:60]
        for kw, lv in _RISK_WORDS:
            if kw in seg:
                lvl = lv
                break
    cls, label = _RISK_UI[lvl]
    return f'<span class="sbadge {cls}">{label}</span>'


def _badge_for(r: dict) -> str:
    """Passendes Badge je Berichtsart: Risiko → Risiko-Level, sonst Tendenz."""
    if r.get("kind") == "risk":
        return _risk_badge(r.get("content"))
    return _stance_badge(r.get("content"))


def render_research(reports: list[dict]) -> str:
    """Deep-Dive-/Risiko-Reports (Gemini): Eingabeformular + gecachte Berichte."""
    if research.available():
        head = (
            '<form class="addform" method="post" action="/research">'
            '<input name="ticker" placeholder="z. B. NVDA" maxlength="12" '
            'autocomplete="off" spellcheck="false">'
            '<button name="kind" value="deepdive" type="submit">Deep Dive</button>'
            '<button name="kind" value="risk" type="submit">Risiko</button>'
            '</form>'
            '<div class="stamp">Erzeugt einen Bericht via Gemini (dauert ein paar '
            'Sekunden). Ergebnisse werden gecacht.</div>')
    else:
        head = ('<div class="stamp">Kein Gemini-Key aktiv. Setze '
                '<code>GEMINI_API_KEY</code> in der <code>.env</code> und starte '
                'den Stack neu, dann kannst du hier Deep Dives erzeugen.</div>')

    # Berichte je Aktie zu einem aufklappbaren "Ordner" gruppieren
    groups: dict = {}
    for r in reports:
        if r["kind"] not in research.KINDS:      # newsbrief etc. nicht hier
            continue
        groups.setdefault(r["ticker"], []).append(r)

    blocks = []
    for ticker in sorted(groups):
        items = sorted(groups[ticker],
                       key=lambda x: x.get("created_at") or datetime.min.replace(tzinfo=LOCAL_TZ),
                       reverse=True)
        inner = []
        for r in items:
            ts = r["created_at"].astimezone(LOCAL_TZ).strftime("%d.%m. %H:%M") if r.get("created_at") else ""
            label = research.KINDS.get(r["kind"], r["kind"])
            delbtn = (
                '<form class="rdel" method="post" action="/research/delete" '
                'onclick="event.stopPropagation()">'
                f'<input type="hidden" name="id" value="{r["id"]}">'
                '<button type="submit" title="Bericht löschen">&times;</button></form>')
            inner.append(
                f'<details class="report" data-rk="report:{r["id"]}">'
                f'<summary><span class="rkind">{label}</span> '
                f'<span class="muted">{ts}</span>{_badge_for(r)}'
                f'{delbtn}</summary>'
                f'<div class="md">{md_to_html(r["content"])}</div></details>')
        # Ordner zeigt BEIDES: neueste Deep-Dive-Tendenz + neuestes Risiko-Level
        newest_dd = next((x for x in items if x.get("kind") == "deepdive"), None)
        newest_risk = next((x for x in items if x.get("kind") == "risk"), None)
        fbadges = ""
        if newest_dd:
            fbadges += _stance_badge(newest_dd["content"])
        if newest_risk:
            fbadges += _risk_badge(newest_risk["content"])
        folder_del = (
            '<form class="rdel" method="post" action="/research/delete-folder" '
            'onclick="event.stopPropagation()" '
            'onsubmit="return confirm(\'Alle Berichte dieser Aktie löschen?\')">'
            f'<input type="hidden" name="ticker" value="{ticker}">'
            '<button type="submit" title="Ganzen Ordner löschen">&times;</button></form>')
        blocks.append(
            f'<details class="rfolder" data-rk="folder:{ticker}">'
            f'<summary><b>{ticker}</b> <span class="muted">· {len(items)} '
            f'Bericht{"e" if len(items) != 1 else ""}</span>'
            f'<span class="rmeta">{fbadges}{folder_del}</span></summary>'
            f'<div class="rinner">{"".join(inner)}</div></details>')
    body = "\n".join(blocks) if blocks else \
        '<div class="stamp">Noch keine Berichte erzeugt.</div>'
    return head + body


def render_fundamentals(rows: list[dict]) -> str:
    """Peer-/Relative-Valuation-Tabelle (P/S, EV/EBITDA, Marge, Wachstum,
    Value/Growth) – aus den zuletzt geloggten Fundamentaldaten je Symbol."""
    if not rows:
        return ('<tr><td colspan="6" class="empty">Noch keine Fundamentaldaten – '
                'kommen mit dem nächsten Durchlauf.</td></tr>')
    out = []
    for r in sorted(rows, key=lambda x: x["etf"]):
        ps, ev = r.get("ps_ttm"), r.get("ev_ebitda")
        gm, rg, vg = r.get("gross_margin"), r.get("rev_growth"), r.get("value_growth")
        # Value/Growth: niedriger = besser → grün, hoch → rot
        vg_cls = "" if vg is None else ("pos" if vg <= 0.5 else ("neg" if vg > 2 else ""))
        rg_cls = "" if rg is None else ("pos" if rg >= 0 else "neg")
        ds = lambda x: "" if x is None else x
        out.append(
            f'<tr data-sym="{r["etf"]}">'
            f'<td class="sym">{symlink(r["etf"], r["etf"])}</td>'
            f'<td class="num" data-sort="{ds(ps)}">{("%.2f" % ps) if ps is not None else "—"}</td>'
            f'<td class="num" data-sort="{ds(ev)}">{("%.1f" % ev) if ev is not None else "—"}</td>'
            f'<td class="num" data-sort="{ds(gm)}">{("%.1f %%" % (gm * 100)) if gm is not None else "—"}</td>'
            f'<td class="num {rg_cls}" data-sort="{ds(rg)}">{("%+.1f %%" % (rg * 100)) if rg is not None else "—"}</td>'
            f'<td class="num {vg_cls}" data-sort="{ds(vg)}">{("%.2f" % vg) if vg is not None else "—"}</td>'
            "</tr>"
        )
    return "\n".join(out)


def render_trades(trades: list[dict]) -> str:
    """Tabelle der tatsächlichen Ausführungen (Käufe/Verkäufe)."""
    if not trades:
        return ('<tr><td colspan="6" class="empty">Noch keine Trades '
                'aufgezeichnet (werden ab jetzt gesammelt).</td></tr>')
    out = []
    for t in trades:
        ts = t["ts"].astimezone(LOCAL_TZ).strftime("%d.%m. %H:%M") if t.get("ts") else "—"
        ts_sort = t["ts"].timestamp() if t.get("ts") else ""
        side = t["side"]
        scls = {"BUY": "pos", "SELL": "neg"}.get(side, "")
        cur = t.get("currency") or "USD"
        r = t.get("realized")
        rcls = "" if not r else ("pos" if r >= 0 else "neg")
        out.append(
            f'<tr data-sym="{t["symbol"]}">'
            f'<td data-sort="{ts_sort}">{ts}</td>'
            f'<td class="sym">{symlink(t["symbol"], t["symbol"])}</td>'
            f'<td class="num {scls}">{side}</td>'
            f'<td class="num" data-sort="{t.get("qty") or 0}">{stueck(t.get("qty") or 0)}</td>'
            f'<td class="num">{money(t.get("price") or 0, cur)}</td>'
            f'<td class="num {rcls}" data-sort="{r if r is not None else ""}">'
            f'{money(r, cur) if r else "—"}</td>'
            "</tr>"
        )
    return "\n".join(out)


def render_equity(points: list[dict]) -> str:
    """Inline-SVG-Liniendiagramm des Kontowerts (keine externe Bibliothek)."""
    if not points or len(points) < 2:
        return ('<div class="stamp">Noch zu wenig Datenpunkte – die Kurve '
                'füllt sich stündlich von selbst.</div>')
    vals = [p["net_liq"] for p in points]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    W, H, pad = 920, 170, 8
    n = len(vals)

    def px(i):
        return pad + i * (W - 2 * pad) / (n - 1)

    def py(v):
        return pad + (H - 2 * pad) * (1 - (v - lo) / rng)

    line = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
    area = f"{px(0):.1f},{H - pad:.1f} {line} {px(n - 1):.1f},{H - pad:.1f}"
    first, last = vals[0], vals[-1]
    up = last >= first
    color = "#1bc47d" if up else "#f6465d"
    chg = (last - first) / first * 100 if first else 0.0
    cls = "pos" if up else "neg"

    svg = (f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" '
           f'style="width:100%;height:{H}px;display:block">'
           f'<polyline points="{area}" fill="{color}22" stroke="none"/>'
           f'<polyline points="{line}" fill="none" stroke="{color}" '
           f'stroke-width="2" vector-effect="non-scaling-stroke"/></svg>')
    label = (f'<div class="stamp">Verlauf ({n} Punkte) · Start {euro(first)} → '
             f'aktuell {euro(last)} <span class="{cls}">({chg:+.2f} %)</span> · '
             f'Hoch {euro(hi)} / Tief {euro(lo)}</div>')
    return label + svg


def render_settings(s: dict) -> str:
    """Baut die Eingabefelder des Einstellungs-Panels aus dem Settings-Schema."""
    rows = []
    for key, (label, typ, default) in SCHEMA.items():
        val = s.get(key, default)
        if typ == "bool":
            on = "selected" if val else ""
            off = "" if val else "selected"
            field = (f'<select name="{key}">'
                     f'<option value="yes" {on}>An</option>'
                     f'<option value="no" {off}>Aus</option></select>')
            suffix = ""
        elif typ == "pct":
            field = (f'<input type="number" step="0.5" name="{key}" '
                     f'value="{round(float(val) * 100, 2)}">')
            suffix = '<span class="unit">%</span>'
        elif typ == "int":
            field = f'<input type="number" step="1" name="{key}" value="{int(val)}">'
            suffix = ""
        else:  # float
            field = (f'<input type="number" step="0.05" name="{key}" '
                     f'value="{round(float(val), 4)}">')
            suffix = ""
        rows.append(f'<label class="setrow"><span class="setlbl">{label}</span>'
                    f'<span class="setin">{field}{suffix}</span></label>')
    return "\n".join(rows)


def _spark(vals: list[float], w: int = 420, h: int = 46) -> str:
    """Kompakte Inline-SVG-Sparkline (ohne externe Bibliothek)."""
    if len(vals) < 2:
        return ('<div class="stamp" style="margin:8px 0 0">Kurve füllt sich ab '
                'dem zweiten Handelstag von selbst.</div>')
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    pad, n = 3, len(vals)
    pts = " ".join(f"{pad + i * (w - 2 * pad) / (n - 1):.1f},"
                   f"{pad + (h - 2 * pad) * (1 - (v - lo) / rng):.1f}"
                   for i, v in enumerate(vals))
    col = "#34d399" if vals[-1] >= vals[0] else "#fb7185"
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'style="width:100%;height:{h}px;display:block;margin-top:8px">'
            f'<polyline points="{pts}" fill="none" stroke="{col}" '
            f'stroke-width="2" vector-effect="non-scaling-stroke"/></svg>')


def render_shadow(curve: list, target, rebal_count: int) -> str:
    """Shadow-Track des systematischen Kerns: hypothetische Equity + Regime +
    aktuelles Ziel-Portfolio (klickbare Picks)."""
    if not target:
        return ('<div class="panelbox"><div class="lbl">Shadow-Kern</div>'
                '<div class="stamp">Wird mit dem nächsten täglichen Lauf der '
                'Engine berechnet (Momentum-Blend + SMA200-Regime).</div></div>')
    rebal_day, regime_on, tickers = target[0], bool(target[1]), list(target[2])
    badge = ('<span class="badge on"><i></i>Risk-On</span>' if regime_on
             else '<span class="badge off"><i></i>Risk-Off · Cash</span>')
    if curve:
        level = float(curve[-1][1])
        since = (level - 1.0) * 100
        cls = "pos" if since >= 0 else "neg"
        lvl_html = (f'<span class="shadowlvl {cls}">{level:.4f}</span>'
                    f'<span class="sub {cls}">{since:+.2f} % seit Start</span>')
        spark = _spark([float(p[1]) for p in curve])
    else:
        lvl_html = '<span class="shadowlvl">1,0000</span>'
        spark = _spark([])
    chips = "\n".join(
        f'<span class="pickchip">{symlink(t, yf_symbol_for(t, "USD"))}</span>'
        for t in tickers)
    left = (
        '<div class="panelbox">'
        '<div class="ptitle"><div class="lbl">Hypothetische Equity</div>'
        f'{badge}</div>'
        f'<div class="shadowrow">{lvl_html}</div>'
        f'{spark}'
        f'<div class="stamp" style="margin:12px 0 0">Rebalancings: {rebal_count} · '
        f'letzte Anpassung {rebal_day} · {len(curve)} Tage Track</div>'
        '</div>')
    right = (
        '<div class="panelbox">'
        f'<div class="ptitle"><div class="lbl">Ziel-Portfolio · {len(tickers)} '
        'Werte gleichgewichtet</div></div>'
        f'<div class="pickchips">{chips}</div>'
        '<div class="stamp" style="margin:12px 0 0">Top-30 %-Momentum-Rang · '
        'monatliches Rebalancing</div>'
        '</div>')
    return f'<div class="shadow">{left}{right}</div>'


def render_catalysts(cats: list[dict]) -> str:
    """LLM-Katalysatoren (Gemini): klickbares Symbol, Richtung, Überzeugung,
    Horizont, Begründung."""
    if not cats:
        return ('<div class="stamp">Noch keine Katalysatoren – der nächste '
                'tägliche LLM-Scan füllt das (1 Gemini-Call). Braucht aktuelle '
                'News + Gemini-Key.</div>')
    rows = []
    for c in cats:
        t = c["ticker"]
        d = (c.get("direction") or "neutral")
        bcls = {"bullish": "up", "bearish": "down"}.get(d, "flat")
        blabel = {"bullish": "▲ Bullish", "bearish": "▼ Bearish"}.get(d, "Neutral")
        conv = int(round((c.get("conviction") or 0) * 100))
        hor = _html.escape(c.get("horizon") or "")
        why = _html.escape(c.get("rationale") or "")
        rows.append(
            f'<div class="catrow"><span class="catsym">{symlink(t, t)}</span>'
            f'<span class="badge {bcls}">{blabel}</span>'
            f'<span class="catconv">{conv}%</span>'
            f'<span class="cathor">{hor}</span>'
            f'<span class="catwhy">{why}</span></div>')
    return '<div class="catlist">' + "\n".join(rows) + '</div>'


def render_calendar(events: list) -> str:
    """Trading-/News-Kalender (Gemini): Datum, Kategorie, Event, Auswirkung."""
    if not events:
        return ('<div class="stamp">Noch keine Kalender-Events – der nächste '
                'tägliche Scan füllt das (Gemini + News). Braucht Gemini-Key.</div>')
    ccls = {"Notenbank": "down", "Makro": "flat", "Earnings": "up"}
    rows = []
    for e in events:
        cat = e.get("category") or ""
        cls = ccls.get(cat, "flat")
        rows.append(
            f'<div class="calrow"><span class="caldate">{_html.escape(e.get("event_date") or "")}</span>'
            f'<span class="badge {cls}">{_html.escape(cat)}</span>'
            f'<span class="caltitle">{_html.escape(e.get("title") or "")}'
            f' <span class="nm">{_html.escape(e.get("scope") or "")}</span></span>'
            f'<span class="calwhy">{_html.escape(e.get("impact") or "")}</span></div>')
    return '<div class="callist">' + "\n".join(rows) + '</div>'


def render_macro(corrs: list) -> str:
    """Cross-Asset-Korrelations-Panel (Gold/Silber/Dollar/Anleihen)."""
    if not corrs:
        return ('<div class="stamp">Noch keine Makro-Daten – kommen mit dem '
                'nächsten Kurs-Update (GLD/SLV/UUP/TLT).</div>')
    rows = []
    for label, tkr, corr, n, why in corrs:
        if corr != corr:
            val, cls, w = "n/a", "flat", "0"
        else:
            cls = "up" if corr > 0.15 else ("down" if corr < -0.15 else "flat")
            val = f"{corr:+.2f}"
            w = str(int(min(100, abs(corr) * 100)))
        rows.append(
            f'<div class="macrorow"><span class="macrolbl">{label} '
            f'<span class="nm">({tkr})</span></span>'
            f'<span class="macrobar"><span class="macrofill {cls}" '
            f'style="width:{w}%"></span></span>'
            f'<span class="macroval {cls}">{val}</span>'
            f'<span class="macrowhy">{_html.escape(why)}</span></div>')
    return '<div class="macrolist">' + "\n".join(rows) + '</div>'


def render_html(holdings: list[dict], account: dict, signals: list[dict],
                perf: list[dict], watchlist: list[str], settings: dict,
                equity: list[dict], fundamentals: list[dict],
                reports: list[dict], periods: dict, trades: list[dict],
                shadow_curve: list, shadow_target, shadow_rebal: int,
                catalysts: list[dict], macro: list, calendar: list,
                refresh: int) -> str:
    rows = []
    for h in sorted(holdings, key=lambda x: x["symbol"]):
        qty, avg, price = h["qty"], h["avg"], h["price"]
        val, pnl = h["value"], h["pnl"]
        cur = h.get("currency", "USD")
        mname, mhours, mopen = market_display(h["symbol"], cur)
        cost = qty * avg
        ppct = (pnl / abs(cost) * 100) if cost else 0.0
        cls = "pos" if pnl >= 0 else "neg"
        n2 = lambda x: ("%.2f" % x).replace(".", ",") if x else "—"
        if h.get("protected"):
            sz_cell = (f'<td class="num" data-sort="{h.get("stop") or ""}" '
                       f'style="font-size:12px">{n2(h.get("stop"))} / {n2(h.get("target"))}</td>')
        else:
            sz_cell = ('<td class="num neg" data-sort="" '
                       'style="font-size:12px">⚠ kein Stop</td>')
        rows.append(
            f'<tr data-sym="{h["symbol"]}">'
            f'<td class="sym">{symlink(h["symbol"], yf_symbol_for(h["symbol"], cur))}</td>'
            f'<td class="num" data-sort="{qty}">{stueck(qty)}</td>'
            f'<td class="num" data-sort="{avg}">{money(avg, cur)}</td>'
            f'<td class="num" data-sort="{price}">{money(price, cur)}</td>'
            f'<td class="num" data-sort="{val}">{money(val, cur)}</td>'
            f'<td class="num {cls}" data-sort="{pnl}">{money(pnl, cur)}</td>'
            f'<td class="num {cls}" data-sort="{ppct}">{pct(ppct)}</td>'
            f'{sz_cell}'
            f'<td><button type="button" class="sellbtn" data-sym="{h["symbol"]}" '
            f'data-cur="{cur}" data-qty="{int(qty)}" data-price="{price}" '
            f'data-market="{mname}" data-hours="{mhours}" data-open="{1 if mopen else 0}">'
            'Verkaufen</button></td>'
            "</tr>"
        )

    if not rows:
        rows_html = '<tr><td colspan="9" class="empty">Noch keine offenen Positionen.</td></tr>'
    else:
        rows_html = "\n".join(rows)

    # Kennzahlen kommen direkt aus dem Konto-Snapshot (Kontowährung, z. B. EUR).
    net_liq   = account.get("NetLiquidation", 0.0)
    cash      = account.get("TotalCashValue", 0.0)
    invested  = max(net_liq - cash, 0.0)           # Marktwert der Positionen
    unreal    = account.get("UnrealizedPnL", 0.0)
    realized  = account.get("RealizedPnL", 0.0)
    inv_pct   = (unreal / invested * 100) if invested else 0.0
    pnl_cls   = "pos" if unreal >= 0 else "neg"

    # Realisiert-Kachel mit Umschaltung Tag/Woche/Monat/Jahr
    rc = lambda v: "pos" if v >= 0 else "neg"
    realized_card = (
        '<div class="card"><div class="lbl">Realisiert'
        '<span class="rtabs">'
        '<button type="button" class="rtab" data-p="day">Tag</button>'
        '<button type="button" class="rtab" data-p="week">Wo</button>'
        '<button type="button" class="rtab" data-p="month">Mon</button>'
        '<button type="button" class="rtab" data-p="year">Jahr</button>'
        '<button type="button" class="rtab" data-p="total">Ges</button>'
        '</span></div>'
        f'<div class="big" id="realizedVal"'
        f' data-day="{euro(periods["day"])}" data-dayc="{rc(periods["day"])}"'
        f' data-week="{euro(periods["week"])}" data-weekc="{rc(periods["week"])}"'
        f' data-month="{euro(periods["month"])}" data-monthc="{rc(periods["month"])}"'
        f' data-year="{euro(periods["year"])}" data-yearc="{rc(periods["year"])}"'
        f' data-total="{euro(periods["total"])}" data-totalc="{rc(periods["total"])}">…</div></div>'
    )

    summary = (
        f'<div class="card"><div class="lbl">Kontowert</div>'
        f'<div class="big">{euro(net_liq)}</div></div>'
        f'<div class="card"><div class="lbl">Investiert (Marktwert)</div>'
        f'<div class="big">{euro(invested)}</div></div>'
        f'<div class="card"><div class="lbl">Gewinn / Verlust (offen)</div>'
        f'<div class="big {pnl_cls}">{euro(unreal)}</div>'
        f'<div class="sub {pnl_cls}">{pct(inv_pct)}</div></div>'
        + realized_card +
        f'<div class="card"><div class="lbl">Cash frei</div>'
        f'<div class="big">{euro(cash)}</div></div>'
    )

    now = datetime.now(LOCAL_TZ).strftime("%d.%m.%Y  %H:%M:%S")

    page = PAGE_TEMPLATE
    page = page.replace("__REFRESH__", str(refresh))
    page = page.replace("__SUMMARY__", summary)
    page = page.replace("__ROWS__", rows_html)
    page = page.replace("__SIGNALS__", render_signals(signals))
    page = page.replace("__PERFORMANCE__", render_performance(perf))
    page = page.replace("__WATCHLIST__", render_watchlist(watchlist))
    page = page.replace("__SETTINGS__", render_settings(settings))
    page = page.replace("__SHADOW__", render_shadow(shadow_curve, shadow_target, shadow_rebal))
    page = page.replace("__CATALYSTS__", render_catalysts(catalysts))
    page = page.replace("__MACRO__", render_macro(macro))
    page = page.replace("__CALENDAR__", render_calendar(calendar))
    page = page.replace("__EQUITY__", render_equity(equity))
    page = page.replace("__FUNDAMENTALS__", render_fundamentals(fundamentals))
    page = page.replace("__RESEARCH__", render_research(reports))
    page = page.replace("__TRADES__", render_trades(trades))
    page = page.replace("__TIME__", now)
    return page


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sentiment Trader &middot; Quant Desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://s3.tradingview.com/tv.js"></script>
<style>
  :root{
    --bg:#0a0a0b; --bg2:#0d0d0f; --panel:#141416; --panel-solid:#141416;
    --panel2:#181819; --line:rgba(255,255,255,.06); --line2:rgba(255,255,255,.12);
    --txt:#f4f4f5; --muted:#8b8b90;
    --green:#4cba8b; --red:#e06b62; --accent:#d8b888; --accent2:#d8b888;
    --gold:#d8b888;
    --radius:14px;
    --shadow:0 12px 28px -22px rgba(0,0,0,.7);
    --glow:0 0 0 1px rgba(216,184,136,.22);
  }
  *{box-sizing:border-box;}
  body{margin:0;color:var(--txt);
    font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased;background:var(--bg);}
  .wrap{max-width:1360px;margin:0 auto;padding:18px 24px 64px;}
  /* dichtes Spalten-Layout: Panels nebeneinander statt endlos untereinander */
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:8px;}
  .grid2>section{margin:0;}
  @media(max-width:900px){.grid2{grid-template-columns:1fr;}}
  section{margin-bottom:34px;animation:fadeUp .4s ease both;}
  h1{font-size:13px;font-weight:600;margin:0 0 16px;letter-spacing:.9px;
    text-transform:uppercase;color:#d6d7db;display:flex;align-items:center;gap:11px;
    cursor:pointer;user-select:none;padding-bottom:12px;
    border-bottom:1px solid var(--line2);}
  h1:hover{color:var(--txt);}
  h1::before{content:"";width:3px;height:15px;border-radius:2px;
    background:var(--accent);flex:none;}
  h1 .cstate{margin-left:auto;color:var(--muted);font-size:11px;
    transition:transform .15s ease;}
  h1.collapsed .cstate{transform:rotate(-90deg);}
  h1.collapsed{margin-bottom:0;border-bottom-color:var(--line);}
  @keyframes fadeUp{from{opacity:0;transform:translateY(6px);}to{opacity:1;transform:none;}}
  .stamp{color:var(--muted);font-size:12.5px;margin-bottom:16px;line-height:1.5;}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(176px,1fr));
    gap:12px;margin-bottom:28px;}
  .card{position:relative;background:var(--panel-solid);
    border:1px solid var(--line);border-radius:var(--radius);padding:15px 17px;
    transition:border-color .18s ease;}
  .card:hover{border-color:var(--line2);}
  .lbl{color:var(--muted);font-size:10.5px;text-transform:uppercase;
    letter-spacing:1px;margin-bottom:10px;font-weight:600;}
  .big{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums;
    letter-spacing:-.3px;}
  .sub{font-size:13px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums;}
  table{width:100%;border-collapse:collapse;background:var(--panel-solid);
    border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;}
  th,td{padding:11px 15px;text-align:right;font-size:13px;
    font-variant-numeric:tabular-nums;}
  th{color:var(--muted);font-weight:600;font-size:10.5px;text-transform:uppercase;
    letter-spacing:.7px;border-bottom:1px solid var(--line);background:var(--panel2);}
  td{border-bottom:1px solid var(--line);}
  tbody tr{transition:background .12s ease;}
  tbody tr:hover{background:rgba(255,255,255,.025);}
  tr:last-child td{border-bottom:none;}
  .sym{text-align:left;font-weight:600;color:var(--txt);}
  th:first-child{text-align:left;}
  .sortable thead th{cursor:pointer;user-select:none;white-space:nowrap;}
  .sortable thead th:hover{color:var(--txt);}
  .sortable thead th .arr{font-size:10px;color:var(--accent);margin-left:3px;}
  .pos{color:var(--green);}
  .neg{color:var(--red);}
  .empty{text-align:center;color:var(--muted);padding:30px;}
  .foot{color:var(--muted);font-size:12px;margin-top:16px;text-align:center;}
  .addform{display:flex;gap:10px;margin:6px 0 14px;}
  .addform input{flex:0 0 200px;background:var(--panel);border:1px solid var(--line);
    color:var(--txt);border-radius:10px;padding:11px 14px;font-size:14px;
    text-transform:uppercase;letter-spacing:.5px;outline:none;}
  .addform input:focus{border-color:var(--accent);}
  .addform button{background:var(--accent);color:#fff;border:none;border-radius:10px;
    padding:11px 18px;font-size:14px;font-weight:600;cursor:pointer;}
  .addform button:hover{filter:brightness(1.1);}
  .chips{display:flex;flex-wrap:wrap;gap:8px;}
  .chip{display:inline-flex;align-items:center;gap:8px;background:var(--panel);
    border:1px solid var(--line);border-radius:999px;padding:6px 8px 6px 14px;
    margin:0;}
  .chip span{font-weight:600;color:var(--accent);font-size:14px;}
  .chip button{background:transparent;border:none;color:var(--muted);cursor:pointer;
    font-size:18px;line-height:1;padding:0 4px;}
  .chip button:hover{color:var(--red);}
  .wl-empty{color:var(--muted);font-size:14px;}
  .scroll{max-height:430px;overflow:auto;border:1px solid var(--line);
    border-radius:14px;
    scrollbar-width:thin;scrollbar-color:#3a4456 var(--panel);}
  .scroll table{border:none;border-radius:0;}
  .scroll thead th{position:sticky;top:0;z-index:1;background:#121622;}
  /* dunkle Scrollleiste passend zum Design (Webkit/Chrome/Edge) */
  html{scrollbar-width:thin;scrollbar-color:#2e3647 var(--bg);}
  ::-webkit-scrollbar{width:12px;height:12px;}
  ::-webkit-scrollbar-track{background:var(--bg);}
  .scroll::-webkit-scrollbar-track{background:var(--panel);}
  ::-webkit-scrollbar-thumb{background:#2e3647;border-radius:8px;
    border:3px solid var(--bg);}
  .scroll::-webkit-scrollbar-thumb{background:#3a4456;
    border:3px solid var(--panel);}
  ::-webkit-scrollbar-thumb:hover{background:#3f4a5f;}
  ::-webkit-scrollbar-corner{background:var(--bg);}
  .tfilter{width:100%;max-width:280px;background:var(--panel);
    border:1px solid var(--line);color:var(--txt);border-radius:10px;
    padding:9px 13px;font-size:14px;outline:none;margin:0 0 12px;}
  .tfilter:focus{border-color:var(--accent);}
  .topbar{position:sticky;top:0;z-index:40;display:flex;align-items:center;
    justify-content:space-between;gap:12px;margin:-18px -24px 24px;padding:15px 24px;
    background:rgba(10,10,11,.85);backdrop-filter:blur(10px);
    border-bottom:1px solid var(--line);}
  .brand{display:flex;align-items:center;gap:13px;}
  .logo{width:34px;height:34px;border-radius:9px;flex:none;position:relative;
    border:1px solid var(--line2);background:var(--panel2);}
  .logo::before{content:"";position:absolute;inset:11px;border-radius:50%;
    background:var(--accent);}
  .brandname{font-size:16px;font-weight:650;letter-spacing:.2px;line-height:1.15;}
  .brandtag{font-size:9.5px;font-weight:600;letter-spacing:1.6px;color:var(--muted);
    vertical-align:middle;margin-left:7px;border:1px solid var(--line);
    border-radius:4px;padding:2px 6px;}
  .topbar .stamp{margin:3px 0 0;font-size:11.5px;}
  .live{display:inline-flex;align-items:center;gap:5px;color:var(--green);font-weight:600;}
  .live i{width:6px;height:6px;border-radius:50%;background:var(--green);}
  .gear{background:var(--panel2);border:1px solid var(--line);color:var(--muted);
    width:40px;height:40px;border-radius:10px;font-size:18px;cursor:pointer;
    line-height:1;flex:none;transition:all .18s ease;}
  .gear:hover{border-color:var(--line2);color:var(--txt);}
  /* Einstellungen als feste, einschiebbare Seitenleiste rechts */
  .settings{position:fixed;top:0;right:0;bottom:0;width:380px;max-width:92vw;
    background:var(--panel-solid);border-left:1px solid var(--line2);
    padding:18px 20px 30px;overflow-y:auto;z-index:60;
    transform:translateX(100%);transition:transform .22s ease;
    box-shadow:-14px 0 34px rgba(0,0,0,.4);}
  body.settings-open .settings{transform:translateX(0);}
  /* Inhalt bleibt mittig (margin:0 auto) – die feste Leiste liegt rechts
     daneben und schiebt den zentrierten Inhalt nicht. */
  .setclose{position:absolute;top:14px;right:16px;background:transparent;
    border:none;color:var(--muted);font-size:24px;line-height:1;cursor:pointer;}
  .setclose:hover{color:var(--txt);}
  .settings h2{font-size:16px;margin:2px 0 4px;}
  .setrow{display:flex;align-items:center;justify-content:space-between;
    gap:12px;padding:10px 0;border-bottom:1px solid var(--line);}
  .setrow:last-of-type{border-bottom:none;}
  .setlbl{font-size:14px;}
  .setin{display:inline-flex;align-items:center;gap:6px;}
  .setin input,.setin select{background:var(--bg);border:1px solid var(--line);
    color:var(--txt);border-radius:8px;padding:8px 10px;font-size:14px;
    width:110px;text-align:right;outline:none;}
  .setin select{text-align:left;}
  .setin input:focus,.setin select:focus{border-color:var(--accent);}
  .unit{color:var(--muted);font-size:13px;width:12px;}
  .setbtns{display:flex;gap:10px;margin-top:16px;}
  .setbtns button{background:var(--accent);color:#fff;border:none;border-radius:10px;
    padding:11px 18px;font-size:14px;font-weight:600;cursor:pointer;}
  .setbtns button:hover{filter:brightness(1.1);}
  .setnote{color:var(--muted);font-size:12px;margin-top:12px;}
  .equity{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:14px 16px;margin:0 0 26px;}
  .rtabs{float:right;display:inline-flex;gap:2px;}
  .rtab{background:transparent;border:1px solid var(--line);color:var(--muted);
    border-radius:6px;padding:1px 6px;font-size:10px;cursor:pointer;}
  .rtab.active{border-color:var(--accent);color:var(--accent);}
  .symlink{cursor:pointer;color:var(--accent);font-weight:600;}
  .symlink:hover{text-decoration:underline;}
  .modal-box.modal-wide{width:92vw;max-width:1800px;max-height:94vh;overflow-y:auto;}
  #tvHost{height:62vh;min-height:420px;margin-top:12px;}
  #chartBrief{margin-top:14px;background:var(--panel2);border:1px solid var(--line);
    border-radius:10px;padding:14px 16px;font-size:13.5px;line-height:1.6;color:var(--txt);
    white-space:pre-wrap;}
  #chartBrief b,#chartBrief strong{color:var(--accent);font-weight:600;}
  #chartNews{margin-top:14px;max-height:220px;overflow-y:auto;}
  .newshead{font-size:13px;font-weight:600;color:var(--muted);margin-bottom:6px;
    text-transform:uppercase;letter-spacing:.5px;}
  .newsrow{font-size:13px;padding:5px 0;border-bottom:1px solid var(--line);line-height:1.4;}
  .newsrow:last-child{border-bottom:none;}
  .nb{display:inline-block;min-width:42px;text-align:center;border-radius:5px;
    padding:1px 5px;font-size:11px;font-weight:600;background:#11141c;}
  .nm{color:var(--muted);font-size:12px;}
  .flash{border-radius:10px;padding:12px 16px;margin:0 0 16px;font-size:14px;font-weight:600;}
  .flash-ok{background:rgba(27,196,125,.15);border:1px solid var(--green);color:var(--green);}
  .flash-warn{background:rgba(246,70,93,.12);border:1px solid var(--red);color:var(--red);}
  .sellbtn{background:var(--red);color:#fff;border:none;border-radius:6px;
    padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;}
  .sellbtn:hover{filter:brightness(1.1);}
  /* Verkaufs-Dialog (modal) */
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:80;
    display:none;align-items:center;justify-content:center;}
  .modal-box{position:relative;background:var(--panel-solid);border:1px solid var(--line2);
    border-radius:16px;padding:22px 24px;width:380px;max-width:92vw;
    box-shadow:var(--shadow);}
  .modal-box h2{font-size:18px;margin:0 0 6px;}
  .modal-box .setrow{margin-top:8px;}
  .modal-box input[type=number]{background:var(--bg);border:1px solid var(--line);
    color:var(--txt);border-radius:8px;padding:8px 10px;font-size:14px;width:120px;
    text-align:right;outline:none;}
  .modal-box input[type=range]{width:100%;margin:12px 0 2px;accent-color:var(--accent);}
  .modal-box .proceeds{font-size:20px;font-weight:600;margin-top:8px;}
  .modal-box .sellgo{background:var(--red);color:#fff;border:none;border-radius:10px;
    padding:11px 18px;font-size:14px;font-weight:600;cursor:pointer;width:100%;margin-top:14px;}
  .modal-box .sellgo:hover{filter:brightness(1.1);}
  /* Berichts-Ordner je Aktie + einzelne Berichte */
  .rfolder{background:var(--panel-solid);border:1px solid var(--line);
    border-radius:12px;padding:11px 15px;margin:0 0 10px;}
  .rfolder>summary{cursor:pointer;font-weight:600;color:var(--txt);
    list-style:none;font-size:14px;display:flex;align-items:center;gap:8px;}
  .rfolder>summary::-webkit-details-marker{display:none;}
  .rfolder>summary b{color:var(--accent);}
  .rfolder>summary .rmeta{margin-left:auto;display:flex;align-items:center;gap:8px;}
  .sbadge{display:inline-flex;align-items:center;gap:4px;font-size:10px;font-weight:700;
    letter-spacing:.4px;text-transform:uppercase;padding:3px 8px;border-radius:999px;
    border:1px solid var(--line2);white-space:nowrap;}
  .sbadge.buy{color:var(--green);border-color:rgba(76,186,139,.45);background:rgba(76,186,139,.10);}
  .sbadge.sell{color:var(--red);border-color:rgba(224,107,98,.45);background:rgba(224,107,98,.10);}
  .sbadge.neutral{color:var(--accent);border-color:rgba(216,184,136,.45);background:rgba(216,184,136,.10);}
  .rinner{margin-top:10px;display:flex;flex-direction:column;gap:8px;}
  .report{background:var(--panel2);border:1px solid var(--line);border-radius:10px;
    padding:9px 13px;margin:0;}
  .report summary{cursor:pointer;font-weight:600;color:var(--accent);
    display:flex;align-items:center;gap:8px;list-style:none;}
  .report summary::-webkit-details-marker{display:none;}
  .rkind{color:var(--accent);}
  .rdel{margin-left:auto;display:inline;}
  .rdel button{background:transparent;border:1px solid var(--line);color:var(--muted);
    width:24px;height:24px;border-radius:7px;cursor:pointer;font-size:15px;line-height:1;
    padding:0;transition:all .15s ease;}
  .rdel button:hover{border-color:var(--red);color:var(--red);}
  .report .muted{color:var(--muted);font-weight:400;}
  .md{font-size:13.5px;line-height:1.62;color:var(--txt);margin:10px 0 2px;}
  .md .mdp{margin:0 0 6px;}
  .md .mdli{margin:0 0 5px;padding-left:16px;position:relative;}
  .md .mdli::before{content:"–";position:absolute;left:2px;color:var(--accent);}
  .md .mdh{margin:15px 0 7px;font-size:11.5px;text-transform:uppercase;
    letter-spacing:.8px;color:var(--muted);font-weight:600;}
  .md>.mdh:first-child{margin-top:2px;}
  .md .mdsp{height:7px;}
  .md b{color:var(--accent);font-weight:600;}
  .md a{color:var(--accent);}
  code{background:#11141c;border:1px solid var(--line);border-radius:5px;
    padding:1px 5px;font-size:12px;}
  /* --- Shadow / systematischer Kern (Forward-Track ohne Handel) --- */
  .shadow{display:grid;grid-template-columns:1.05fr 1fr;gap:18px;margin-bottom:30px;}
  @media(max-width:780px){.shadow{grid-template-columns:1fr;}}
  .panelbox{position:relative;overflow:hidden;
    background:linear-gradient(160deg,rgba(124,131,255,.09),rgba(255,255,255,.012));
    border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;
    box-shadow:var(--shadow);}
  .panelbox::after{content:"";position:absolute;inset:0 0 auto 0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(124,131,255,.4),transparent);}
  .ptitle{display:flex;align-items:center;justify-content:space-between;gap:10px;
    margin-bottom:6px;}
  .ptitle .lbl{margin:0;}
  .badge{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:700;
    letter-spacing:.6px;text-transform:uppercase;padding:4px 10px;border-radius:999px;
    border:1px solid var(--line2);}
  .badge.on{color:var(--green);border-color:rgba(52,211,153,.45);background:rgba(52,211,153,.1);}
  .badge.off{color:var(--gold);border-color:rgba(251,191,36,.45);background:rgba(251,191,36,.1);}
  .badge i{width:6px;height:6px;border-radius:50%;background:currentColor;}
  .shadowrow{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin:10px 0 2px;}
  .shadowlvl{font-size:30px;font-weight:800;letter-spacing:-1px;
    font-variant-numeric:tabular-nums;}
  .pickchips{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px;}
  .pickchip{font-size:12px;font-weight:600;background:rgba(255,255,255,.05);
    border:1px solid var(--line);border-radius:8px;padding:4px 9px;cursor:pointer;
    transition:all .14s ease;}
  .pickchip:hover{border-color:var(--accent);color:var(--accent);
    background:rgba(124,131,255,.1);}
  /* --- Katalysatoren (LLM) --- */
  .catlist{display:flex;flex-direction:column;gap:8px;margin-bottom:30px;}
  .catrow{display:grid;grid-template-columns:92px 104px 50px 72px 1fr;
    align-items:center;gap:12px;font-size:13.5px;padding:11px 16px;
    border:1px solid var(--line);border-radius:12px;
    background:linear-gradient(160deg,rgba(255,255,255,.04),rgba(255,255,255,.01));
    transition:border-color .15s ease;}
  .catrow:hover{border-color:var(--line2);}
  .catsym{font-weight:600;}
  .badge.up{color:var(--green);border-color:rgba(52,211,153,.45);background:rgba(52,211,153,.1);}
  .badge.down{color:var(--red);border-color:rgba(251,113,133,.45);background:rgba(251,113,133,.1);}
  .badge.flat{color:var(--muted);}
  .catconv{font-weight:700;font-variant-numeric:tabular-nums;text-align:right;}
  .cathor{color:var(--muted);font-size:12px;}
  .catwhy{color:var(--txt);line-height:1.4;}
  @media(max-width:780px){.catrow{grid-template-columns:1fr;gap:5px;}}
  /* --- Cross-Asset-Korrelation --- */
  .macrolist{display:flex;flex-direction:column;gap:8px;margin-bottom:30px;}
  .macrorow{display:grid;grid-template-columns:140px 1fr 52px 2fr;align-items:center;
    gap:14px;font-size:13.5px;padding:11px 16px;border:1px solid var(--line);
    border-radius:12px;
    background:linear-gradient(160deg,rgba(255,255,255,.04),rgba(255,255,255,.01));}
  .macrolbl{font-weight:600;}
  .macrobar{height:7px;border-radius:5px;background:rgba(255,255,255,.07);
    overflow:hidden;}
  .macrofill{display:block;height:100%;border-radius:5px;}
  .macrofill.up{background:var(--green);}
  .macrofill.down{background:var(--red);}
  .macrofill.flat{background:var(--muted);}
  .macroval{font-weight:700;font-variant-numeric:tabular-nums;text-align:right;}
  .macroval.up{color:var(--green);} .macroval.down{color:var(--red);}
  .macroval.flat{color:var(--muted);}
  .macrowhy{color:var(--muted);line-height:1.4;}
  @media(max-width:780px){.macrorow{grid-template-columns:1fr;gap:6px;}.macrobar{display:none;}}
  /* --- Trading-Kalender --- */
  .callist{display:flex;flex-direction:column;gap:8px;margin-bottom:30px;}
  .calrow{display:grid;grid-template-columns:96px 104px 1.2fr 1.6fr;align-items:center;
    gap:12px;font-size:13.5px;padding:11px 16px;border:1px solid var(--line);
    border-radius:12px;
    background:linear-gradient(160deg,rgba(255,255,255,.04),rgba(255,255,255,.01));}
  .caldate{font-weight:700;font-variant-numeric:tabular-nums;font-size:12.5px;}
  .caltitle{font-weight:600;}
  .calwhy{color:var(--muted);line-height:1.4;}
  @media(max-width:780px){.calrow{grid-template-columns:1fr;gap:5px;}}
</style>
</head>
<body>
  <div class="wrap">
    <header class="topbar">
      <div class="brand">
        <span class="logo"></span>
        <div>
          <div class="brandname">Sentiment&nbsp;Trader<span class="brandtag">QUANT&nbsp;DESK</span></div>
          <div class="stamp">Stand: __TIME__ &middot; <span class="live"><i></i>live</span></div>
        </div>
      </div>
      <button id="gear" class="gear" title="Einstellungen" type="button">&#9881;</button>
    </header>
    <div id="flash" class="flash" style="display:none"></div>

    <div id="settingsPanel" class="settings">
      <button id="gearClose" class="setclose" type="button" title="Schließen">&times;</button>
      <h2>Einstellungen</h2>
      <div class="stamp">Änderungen greifen ab dem nächsten stündlichen Durchlauf.</div>
      <form method="post" action="/settings">
        __SETTINGS__
        <div class="setbtns"><button type="submit">Speichern</button></div>
        <div class="setnote">Schwellen als Score (z.&nbsp;B. 0.30). Prozentwerte
          in % (z.&nbsp;B. 8 = 8&nbsp;%). Kill-Switch „Aus" stoppt jeden Handel.</div>
      </form>
    </div>

    <div class="cards">__SUMMARY__</div>

    <section>
      <h1>Systematischer Kern · Shadow-Track</h1>
      <div class="stamp">Validierte Momentum-+-Regime-Strategie · rechnet live mit,
        handelt aber NICHT – ein ehrlicher, risikofreier Forward-Track.</div>
      __SHADOW__
    </section>

    <div class="grid2">
    <section>
      <h1>Katalysatoren · LLM-Scan</h1>
      <div class="stamp">Gemini liest die jüngsten News und extrahiert handelbare
        Events (Lieferketten/Engpässe, Deals, Zulassungen) · informativ, fließt
        später als ML-Feature ein.</div>
      __CATALYSTS__
    </section>

    <section>
      <h1>Trading-Kalender · anstehende Events</h1>
      <div class="stamp">Notenbank, Makro-Daten (CPI/FOMC/Jobs) &amp; Earnings deiner
        Werte – mit kurzer Erläuterung der voraussichtlichen Auswirkung (Gemini).</div>
      __CALENDAR__
    </section>
    </div>

    <section>
      <h1>Cross-Asset-Korrelation</h1>
      <div class="stamp">Wie dein Portfolio mit Gold, Silber, US-Dollar und
        US-Anleihen zusammenhängt (Tagesrenditen, letzte ~180 Handelstage).</div>
      __MACRO__
    </section>

    <section>
      <h1>Kontowert-Verlauf</h1>
      <div class="equity">__EQUITY__</div>
    </section>

    <section>
      <h1>Positionen</h1>
    <input id="posFilter" class="tfilter" type="text" autocomplete="off"
           placeholder="Positionen filtern (Symbol)…">
    <div class="scroll">
    <table id="posTable" class="sortable">
      <thead>
        <tr>
          <th>Symbol</th><th>Stück</th><th>Ø Einstand</th><th>Akt. Kurs</th>
          <th>Wert</th><th>G/V €</th><th>G/V %</th><th>Stop / Ziel</th><th>Aktion</th>
        </tr>
      </thead>
      <tbody id="posBody">
        __ROWS__
      </tbody>
    </table>
    </div>
    </section>

    <div class="grid2">
    <section>
      <h1>Eigene Werte handeln</h1>
      <div class="stamp">Kürzel eingeben – US ohne Suffix (NVDA), Ausland mit
        Börsen-Suffix wie bei Yahoo: z.&nbsp;B. SIVE.ST, SAP.DE, SHEL.L.</div>
      <form class="addform" method="post" action="/watchlist/add">
        <input name="ticker" placeholder="z. B. SIVE.ST" maxlength="12"
               autocomplete="off" spellcheck="false">
        <button type="submit">Hinzufügen</button>
      </form>
      <div class="chips">__WATCHLIST__</div>
    </section>
    <section>
      <h1>Strategie-Performance</h1>
      <div class="stamp">Trefferquote &amp; Ø-Return der bewerteten Signale
        (5-Börsentage-Horizont)</div>
      <div class="cards">__PERFORMANCE__</div>
    </section>
    </div>

    <section>
      <h1>Deep-Dive-Research (Gemini)</h1>
    __RESEARCH__
    </section>

    <section>
      <h1>Fundamental-Vergleich</h1>
    <div class="stamp">Relative Bewertung · Value/Growth = P/S ÷ Umsatzwachstum
      (niedriger = mehr Wachstum pro Bewertungs-Dollar)</div>
    <div class="scroll">
    <table id="fundTable" class="sortable">
      <thead>
        <tr>
          <th>Symbol</th><th>P/S</th><th>EV/EBITDA</th><th>Bruttomarge</th>
          <th>Umsatzwachstum</th><th>Value/Growth</th>
        </tr>
      </thead>
      <tbody>
        __FUNDAMENTALS__
      </tbody>
    </table>
    </div>
    </section>

    <section>
      <h1>Letzte Signale</h1>
    <input id="sigFilter" class="tfilter" type="text" autocomplete="off"
           placeholder="Signale filtern (Symbol)…">
    <div class="scroll">
    <table id="sigTable" class="sortable">
      <thead>
        <tr>
          <th>Zeit</th><th>Symbol</th><th>Aktion</th><th>Score</th>
          <th>Sentiment</th><th>Stück</th><th>Return 5T</th>
        </tr>
      </thead>
      <tbody id="sigBody">
        __SIGNALS__
      </tbody>
    </table>
    </div>
    </section>

    <section>
      <h1>Trade-Verlauf</h1>
    <div class="stamp">Tatsächlich ausgeführte Käufe/Verkäufe (realisierter G/V je Verkauf)</div>
    <div class="scroll">
    <table id="tradeTable" class="sortable">
      <thead>
        <tr><th>Zeit</th><th>Symbol</th><th>Seite</th><th>Stück</th>
            <th>Kurs</th><th>Realisiert</th></tr>
      </thead>
      <tbody>
        __TRADES__
      </tbody>
    </table>
    </div>
    </section>

    <div class="foot">Paper-Konto &middot; Positionen live, Verkauf direkt möglich</div>
  </div>

  <div id="sellModal" class="modal">
    <div class="modal-box">
      <button id="sellClose" class="setclose" type="button">&times;</button>
      <h2>Verkaufen: <span id="sellSym"></span></h2>
      <div class="stamp" id="sellMarket"></div>
      <div class="stamp">Bestand: <b id="sellHave"></b> &middot; Kurs <span id="sellPrice"></span></div>
      <form method="post" action="/sell">
        <input type="hidden" name="symbol" id="sellSymIn">
        <input type="hidden" name="currency" id="sellCurIn">
        <div class="setrow"><span class="setlbl">Anzahl verkaufen</span>
          <span class="setin"><input type="number" name="qty" id="sellQty" min="1"></span></div>
        <input type="range" id="sellRange" min="1" step="1">
        <div class="proceeds" id="sellProceeds"></div>
        <button type="submit" class="sellgo">Jetzt verkaufen</button>
      </form>
    </div>
  </div>

  <div id="chartModal" class="modal">
    <div class="modal-box modal-wide">
      <button id="chartClose" class="setclose" type="button">&times;</button>
      <h2 id="chartTitle"></h2>
      <div id="tvHost"></div>
      <div id="chartBrief"></div>
      <div id="chartNews"></div>
    </div>
  </div>
<script>
  const REFRESH = __REFRESH__;
  function applyFilter(inputId, bodyId){
    const q = (document.getElementById(inputId).value || '').trim().toUpperCase();
    document.querySelectorAll('#' + bodyId + ' tr').forEach(function(tr){
      const sym = tr.getAttribute('data-sym');
      const keep = !sym || !q || sym.toUpperCase().indexOf(q) >= 0;
      tr.style.display = keep ? '' : 'none';
    });
    try { localStorage.setItem(inputId, q); } catch(e) {}
  }
  function initFilter(inputId, bodyId){
    const el = document.getElementById(inputId);
    if(!el) return;
    try { el.value = localStorage.getItem(inputId) || ''; } catch(e) {}
    el.addEventListener('input', function(){ applyFilter(inputId, bodyId); });
    applyFilter(inputId, bodyId);
  }
  function typing(){
    // Auto-Refresh pausiert, solange in IRGENDEIN Feld getippt wird (Filter
    // oder Einstellungen) – sonst geht die Eingabe beim Reload verloren.
    const a = document.activeElement;
    return a && ['INPUT','SELECT','TEXTAREA'].indexOf(a.tagName) >= 0;
  }
  function setDrawer(open){
    document.body.classList.toggle('settings-open', open);
    try { localStorage.setItem('settingsOpen', open ? '1' : '0'); } catch(e){}
  }
  function initGear(){
    const gear = document.getElementById('gear');
    const close = document.getElementById('gearClose');
    if(!gear) return;
    // Zustand merken, damit die Leiste beim Auto-Refresh offen bleibt.
    try { if(localStorage.getItem('settingsOpen') === '1') setDrawer(true); } catch(e){}
    gear.addEventListener('click', function(){
      setDrawer(!document.body.classList.contains('settings-open'));
    });
    if(close) close.addEventListener('click', function(){ setDrawer(false); });
  }
  // --- Spalten-Sortierung (klick auf Überschrift, asc/desc, gemerkt) ---
  function _key(td){
    var ds = td.getAttribute('data-sort');
    if(ds !== null){
      if(ds === '') return {empty:true};
      var v = parseFloat(ds);
      if(!isNaN(v)) return {num:v};
      return {str:ds.toLowerCase()};
    }
    return {str:(td.textContent||'').trim().toLowerCase()};
  }
  function sortTable(table, col, dir){
    var tb = table.tBodies[0]; if(!tb) return;
    var rows = Array.prototype.slice.call(tb.rows).filter(function(r){
      return r.cells.length > col && !r.querySelector('td.empty');
    });
    rows.sort(function(a,b){
      var ka=_key(a.cells[col]), kb=_key(b.cells[col]);
      if(ka.empty && kb.empty) return 0;
      if(ka.empty) return 1;            // leere / „—" immer ans Ende
      if(kb.empty) return -1;
      var c;
      if(ka.num!==undefined && kb.num!==undefined){ c = ka.num - kb.num; }
      else {
        var sa = ka.str!==undefined?ka.str:String(ka.num);
        var sb = kb.str!==undefined?kb.str:String(kb.num);
        c = sa<sb?-1:(sa>sb?1:0);
      }
      return dir==='desc' ? -c : c;
    });
    rows.forEach(function(r){ tb.appendChild(r); });
  }
  function markArrows(table, col, dir){
    table.querySelectorAll('thead th').forEach(function(th,i){
      var a=th.querySelector('.arr'); if(a) a.remove();
      if(i===col){ var s=document.createElement('span'); s.className='arr';
        s.textContent = dir==='desc'?'▼':'▲'; th.appendChild(s); }
    });
  }
  function getSort(id){ try{ return JSON.parse(localStorage.getItem('sort_'+id)||'null'); }catch(e){ return null; } }
  function initSort(){
    document.querySelectorAll('table.sortable').forEach(function(table){
      var id = table.id || 'tbl';
      table.querySelectorAll('thead th').forEach(function(th,i){
        th.addEventListener('click', function(){
          var cur = getSort(id);
          var dir = (cur && cur.col===i && cur.dir==='asc') ? 'desc' : 'asc';
          try{ localStorage.setItem('sort_'+id, JSON.stringify({col:i,dir:dir})); }catch(e){}
          sortTable(table,i,dir); markArrows(table,i,dir);
        });
      });
      var saved = getSort(id);
      if(saved){ sortTable(table,saved.col,saved.dir); markArrows(table,saved.col,saved.dir); }
    });
  }
  // --- Realisiert: Umschaltung Tag/Woche/Monat/Jahr ---
  function showRealized(p){
    var el = document.getElementById('realizedVal');
    if(!el) return;
    var v = el.getAttribute('data-'+p);
    if(v === null){ p='day'; v = el.getAttribute('data-day'); }
    el.textContent = v;
    el.className = 'big ' + (el.getAttribute('data-'+p+'c') || '');
    document.querySelectorAll('.rtab').forEach(function(b){
      b.classList.toggle('active', b.getAttribute('data-p')===p);
    });
    try{ localStorage.setItem('realizedPeriod', p); }catch(e){}
  }
  function initRealized(){
    var tabs = document.querySelectorAll('.rtab');
    if(!tabs.length) return;
    tabs.forEach(function(b){
      b.addEventListener('click', function(){ showRealized(b.getAttribute('data-p')); });
    });
    var saved = 'day';
    try{ saved = localStorage.getItem('realizedPeriod') || 'day'; }catch(e){}
    showRealized(saved);
  }
  // --- Verkaufs-Dialog ---
  var CURSYM={USD:'$',EUR:'€',GBP:'£',JPY:'¥',CHF:'CHF',CAD:'C$',AUD:'A$',HKD:'HK$',SEK:'SEK',NOK:'NOK',DKK:'DKK'};
  function deNum(x){ return (x||0).toLocaleString('de-DE',{minimumFractionDigits:2,maximumFractionDigits:2}); }
  function sellModalEl(){ return document.getElementById('sellModal'); }
  function closeSell(){ var m=sellModalEl(); if(m) m.style.display='none'; }
  function modalOpen(){ return Array.prototype.some.call(
      document.querySelectorAll('.modal'), function(m){ return m.style.display==='flex'; }); }
  // --- Chart-Fenster (TradingView) ---
  function closeChart(){ var m=document.getElementById('chartModal');
    if(m){ m.style.display='none'; document.getElementById('tvHost').innerHTML=''; } }
  function loadNews(news){
    var box=document.getElementById('chartNews'); if(!box) return;
    box.innerHTML='<div class="stamp">Lade News…</div>';
    fetch('/news?sym='+encodeURIComponent(news)).then(function(r){return r.json();})
      .then(function(items){
        box.innerHTML='';
        var head=document.createElement('div'); head.className='newshead';
        head.textContent='News zu '+news; box.appendChild(head);
        if(!items.length){ var e=document.createElement('div'); e.className='stamp';
          e.textContent='Keine getaggten News.'; box.appendChild(e); return; }
        items.forEach(function(n){
          var row=document.createElement('div'); row.className='newsrow';
          if(n.sent!=null){ var b=document.createElement('span');
            b.className='nb '+(n.sent>=0.1?'pos':(n.sent<=-0.1?'neg':''));
            b.textContent=(n.sent>=0?'+':'')+n.sent.toFixed(2); row.appendChild(b); }
          var tt=document.createElement('span'); tt.className='nt';
          tt.textContent=' '+(n.title||''); row.appendChild(tt);
          var mm=document.createElement('span'); mm.className='nm';
          mm.textContent=' ('+(n.source||'')+' · '+(n.ts||'')+')'; row.appendChild(mm);
          box.appendChild(row);
        });
      }).catch(function(){ box.innerHTML='<div class="stamp">News konnten nicht geladen werden.</div>'; });
  }
  function loadBrief(sym){
    var box=document.getElementById('chartBrief'); if(!box) return;
    box.innerHTML='<span class="nm">Lade Zusammenfassung… (KI, einen Moment)</span>';
    fetch('/newsbrief?sym='+encodeURIComponent(sym)).then(function(r){return r.json();})
      .then(function(d){
        var t=(d&&d.brief)||'Keine Zusammenfassung.';
        t=t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        t=t.replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>');
        box.innerHTML=t;
      }).catch(function(){ box.innerHTML='<span class="nm">Zusammenfassung nicht verfügbar.</span>'; });
  }
  function openChart(tv, label, news){
    document.getElementById('chartTitle').textContent='Chart: '+label;
    var host=document.getElementById('tvHost');
    host.innerHTML='<div id="tvchart" style="height:100%"></div>';
    if(window.TradingView){
      new TradingView.widget({container_id:'tvchart', symbol:tv, interval:'D',
        autosize:true, theme:'dark', style:'1', locale:'de', timezone:'Europe/Berlin',
        hide_side_toolbar:false, allow_symbol_change:true, withdateranges:true});
    } else {
      host.innerHTML='<div class="stamp">Chart konnte nicht geladen werden (kein Internet?).</div>';
    }
    document.getElementById('chartNews').innerHTML='';
    document.getElementById('chartBrief').innerHTML='';
    if(news){ loadBrief(news); loadNews(news); }
    document.getElementById('chartModal').style.display='flex';
  }
  function initChart(){
    document.querySelectorAll('.symlink').forEach(function(s){
      s.addEventListener('click', function(){
        openChart(s.getAttribute('data-tv'), s.getAttribute('data-label'), s.getAttribute('data-news')); });
    });
    var c=document.getElementById('chartClose'); if(c) c.addEventListener('click', closeChart);
    var m=document.getElementById('chartModal');
    if(m) m.addEventListener('click', function(e){ if(e.target===m) closeChart(); });
  }
  function openSell(btn){
    var sym=btn.getAttribute('data-sym'), cur=btn.getAttribute('data-cur');
    var qty=parseInt(btn.getAttribute('data-qty'))||0;
    var price=parseFloat(btn.getAttribute('data-price'))||0;
    var symEl=CURSYM[cur]||cur;
    document.getElementById('sellSym').textContent=sym;
    document.getElementById('sellSymIn').value=sym;
    document.getElementById('sellCurIn').value=cur;
    document.getElementById('sellHave').textContent=qty.toLocaleString('de-DE')+' Stück';
    document.getElementById('sellPrice').textContent=deNum(price)+' '+symEl;
    var mk=btn.getAttribute('data-market')||'', hrs=btn.getAttribute('data-hours')||'';
    var op=btn.getAttribute('data-open')==='1';
    function setMarket(mkx, hrsx, opx){
      document.getElementById('sellMarket').innerHTML =
        'Börse: <b>'+mkx+'</b> · handelbar (DE-Zeit): '+hrsx+' · '+
        (opx?'<span style="color:var(--green)">jetzt offen</span>'
           :'<span style="color:var(--red)">geschlossen – Verkauf erst zur Handelszeit</span>');
    }
    setMarket(mk, hrs, op);
    // Börsenstatus beim Öffnen FRISCH abfragen (eingebackener Wert kann veraltet sein)
    fetch('/marketopen?sym='+encodeURIComponent(sym)+'&cur='+encodeURIComponent(cur))
      .then(function(r){return r.json();})
      .then(function(d){ setMarket(d.market||mk, d.hours||hrs, !!d.open); })
      .catch(function(){});
    var qi=document.getElementById('sellQty'), rg=document.getElementById('sellRange');
    qi.max=qty; qi.value=qty; rg.max=qty; rg.value=qty;
    function upd(){ var q=parseInt(qi.value)||0; if(q<1)q=1; if(q>qty)q=qty;
      document.getElementById('sellProceeds').textContent='Erlös ca. '+deNum(q*price)+' '+symEl; }
    qi.oninput=function(){ rg.value=qi.value; upd(); };
    rg.oninput=function(){ qi.value=rg.value; upd(); };
    upd();
    sellModalEl().style.display='flex';
  }
  function initSell(){
    closeSell();   // sicher geschlossen starten (zuverlässige modalOpen-Erkennung)
    document.querySelectorAll('.sellbtn').forEach(function(b){
      b.addEventListener('click', function(){ openSell(b); });
    });
    var c=document.getElementById('sellClose'); if(c) c.addEventListener('click', closeSell);
    var m=sellModalEl();
    if(m) m.addEventListener('click', function(e){ if(e.target===m) closeSell(); });
  }
  function initFlash(){
    var p; try{ p = new URLSearchParams(location.search).get('sell'); }catch(e){ return; }
    if(!p) return;
    var el=document.getElementById('flash'); if(!el) return;
    var msg, cls='flash-warn';
    if(p==='ok'){ msg='✅ Verkauf ausgelöst (füllt zum nächsten Kurs).'; cls='flash-ok'; }
    else if(p==='closed'){ msg='⚠️ Börse geschlossen – Verkauf nur zur Börsenzeit möglich. Es wurde nichts verändert.'; }
    else { msg='⚠️ Verkauf fehlgeschlagen – bitte Logs prüfen.'; }
    el.textContent=msg; el.className='flash '+cls; el.style.display='block';
    try{ history.replaceState({}, '', '/'); }catch(e){}
  }
  // --- Deep-Dive-/Risiko-Reports: offen halten über Auto-Reload hinweg ---
  function reportOpen(){ return !!document.querySelector('details.report[open], details.rfolder[open]'); }
  function initResearch(){
    var s={}; try{ s=JSON.parse(localStorage.getItem('openReports')||'{}'); }catch(e){}
    document.querySelectorAll('details[data-rk]').forEach(function(d){
      var k=d.getAttribute('data-rk')||'';
      if(s[k]) d.open=true;
      d.addEventListener('toggle', function(){
        var o={}; try{ o=JSON.parse(localStorage.getItem('openReports')||'{}'); }catch(e){}
        if(d.open) o[k]=1; else delete o[k];
        try{ localStorage.setItem('openReports', JSON.stringify(o)); }catch(e){}
      });
    });
  }
  // --- Themen ein-/ausklappen (Zustand gemerkt) ---
  function initCollapse(){
    document.querySelectorAll('section > h1').forEach(function(h){
      var label=(h.textContent||'').trim();
      var key='collapse_'+label.slice(0,46);
      var sec=h.parentElement;
      if(!h.querySelector('.cstate')){
        var c=document.createElement('span'); c.className='cstate';
        c.textContent='\\u25BE'; h.appendChild(c);
      }
      function apply(coll){
        Array.prototype.forEach.call(sec.children, function(e){
          if(e!==h) e.style.display = coll ? 'none' : ''; });
        h.classList.toggle('collapsed', coll);
      }
      var coll=false; try{ coll = localStorage.getItem(key)==='1'; }catch(e){}
      apply(coll);
      h.addEventListener('click', function(){
        var c=!h.classList.contains('collapsed'); apply(c);
        try{ localStorage.setItem(key, c?'1':'0'); }catch(e){}
      });
    });
  }
  window.addEventListener('DOMContentLoaded', function(){
    initFilter('posFilter','posBody');
    initFilter('sigFilter','sigBody');
    initGear();
    initSort();
    initRealized();
    initSell();
    initChart();
    initFlash();
    initCollapse();
    initResearch();
    // Mitte aktualisiert sich normal weiter; pausiert beim Tippen oder offenem Dialog.
    setInterval(function(){ if(!typing() && !modalOpen() && !reportOpen()) location.reload(); }, REFRESH * 1000);
  });
</script>
</body>
</html>"""


# -------------------------------------------------------------------------
# Webserver (eigener Thread, liefert nur statische Dateien aus)
# -------------------------------------------------------------------------
def _clean_ticker(raw: str) -> str:
    """Macht aus Nutzereingabe ein sauberes Symbol: Großbuchstaben, nur
    Buchstaben/Ziffern/.- erlaubt, max. 8 Zeichen."""
    t = (raw or "").strip().upper()
    return "".join(ch for ch in t if ch.isalnum() or ch in ".-")[:12]


def _parse_settings(fields: dict) -> dict:
    """Wandelt die Formularfelder in zu speichernde Werte (gemäß SCHEMA-Typ).
    pct-Felder kommen in % und werden als Anteil (0..1) gespeichert."""
    out = {}
    for key, (label, typ, default) in SCHEMA.items():
        if key not in fields:
            continue
        v = (fields[key][0] or "").strip().replace(",", ".")
        try:
            if typ == "bool":
                out[key] = "yes" if v.lower() in ("yes", "an", "1", "true", "on") else "no"
            elif typ == "pct":
                out[key] = str(float(v) / 100.0)
            elif typ == "int":
                out[key] = str(int(float(v)))
            else:  # float
                out[key] = str(float(v))
        except ValueError:
            continue
    return out


def place_manual_sell(symbol: str, qty: int, currency: str = "USD") -> str:
    """Verkauft per Markt-Order eine bestimmte Stückzahl. Eigene IB-Verbindung
    (nicht read-only). Nur bei OFFENER Börse (Market-Order kann sonst nicht
    füllen) – storniert die Schutz-Orders erst, wenn wirklich verkauft wird.
    Gibt 'ok' | 'closed' | 'error' zurück."""
    # Markt zu? Dann gar nichts anfassen (sonst Schutz-Orders weg, aber kein Fill)
    if not is_open(yf_symbol_for(symbol, currency)):
        log.warning("Verkauf %s nicht möglich: Börse geschlossen", symbol)
        return "closed"
    asyncio.set_event_loop(asyncio.new_event_loop())
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=SELL_CLIENT_ID,
                   readonly=False, timeout=10)
        contract = Stock(symbol, "SMART", currency)
        try:
            ib.qualifyContracts(contract)
        except Exception:
            pass
        for t in ib.reqAllOpenOrders():  # bestehende Bracket-/Stop-Orders lösen
            if t.contract.symbol == symbol:
                ib.cancelOrder(t.order)
        order = MarketOrder("SELL", qty)
        order.tif = "DAY"
        ib.placeOrder(contract, order)
        ib.sleep(2)
        log.info("Manueller Verkauf: %d %s (%s)", qty, symbol, currency)
        return "ok"
    except Exception as e:
        log.error("Manueller Verkauf %s fehlgeschlagen: %s", symbol, e)
        return "error"
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def _render_conn():
    c = _CONN_HOLDER["conn"]
    if c is None or getattr(c, "closed", 1):
        c = get_connection()
        _CONN_HOLDER["conn"] = c
    return c


def build_and_write_html():
    """Sammelt DB-Daten + IB-State und schreibt index.html. Thread-sicher (Lock),
    damit Render-Schleife und POST-Handler sich nicht in die Quere kommen. Wird
    nach JEDER ändernden Aktion sofort aufgerufen → Änderungen ohne F5 sichtbar."""
    with _RENDER_LOCK:
        try:
            conn = _render_conn()
            if not _TABLES_READY["ok"]:
                for fn in (ensure_signals_table, ensure_signals_outcome_columns,
                           ensure_watchlist_table, ensure_settings_table,
                           ensure_equity_table, ensure_research_table,
                           ensure_realized_table, ensure_trades_table,
                           ensure_reports_table, ensure_shadow_tables,
                           ensure_catalysts_table, ensure_calendar_table):
                    try:
                        fn(conn)
                    except Exception:
                        pass
                _TABLES_READY["ok"] = True
            account = _STATE.get("account", {})
            holdings = _STATE.get("holdings", [])
            net_liq = account.get("NetLiquidation")
            if net_liq:
                try:
                    insert_equity(conn, net_liq, account.get("TotalCashValue"))
                except Exception:
                    pass
            if account:
                try:
                    upsert_realized(conn, account.get("RealizedPnL", 0.0))
                except Exception:
                    pass
            for f in _STATE.get("fills", []):
                try:
                    upsert_fill(conn, f)
                except Exception:
                    pass
            signals = fetch_recent_signals(conn, limit=150)
            perf = fetch_strategy_performance(conn)
            watchlist = fetch_watchlist(conn)
            settings = load_settings(conn)
            equity = fetch_equity(conn)
            fundamentals = fetch_latest_fundamentals(conn)
            reports = fetch_reports(conn)
            periods = fetch_realized_periods(conn)
            trades = fetch_trades(conn, limit=100)
            shadow_curve = fetch_shadow_equity(conn)
            shadow_target = latest_shadow_portfolio(conn)
            shadow_rebal = count_shadow_rebalances(conn)
            catalysts = fetch_recent_catalysts(conn)
            calendar = fetch_calendar(conn)
            if time.time() - _MACRO["ts"] > 600 or not _MACRO["data"]:
                try:
                    _MACRO["data"] = macro_correlations(conn)
                    _MACRO["ts"] = time.time()
                except Exception as e:
                    log.warning("Makro-Korrelation fehlgeschlagen: %s", e)
            html = render_html(holdings, account, signals, perf, watchlist,
                               settings, equity, fundamentals, reports, periods,
                               trades, shadow_curve, shadow_target, shadow_rebal,
                               catalysts, _MACRO["data"], calendar, REFRESH_SEC)
            tmp = HTML_OUT + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(html)
            os.replace(tmp, HTML_OUT)
        except Exception as e:
            log.error("Render-Fehler: %s", e)
            try:
                _CONN_HOLDER["conn"].close()
            except Exception:
                pass
            _CONN_HOLDER["conn"] = None


class DashboardHandler(BaseHTTPRequestHandler):
    """Liefert die Übersichtsseite (GET) und nimmt Aktionen entgegen
    (Watchlist, Einstellungen, Research, manueller Verkauf)."""

    def log_message(self, *args):  # HTTP-Logs unterdrücken
        pass

    def _send_html(self, status: int = 200):
        try:
            with open(HTML_OUT, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            body = b"<html><body>Lade Daten...</body></html>"
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect_home(self, location: str = "/"):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]      # Query (?sell=…) ignorieren
        if path in ("/", "/index.html"):
            self._send_html()
        elif path == "/news":
            self._send_news()
        elif path == "/newsbrief":
            self._send_brief()
        elif path == "/marketopen":
            self._send_marketopen()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_marketopen(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        q = urllib.parse.parse_qs(qs)
        sym = _clean_ticker(q.get("sym", [""])[0])
        cur = (q.get("cur", ["USD"])[0] or "USD").strip()
        name, hours, op = "", "", False
        if sym:
            try:
                name, hours, op = market_display(sym, cur)
            except Exception as e:
                log.warning("market_display(%s) fehlgeschlagen: %s", sym, e)
        body = json.dumps({"open": bool(op), "market": name,
                           "hours": hours}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_brief(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        sym = _clean_ticker(urllib.parse.parse_qs(qs).get("sym", [""])[0])
        text = ""
        if sym:
            try:
                from .newsbrief import brief
                conn = get_connection()
                text = brief(conn, sym)
                conn.close()
            except Exception as e:
                log.error("Newsbrief fehlgeschlagen (%s): %s", sym, e)
                text = "Zusammenfassung gerade nicht verfügbar."
        body = json.dumps({"brief": text}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_news(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        sym = _clean_ticker(urllib.parse.parse_qs(qs).get("sym", [""])[0])
        items = []
        if sym:
            try:
                conn = get_connection()
                for n in fetch_news_for_ticker(conn, sym, 12):
                    pa = n.get("published_at")
                    items.append({
                        "ts": pa.astimezone(LOCAL_TZ).strftime("%d.%m. %H:%M") if pa else "",
                        "sent": n.get("sentiment"),
                        "source": n.get("source"),
                        "title": n.get("title"),
                    })
                conn.close()
            except Exception as e:
                log.error("News-Abruf fehlgeschlagen (%s): %s", sym, e)
        body = json.dumps(items).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
        fields = urllib.parse.parse_qs(raw)

        if self.path in ("/watchlist/add", "/watchlist/remove"):
            ticker = _clean_ticker(fields.get("ticker", [""])[0])
            if ticker:
                try:
                    conn = get_connection()
                    ensure_watchlist_table(conn)
                    if self.path.endswith("/add"):
                        add_watchlist_ticker(conn, ticker)
                        log.info("Watchlist: %s hinzugefügt", ticker)
                    else:
                        remove_watchlist_ticker(conn, ticker)
                        log.info("Watchlist: %s entfernt", ticker)
                    conn.close()
                except Exception as e:
                    log.error("Watchlist-Änderung fehlgeschlagen (%s): %s", ticker, e)

        elif self.path == "/settings":
            updates = _parse_settings(fields)
            if updates:
                try:
                    conn = get_connection()
                    ensure_settings_table(conn)
                    set_settings(conn, updates)
                    conn.close()
                    log.info("Einstellungen aktualisiert: %s", updates)
                except Exception as e:
                    log.error("Einstellungen speichern fehlgeschlagen: %s", e)

        elif self.path == "/research":
            ticker = _clean_ticker(fields.get("ticker", [""])[0])
            kind = (fields.get("kind", ["deepdive"])[0] or "deepdive")
            if ticker and kind in research.KINDS and research.available():
                try:
                    content = research.generate(kind, ticker)
                    if content:
                        conn = get_connection()
                        ensure_reports_table(conn)
                        save_report(conn, ticker, kind, content)
                        conn.close()
                        log.info("Research %s/%s erzeugt", kind, ticker)
                except Exception as e:
                    log.error("Research fehlgeschlagen (%s/%s): %s", kind, ticker, e)

        elif self.path == "/research/delete":
            rid = (fields.get("id", [""])[0] or "").strip()
            if rid.isdigit():
                try:
                    conn = get_connection()
                    ensure_reports_table(conn)
                    delete_report(conn, int(rid))
                    conn.close()
                    log.info("Bericht %s gelöscht", rid)
                except Exception as e:
                    log.error("Bericht-Löschung fehlgeschlagen (%s): %s", rid, e)

        elif self.path == "/research/delete-folder":
            tkr = _clean_ticker(fields.get("ticker", [""])[0])
            if tkr:
                try:
                    conn = get_connection()
                    ensure_reports_table(conn)
                    delete_reports_for(conn, tkr)
                    conn.close()
                    log.info("Berichts-Ordner %s gelöscht", tkr)
                except Exception as e:
                    log.error("Ordner-Löschung fehlgeschlagen (%s): %s", tkr, e)

        elif self.path == "/sell":
            sym = _clean_ticker(fields.get("symbol", [""])[0])
            cur = (fields.get("currency", ["USD"])[0] or "USD").strip()
            try:
                qty = int(float(fields.get("qty", ["0"])[0]))
            except ValueError:
                qty = 0
            status = place_manual_sell(sym, qty, cur) if (sym and qty > 0) else "error"
            try:
                build_and_write_html()
            except Exception:
                pass
            self._redirect_home("/?sell=" + status)
            return

        # Nach jeder Änderung sofort neu rendern → kein F5/Warten nötig.
        try:
            build_and_write_html()
        except Exception:
            pass
        self._redirect_home()


def write_placeholder():
    os.makedirs(WEB_DIR, exist_ok=True)
    with open(HTML_OUT, "w", encoding="utf-8") as f:
        f.write(PAGE_TEMPLATE
                .replace("__REFRESH__", "5")
                .replace("__SUMMARY__", '<div class="card"><div class="lbl">Status</div>'
                                        '<div class="big">Verbinde mit IB Gateway…</div></div>')
                .replace("__ROWS__", '<tr><td colspan="9" class="empty">Lade Daten…</td></tr>')
                .replace("__SIGNALS__", '<tr><td colspan="7" class="empty">Lade Daten…</td></tr>')
                .replace("__PERFORMANCE__", '<div class="card"><div class="lbl">Performance</div>'
                                            '<div class="big">Lade Daten…</div></div>')
                .replace("__WATCHLIST__", '<span class="wl-empty">Lade Daten…</span>')
                .replace("__SETTINGS__", '')
                .replace("__SHADOW__", '<div class="stamp">Lade Daten…</div>')
                .replace("__CATALYSTS__", '<div class="stamp">Lade Daten…</div>')
                .replace("__MACRO__", '<div class="stamp">Lade Daten…</div>')
                .replace("__CALENDAR__", '<div class="stamp">Lade Daten…</div>')
                .replace("__EQUITY__", '<div class="stamp">Lade Daten…</div>')
                .replace("__FUNDAMENTALS__", '<tr><td colspan="6" class="empty">Lade Daten…</td></tr>')
                .replace("__RESEARCH__", '<div class="stamp">Lade Daten…</div>')
                .replace("__TRADES__", '<tr><td colspan="6" class="empty">Lade Daten…</td></tr>')
                .replace("__TIME__", datetime.now(LOCAL_TZ).strftime("%d.%m.%Y  %H:%M:%S")))


def _ib_loop(state: dict):
    """Eigener Thread: hält Konto + Positionen aktuell und legt sie in `state`.
    Läuft komplett getrennt von der Render-Schleife – hängt oder fehlt das IB
    Gateway, bleibt die Web-Seite (Watchlist, Signale, Performance) trotzdem
    aktuell. ib_async braucht im Thread einen eigenen Event-Loop."""
    asyncio.set_event_loop(asyncio.new_event_loop())
    ib = IB()
    while True:
        try:
            if not ib.isConnected():
                log.info("Verbinde mit IB Gateway %s:%d (clientId=%d) …",
                         IB_HOST, IB_PORT, CLIENT_ID)
                ib.connect(IB_HOST, IB_PORT, clientId=CLIENT_ID,
                           readonly=True, timeout=10)
                ib.reqMarketDataType(4)   # verzögerte Kurse, falls keine Echtzeit-Subscription
                log.info("✅ Mit IB Gateway verbunden.")
            # Snapshot-Abfragen (kein hängendes reqAccountUpdates-Streaming)
            state["account"] = get_account(ib)
            state["holdings"] = get_holdings(ib)
            state["fills"] = get_fills(ib)
            ib.sleep(REFRESH_SEC)
        except Exception as e:
            log.error("IB-Fehler: %s", e)
            state["holdings"], state["account"] = [], {}
            try:
                ib.disconnect()
            except Exception:
                pass
            time.sleep(REFRESH_SEC)


# -------------------------------------------------------------------------
def main():
    write_placeholder()

    httpd = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), DashboardHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("Webserver läuft → http://localhost:%d", WEB_PORT)

    # IB läuft in einem eigenen Thread und kann die Render-Schleife nicht blockieren.
    threading.Thread(target=_ib_loop, args=(_STATE,), daemon=True).start()

    # Periodisch neu rendern; zusätzlich schreiben die POST-Handler sofort neu.
    while True:
        build_and_write_html()
        log.info("Übersicht aktualisiert (%d Positionen).",
                 len(_STATE.get("holdings", [])))
        time.sleep(REFRESH_SEC)


if __name__ == "__main__":
    main()
