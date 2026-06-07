"""src/executor.py
Verbindet den Bot mit Interactive Brokers über IB Gateway.
"""
import logging
import sys
import time

# Logging VOR ib_async-Import aufsetzen, damit wir die Kontrolle behalten
def _setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # ib_async-interne Logs auf WARNING reduzieren
    logging.getLogger("ib_async").setLevel(logging.WARNING)

_setup_logging()
log = logging.getLogger("executor")

from ib_async import IB, LimitOrder, MarketOrder, Order, Stock, StopOrder  # noqa: E402
from .config import config  # noqa: E402


class IBKRExecutor:
    def __init__(self):
        self.ib = IB()

    def connect(self, client_id: int = None):
        cid = client_id if client_id is not None else config.IBKR_CLIENT_ID
        attempts = 0
        while True:
            try:
                self.ib.connect(
                    host=config.IBKR_HOST,
                    port=config.IBKR_PORT,
                    clientId=cid,
                    readonly=False,
                )
                log.info(
                    "✅ Verbunden mit IB Gateway (%s:%d) | Konto: %s",
                    config.IBKR_HOST, config.IBKR_PORT,
                    self.ib.managedAccounts(),
                )
                return
            except Exception as exc:
                attempts += 1
                log.warning("Verbindung fehlgeschlagen (Versuch %d): %s", attempts, exc)
                log.info("Nächster Versuch in 15 Sekunden ...")
                time.sleep(15)

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    def ensure_connected(self):
        if not self.ib.isConnected():
            log.warning("Verbindung verloren – reconnect ...")
            self.connect()

    def print_account_summary(self):
        self.ensure_connected()
        try:
            summary = self.ib.accountSummary()
            interesting = {"TotalCashValue", "NetLiquidation",
                           "UnrealizedPnL", "RealizedPnL", "AvailableFunds"}
            log.info("--- Kontostand (Paper) ---")
            for item in summary:
                if item.tag in interesting:
                    log.info("  %-22s %12s %s",
                             item.tag, item.value, item.currency)
        except Exception as e:
            log.warning("accountSummary Fehler: %s", e)

        try:
            positions = self.ib.positions()
            if positions:
                log.info("--- Offene Positionen ---")
                for p in positions:
                    log.info("  %-8s %+6.0f @ %.2f",
                             p.contract.symbol, p.position, p.avgCost)
            else:
                log.info("  Keine offenen Positionen.")
        except Exception as e:
            log.warning("positions Fehler: %s", e)
        log.info("---")

    def get_positions(self) -> dict[str, float]:
        """Aktuell gehaltene Stückzahlen je Symbol {symbol: qty}.
        Positiv = long, negativ = short (sollte bei uns nie vorkommen)."""
        self.ensure_connected()
        out: dict[str, float] = {}
        for p in self.ib.positions():
            out[p.contract.symbol] = out.get(p.contract.symbol, 0.0) + p.position
        return out

    def get_open_buy_symbols(self) -> set:
        """Symbole, für die gerade eine KAUF-Order offen (noch nicht gefüllt)
        ist. Verhindert Doppelkäufe, wenn eine Order noch nicht durch ist."""
        self.ensure_connected()
        return {t.contract.symbol for t in self.ib.openTrades()
                if t.order.action == "BUY"}

    def cancel_orders_for(self, ticker: str):
        """Storniert alle offenen Orders eines Symbols (z.B. die Stop/Take-
        Profit-Klammern), bevor eine Position aktiv geschlossen wird."""
        self.ensure_connected()
        for trade in self.ib.openTrades():
            if trade.contract.symbol == ticker:
                self.ib.cancelOrder(trade.order)
                log.info("  Offene Order für %s storniert (orderId %s)",
                         ticker, trade.order.orderId)

    def _contract(self, ticker: str, currency: str = "USD"):
        """Baut den Aktien-Kontrakt (SMART-Routing + Währung) und lässt ihn von
        IB auflösen. So funktionieren US- und Auslandswerte gleichermaßen."""
        contract = Stock(ticker, "SMART", currency)
        try:
            qualified = self.ib.qualifyContracts(contract)
            if qualified:
                return qualified[0]
        except Exception as e:
            log.warning("  Kontrakt %s (%s) nicht eindeutig auflösbar: %s",
                        ticker, currency, e)
        return contract

    def place_market_order(self, ticker: str, direction: str, quantity: int,
                           currency: str = "USD"):
        self.ensure_connected()
        contract = self._contract(ticker, currency)
        order = MarketOrder(direction.upper(), quantity)
        order.tif = "DAY"   # explizit, sonst Cancel via Error 10349 (Order-Preset)
        trade = self.ib.placeOrder(contract, order)
        log.info("📤 Market-Order: %s %d %s (%s)",
                 direction.upper(), quantity, ticker, currency)
        return trade

    def place_bracket_order(self, ticker: str, quantity: int, entry_price: float,
                            stop_pct: float, take_pct: float,
                            currency: str = "USD"):
        """Kauf mit broker-seitiger Absicherung – robuste Variante:
          1. Market-Kauf, der sofort überträgt und zuverlässig füllt.
          2. Erst NACH dem Fill werden Stop-Loss und Take-Profit als
             OCA-Gruppe (eine Order storniert die andere, TIF GTC) angehängt.
        Vermeidet die fragile Parent/Child-Transmit-Kette (Error 10349,
        hängende PendingSubmit-Orders). Ohne stop/take_pct nur Market-Kauf."""
        self.ensure_connected()
        contract = self._contract(ticker, currency)

        # 1) Market-Kauf sofort übertragen
        buy = MarketOrder("BUY", quantity)
        buy.tif = "DAY"
        trade = self.ib.placeOrder(contract, buy)
        log.info("📤 Market-Kauf: %d %s (%s)", quantity, ticker, currency)

        # auf Fill warten (max. ~10s); während der Börsenzeit füllt Market sofort
        for _ in range(20):
            self.ib.sleep(0.5)
            if trade.orderStatus.status == "Filled":
                break
        if trade.orderStatus.status != "Filled":
            log.warning("  %s: Kauf noch nicht gefüllt (Status %s) – Stop/TP "
                        "wird diesmal NICHT gesetzt", ticker,
                        trade.orderStatus.status)
            return [trade]

        if not (stop_pct and take_pct):
            return [trade]

        # 2) Absicherung an den tatsächlichen Fill-Kurs hängen (OCA-Gruppe):
        #    Take-Profit (fix) + TRAILING-Stop (zieht mit steigendem Kurs nach).
        fill = trade.orderStatus.avgFillPrice or entry_price
        take_price = round(fill * (1 + take_pct), 2)
        stop_init = round(fill * (1 - stop_pct), 2)   # anfänglicher Stop-Trigger
        oca = "oca_%s_%d" % (ticker, int(time.time()))

        take = LimitOrder("SELL", quantity, take_price)
        take.ocaGroup, take.ocaType, take.tif = oca, 1, "GTC"

        # Trailing-Stop: bleibt stop_pct unter dem höchsten erreichten Kurs.
        # WICHTIG: trailingPercent UND auxPrice gleichzeitig → Error 320.
        # Daher trailStopPrice (Start-Trigger) statt auxPrice verwenden.
        trail = Order(orderType="TRAIL", action="SELL", totalQuantity=quantity,
                      trailingPercent=round(stop_pct * 100, 2),
                      trailStopPrice=stop_init)
        trail.ocaGroup, trail.ocaType, trail.tif = oca, 1, "GTC"

        t_take = self.ib.placeOrder(contract, take)
        t_trail = self.ib.placeOrder(contract, trail)
        log.info("  🛡️  %s abgesichert: Trailing-Stop %.0f%% (Start %.2f) / "
                 "Ziel %.2f (Fill %.2f, OCA)",
                 ticker, stop_pct * 100, stop_init, take_price, fill)
        return [trade, t_take, t_trail]

    def protect_position(self, ticker: str, quantity: int, stop_pct: float,
                         take_pct: float, ref_price: float, currency: str = "USD"):
        """Hängt frische OCA-Absicherung (Trailing-Stop + Take-Profit) über
        `quantity` Stück an eine bestehende Position. Storniert vorher ALLE
        offenen Orders des Symbols – wichtig nach einem Teilverkauf, damit der
        alte Stop nicht die ursprüngliche (größere) Menge verkauft (Oversell)."""
        self.ensure_connected()
        self.cancel_orders_for(ticker)
        self.ib.sleep(0.6)
        contract = self._contract(ticker, currency)
        take_price = round(ref_price * (1 + take_pct), 2)
        stop_init = round(ref_price * (1 - stop_pct), 2)
        oca = "oca_%s_%d" % (ticker, int(time.time()))
        take = LimitOrder("SELL", quantity, take_price)
        take.ocaGroup, take.ocaType, take.tif = oca, 1, "GTC"
        trail = Order(orderType="TRAIL", action="SELL", totalQuantity=quantity,
                      trailingPercent=round(stop_pct * 100, 2),
                      trailStopPrice=stop_init)
        trail.ocaGroup, trail.ocaType, trail.tif = oca, 1, "GTC"
        t_take = self.ib.placeOrder(contract, take)
        t_trail = self.ib.placeOrder(contract, trail)
        log.info("  🛡️  %s neu abgesichert: %d Stück, Trailing-Stop %.0f%%",
                 ticker, quantity, stop_pct * 100)
        return [t_take, t_trail]

    def place_limit_order(self, ticker: str, direction: str,
                          quantity: int, limit_price: float):
        self.ensure_connected()
        contract = Stock(ticker, "SMART", "USD")
        order = LimitOrder(direction.upper(), quantity, limit_price)
        trade = self.ib.placeOrder(contract, order)
        log.info("📤 Limit-Order: %s %d %s @ %.2f",
                 direction.upper(), quantity, ticker, limit_price)
        return trade

    def cancel_all_orders(self):
        self.ensure_connected()
        self.ib.reqGlobalCancel()
        log.warning("🛑 Alle offenen Orders storniert.")


def main():
    log.info("IBKR-Executor startet. Verbinde mit %s:%d ...",
             config.IBKR_HOST, config.IBKR_PORT)
    ex = IBKRExecutor()
    ex.connect()
    while True:
        try:
            ex.print_account_summary()
        except Exception as e:
            log.error("Fehler: %s", e)
        time.sleep(300)


if __name__ == "__main__":
    main()
