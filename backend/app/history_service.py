"""Daily price history for the dashboard, and export in the spreadsheet's format."""

import asyncio
import io
import logging
import time
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from .dashboard import price_symbol
from .options import parse_option_symbol
from .schwab_service import SchwabError, SchwabNotConnected

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
FETCHED_KEY = "prices_fetched"
REFRESH_DAYS = 7
PAUSE_S = 1.5  # between symbols, to stay well inside Schwab's limits


class PriceHistoryService:
    """Keeps daily closes for VIX and every underlying traded, back to the first trade."""

    def __init__(self, source, store, chain_services, sim_trades=None):
        """chain_services: every Schwab account's ChainService (one, or a dict of them)."""
        self.source = source
        self.store = store
        self.chain_services = chain_services
        self.sim_trades = sim_trades
        self.status: dict = {"pending": None, "error": None}

    def symbols(self) -> tuple[set[str], float | None]:
        services = self.chain_services
        services = services.values() if isinstance(services, dict) else [services]
        chains = [c for cs in list(services) for c in cs.result["chains"]]
        if self.sim_trades:
            chains += self.sim_trades()
        unders = {c["underlying"] for c in chains if not str(c["underlying"]).startswith("/")}
        first = min((c["opened_at"] for c in chains if c.get("opened_at")), default=None)
        return {price_symbol(u) for u in unders} | {"$VIX"}, first

    async def run(self) -> None:
        while True:
            try:
                await self.fill()
            except asyncio.CancelledError:
                raise
            except (SchwabNotConnected, SchwabError) as e:
                self.status["error"] = str(e)
            except Exception as e:
                log.exception("Price history fill failed")
                self.status["error"] = f"{type(e).__name__}: {e}"
            await asyncio.sleep(600)

    async def fill(self) -> int:
        symbols, first = self.symbols()
        if first is None:
            return 0
        fetched = await self.store.get_setting(FETCHED_KEY) or {}
        due = sorted(s for s in symbols if time.time() - fetched.get(s, 0) > REFRESH_DAYS * 86400)
        self.status["pending"] = len(due)
        days = (datetime.now(UTC) - datetime.fromtimestamp(first, UTC)).days + 30
        done = 0
        for sym in due:
            try:
                closes = await self.source.daily_closes(sym, days)
            except SchwabNotConnected:
                raise
            except SchwabError as e:
                # Delisted or unknown symbols: remember the attempt and move on.
                log.info("No price history for %s: %s", sym, e)
                closes = []
            if closes:
                await self.store.upsert_prices(sym, closes)
            fetched[sym] = time.time()
            await self.store.set_setting(FETCHED_KEY, fetched)
            done += 1
            self.status["pending"] = len(due) - done
            await asyncio.sleep(PAUSE_S)
        self.status["error"] = None
        return done


# Export in the spreadsheet's layout ---------------------------------------------------

HEADER = [
    "R",
    "Strikes",
    "Strat",
    "Exp",
    "Strike/Diff.",
    "Date",
    "Stock Price",
    "Contracts",
    "Shares",
    "Prem/Price",
    "Commission",
    "P/L",
    "Breakeven",
    "Notional Value",
    "Max Loss",
    "BPR",
    "Account",
    "Status ",
    "Date",
    "Contracts",
    "Shares",
    "Prem/Price",
    "Commission",
    "Total cost",
    "P/L",
    "% Return",
    "Days Held",
]


def export_rows(chains: list[dict], start: date, end: date, account: str = "Joint") -> list[list]:
    """One row per opened leg, closed or still open, laid out like the Trades tab."""
    rows = []
    for c in chains:
        if c.get("source") == "sheet":
            continue  # already in the spreadsheet
        lots: dict[str, list[dict]] = {}
        for e in c["events"]:
            units = sum(abs(x["quantity"]) for x in e["legs"]) or 1
            fee_unit = e["commission"] / units
            day = datetime.fromtimestamp(e["at"], ET).date()
            for leg in e["legs"]:
                sym, q, px = leg["symbol"], leg["quantity"], leg["price"]
                book = lots.setdefault(sym, [])
                left = q
                while book and abs(left) > 1e-9 and book[0]["qty"] * left < 0:
                    lot = book[0]
                    take = min(abs(left), abs(lot["qty"]))
                    status = {
                        "roll": "Rolled",
                        "expire": "Expired",
                        "assign": "Assigned",
                        "exercise": "Assigned",
                        "settle": "Expired",
                    }.get(e["kind"], "Closed")
                    rows.append(_row(lot, take, status, day, px, fee_unit * take, account))
                    sign = 1 if lot["qty"] > 0 else -1
                    lot["fee"] -= lot["fee"] * take / abs(lot["qty"])
                    lot["qty"] -= sign * take
                    left += sign * take
                    if abs(lot["qty"]) <= 1e-9:
                        book.pop(0)
                if abs(left) > 1e-9:
                    book.append(
                        {
                            "symbol": sym,
                            "qty": left,
                            "price": px,
                            "day": day,
                            "fee": fee_unit * abs(left),
                            "underlying": c["underlying"],
                        }
                    )
        for book in lots.values():
            for lot in book:
                rows.append(_row(lot, abs(lot["qty"]), "Open", None, None, 0.0, account))
    rows = [r for r in rows if r[5] and start <= r[5] <= end]
    rows.sort(key=lambda r: r[5])
    return rows


def _row(lot: dict, qty: float, status: str, day, close_px, close_fee, account) -> list:
    occ = parse_option_symbol(lot["symbol"])
    long_ = lot["qty"] > 0
    open_fee = lot["fee"] * qty / abs(lot["qty"]) if lot["qty"] else 0.0
    if occ:
        strat = occ.put_call + ("+" if long_ else "")
        shares = qty * 100
        strike, exp = occ.strike, occ.expiration
    else:
        strat, shares, strike, exp = None, qty, None, None
    prem = lot["price"]
    open_total = prem * shares
    close_total = (close_px or 0.0) * shares if status != "Open" else None
    pl = None
    if status != "Open":
        pl = close_total - open_total if long_ else open_total - close_total
        pl -= open_fee + close_fee
    breakeven = None
    if occ:
        breakeven = strike - prem if (occ.put_call == "P" and not long_) else strike + prem
    return [
        lot["underlying"],
        strike,
        strat,
        exp,
        strike,
        lot["day"],
        None,
        1,
        shares,
        prem,
        round(open_fee, 2),
        round(open_total, 2),
        breakeven,
        (shares * strike) if strike else None,
        (strike * shares - open_total) if strike else None,
        None,
        account,
        status,
        day,
        1 if status != "Open" else None,
        shares if status != "Open" else None,
        close_px,
        round(close_fee, 2) if status != "Open" else None,
        round(close_total, 2) if close_total is not None else None,
        round(pl, 2) if pl is not None else None,
        round(pl / open_total, 4) if (pl is not None and open_total) else None,
        (day - lot["day"]).days if day else None,
    ]


def export_xlsx(rows: list[list]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trades"
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    for col in ("D", "F", "S"):
        for cell in ws[col][1:]:
            cell.number_format = "m/d/yyyy"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
