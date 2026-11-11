"""The simulated broker: working orders, fills against live quotes, positions, cash, journal.

Its state is one JSON document in the settings table. That suits a single user placing a
handful of orders a day, and keeps every change atomic under one lock.

Fill model: a working limit order fills when the live mid of the whole order is at least as
good as the limit, and fills at the limit (never better). Options that expire are settled
at intrinsic value in cash; real SPY options are physically settled, so an in-the-money
expiry here stands in for an assignment the real account would show as shares.
"""

import asyncio
import copy
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .analysis import TradeLeg, analyze, bp_estimate
from .options import parse_option_symbol
from .orders import Ticket, TicketLeg, schwab_order, strategy_name

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
DEFAULT_COMMISSION = 0.45  # per contract; SIM_COMMISSION overrides
STATE_KEY = "sim_state"
AUDIT_KEEP = 2000


def _empty(start_cash: float) -> dict:
    return {
        "start_cash": start_cash,
        "cash": start_cash,
        "next_id": 1,
        "positions": [],
        "orders": [],
        "trades": [],
        "audit": [],
    }


class SimBroker:
    def __init__(
        self,
        store,
        start_cash: float = 100_000.0,
        clock=lambda: datetime.now(ET),
        commission: float = DEFAULT_COMMISSION,
    ):
        self.store = store
        self.commission = commission
        self.start_cash = start_cash
        self.clock = clock
        self.lock = asyncio.Lock()
        self.state: dict | None = None

    async def load(self) -> dict:
        if self.state is None:
            saved = await self.store.get_setting(STATE_KEY)
            self.state = saved if saved else _empty(self.start_cash)
        return self.state

    async def _save(self) -> None:
        self.state["audit"] = self.state["audit"][-AUDIT_KEEP:]
        await self.store.set_setting(STATE_KEY, self.state)

    def _id(self) -> int:
        i = self.state["next_id"]
        self.state["next_id"] = i + 1
        return i

    def _audit(self, kind: str, **payload) -> None:
        self.state["audit"].append({"at": time.time(), "kind": kind, **payload})

    async def reset(self, start_cash: float) -> None:
        async with self.lock:
            self.state = _empty(start_cash)
            self._audit("reset", start_cash=start_cash)
            await self._save()

    # Queries -------------------------------------------------------------------------

    async def held(self, trade_id: int | None = None) -> dict[str, int]:
        st = await self.load()
        out: dict[str, int] = {}
        for p in st["positions"]:
            if trade_id is None or p["trade_id"] == trade_id:
                out[p["symbol"]] = out.get(p["symbol"], 0) + p["quantity"]
        return out

    async def working_symbols(self) -> set[str]:
        st = await self.load()
        return {
            leg["symbol"] for o in st["orders"] if o["status"] == "WORKING" for leg in o["legs"]
        }

    async def replaceable(self, trade_id, symbols: set[str]) -> list[dict]:
        """Working closing orders of a trade that a new closing order for these legs replaces.

        Two closing orders for the same contracts cannot both stand (the broker would reject
        the second), so the new one replaces the old. Orders for other legs of the trade,
        such as the other side of an iron condor, are left alone.
        """
        st = await self.load()
        return [
            copy.deepcopy(o)
            for o in st["orders"]
            if o["status"] == "WORKING"
            and o["trade_id"] == trade_id
            and o["intent"] in ("close", "roll")
            and symbols & {leg["symbol"] for leg in o["legs"]}
        ]

    async def snapshot(self) -> dict:
        return copy.deepcopy(await self.load())

    # Orders --------------------------------------------------------------------------

    async def place(
        self, t: Ticket, quotes: dict, parent_id: int | None = None, entry: dict | None = None
    ) -> dict:
        """Records a working order and tries to fill it at once."""
        async with self.lock:
            st = await self.load()
            trade = None
            if t.intent in ("close", "roll", "adjust"):
                trade = next(
                    (
                        tr
                        for tr in st["trades"]
                        if tr["id"] == t.trade_id and tr["status"] == "open"
                    ),
                    None,
                )
                if trade is None:
                    raise ValueError("That simulated trade is not open")
            order = {
                "id": self._id(),
                "created_at": time.time(),
                "status": "WORKING",
                "intent": t.intent,
                "trade_id": t.trade_id,
                "underlying": t.underlying,
                "strategy": trade["strategy"] if trade else strategy_name(t),
                "legs": [
                    {"symbol": x.symbol, "quantity": x.quantity, "effect": x.effect} for x in t.legs
                ],
                "units": t.units,
                "limit_net": round(t.limit_net, 2),
                "duration": t.duration,
                "profit_target": t.profit_target,
                "parent_id": parent_id,
                "schwab_json": schwab_order(t),
                "fill_net": None,
                "filled_at": None,
                "reason": None,
                # Conditions when the order was placed, for the dashboard.
                "entry": entry,
            }
            if t.intent in ("close", "roll"):
                symbols = {x.symbol for x in t.legs}
                for o in st["orders"]:
                    if (
                        o["status"] == "WORKING"
                        and o["trade_id"] == t.trade_id
                        and o["intent"] in ("close", "roll")
                        and symbols & {leg["symbol"] for leg in o["legs"]}
                    ):
                        o["status"] = "REPLACED"
                        o["reason"] = f"Replaced by #{order['id']}"
                        order["replaces"] = (order.get("replaces") or []) + [o["id"]]
                        self._audit("replace", order_id=o["id"], by=order["id"])
            st["orders"].append(order)
            self._audit("place", order_id=order["id"], order=order["schwab_json"])
            await self._try_fill(order, quotes)
            await self._save()
            return copy.deepcopy(order)

    async def cancel(self, order_id: int) -> dict:
        async with self.lock:
            st = await self.load()
            order = next((o for o in st["orders"] if o["id"] == order_id), None)
            if order is None:
                raise ValueError("No such order")
            if order["status"] != "WORKING":
                raise ValueError(f"Order is already {order['status'].lower()}")
            order["status"] = "CANCELED"
            order["reason"] = "Canceled"
            self._audit("cancel", order_id=order_id)
            await self._save()
            return copy.deepcopy(order)

    async def on_quotes(self, quotes: dict) -> int:
        """Called with fresh quotes: fills, expiries, settlements. Returns fills made."""
        async with self.lock:
            st = await self.load()
            now = self.clock()
            changed = self._expire_day_orders(now)
            fills = 0
            for order in [o for o in st["orders"] if o["status"] == "WORKING"]:
                if await self._try_fill(order, quotes):
                    fills += 1
                    changed = True
            if self._settle_expired(now.date(), quotes):
                changed = True
            if changed:
                await self._save()
            return fills

    def _expire_day_orders(self, now: datetime) -> bool:
        changed = False
        today_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        for o in self.state["orders"]:
            if o["status"] != "WORKING" or o["duration"] != "DAY":
                continue
            created = datetime.fromtimestamp(o["created_at"], ET)
            if created.date() < now.date() or (now >= today_close and created < today_close):
                o["status"], o["reason"] = "EXPIRED", "Day order expired at the close"
                self._audit("expire_order", order_id=o["id"])
                changed = True
        return changed

    def _mid_net(self, order: dict, quotes: dict) -> float | None:
        u = order["units"]
        net = 0.0
        for leg in order["legs"]:
            q = (quotes.get(leg["symbol"]) or {}).get("quote", {})
            mark = q.get("mark")
            if mark is None:
                bid, ask = q.get("bidPrice"), q.get("askPrice")
                if bid is None or ask is None:
                    return None
                mark = (bid + ask) / 2
            net -= leg["quantity"] / u * float(mark)
        return net

    async def _try_fill(self, order: dict, quotes: dict) -> bool:
        mid = self._mid_net(order, quotes)
        if mid is None or mid + 1e-9 < order["limit_net"]:
            return False
        u = order["units"]
        # Leg prices: each leg's mid, worsened evenly so the total equals the limit.
        weight = sum(abs(leg["quantity"]) / u for leg in order["legs"])
        give = (mid - order["limit_net"]) / weight
        fills = []
        for leg in order["legs"]:
            q = (quotes.get(leg["symbol"]) or {}).get("quote", {})
            mark = q.get("mark")
            if mark is None:
                mark = (q["bidPrice"] + q["askPrice"]) / 2
            px = float(mark) + give if leg["quantity"] > 0 else float(mark) - give
            fills.append({**leg, "price": round(max(px, 0.0), 4)})
        self._apply_fill(order, fills)
        return True

    def _apply_fill(self, order: dict, fills: list[dict]) -> None:
        st = self.state
        contracts = sum(abs(f["quantity"]) for f in fills)
        commission = round(contracts * self.commission, 2)
        net_total = round(sum(-f["quantity"] * f["price"] * 100 for f in fills), 2)
        st["cash"] = round(st["cash"] + net_total - commission, 2)

        if order["intent"] == "open":
            trade = {
                "id": self._id(),
                "underlying": order["underlying"],
                "strategy": order["strategy"],
                "opened_at": time.time(),
                "closed_at": None,
                "status": "open",
                "events": [],
                "realized_pl": None,
                "entry": order.get("entry"),
            }
            st["trades"].append(trade)
            order["trade_id"] = trade["id"]
        else:
            trade = next(tr for tr in st["trades"] if tr["id"] == order["trade_id"])

        for f in fills:
            self._move_position(f, trade["id"])

        kind = {"open": "open", "close": "close", "roll": "roll", "adjust": "adjust"}[
            order["intent"]
        ]
        trade["events"].append(
            {
                "at": time.time(),
                "kind": kind,
                "order_id": order["id"],
                "net": net_total,
                "commission": commission,
                "legs": [
                    {"symbol": f["symbol"], "quantity": f["quantity"], "price": f["price"]}
                    for f in fills
                ],
            }
        )
        order["status"], order["fill_net"], order["filled_at"] = (
            "FILLED",
            order["limit_net"],
            time.time(),
        )
        self._audit(
            "fill",
            order_id=order["id"],
            trade_id=trade["id"],
            net=net_total,
            commission=commission,
            legs=fills,
        )
        if order["intent"] in ("close", "roll", "adjust"):
            # Other working orders for this trade (e.g. a profit target) refer to legs that
            # have just changed, so they can no longer be right.
            for o in self.state["orders"]:
                if (
                    o["status"] == "WORKING"
                    and o["trade_id"] == trade["id"]
                    and o["id"] != order["id"]
                ):
                    o["status"] = "CANCELED"
                    o["reason"] = f"Replaced by {order['intent']} #{order['id']}"
                    self._audit("cancel", order_id=o["id"], note=o["reason"])
        self._close_trade_if_flat(trade)

        if order["intent"] == "open" and order.get("profit_target") and trade["status"] == "open":
            self._place_profit_target(order, trade)

    def _move_position(self, fill: dict, trade_id: int) -> None:
        positions = self.state["positions"]
        pos = next(
            (p for p in positions if p["symbol"] == fill["symbol"] and p["trade_id"] == trade_id),
            None,
        )
        q, px = fill["quantity"], fill["price"]
        if pos is None:
            positions.append(
                {"symbol": fill["symbol"], "trade_id": trade_id, "quantity": q, "avg_price": px}
            )
            return
        old = pos["quantity"]
        new = old + q
        if new == 0:
            positions.remove(pos)
        elif (old > 0) == (q > 0):
            pos["avg_price"] = round((abs(old) * pos["avg_price"] + abs(q) * px) / abs(new), 4)
            pos["quantity"] = new
        elif (old > 0) == (new > 0):
            pos["quantity"] = new  # partly closed; average unchanged
        else:
            pos["quantity"], pos["avg_price"] = new, px  # flipped through zero

    def _close_trade_if_flat(self, trade: dict) -> None:
        if any(p["trade_id"] == trade["id"] for p in self.state["positions"]):
            return
        trade["status"] = "closed"
        trade["closed_at"] = time.time()
        trade["realized_pl"] = round(sum(e["net"] - e["commission"] for e in trade["events"]), 2)

    def _place_profit_target(self, order: dict, trade: dict) -> None:
        """GTC order to close the whole trade at the profit target."""
        opened = order["limit_net"]
        if opened <= 0:
            return  # profit targets only for credit trades
        pct = order["profit_target"]
        legs = [
            {"symbol": p["symbol"], "quantity": -p["quantity"], "effect": "close"}
            for p in self.state["positions"]
            if p["trade_id"] == trade["id"]
        ]
        t = Ticket(
            legs=[TicketLeg.parse(x["symbol"], x["quantity"], "close") for x in legs],
            intent="close",
            duration="GTC",
            limit_net=-round(opened * (1 - pct), 2),
            trade_id=trade["id"],
        )
        child = {
            "id": self._id(),
            "created_at": time.time(),
            "status": "WORKING",
            "intent": "close",
            "trade_id": trade["id"],
            "underlying": order["underlying"],
            "strategy": order["strategy"],
            "legs": legs,
            "units": t.units,
            "limit_net": t.limit_net,
            "duration": "GTC",
            "profit_target": None,
            "parent_id": order["id"],
            "schwab_json": schwab_order(t),
            "fill_net": None,
            "filled_at": None,
            "reason": f"Profit target {round(pct * 100)}%",
        }
        self.state["orders"].append(child)
        self._audit("place", order_id=child["id"], order=child["schwab_json"], note="profit target")

    def _settle_expired(self, today: date, quotes: dict) -> bool:
        """Settles options whose expiration has passed, at intrinsic value."""
        changed = False
        for pos in list(self.state["positions"]):
            occ = parse_option_symbol(pos["symbol"])
            if occ is None or occ.expiration >= today:
                continue
            spot = _price(quotes, occ.underlying)
            if spot is None:
                continue  # wait for a quote
            intrinsic = (
                max(spot - occ.strike, 0) if occ.put_call == "C" else max(occ.strike - spot, 0)
            )
            q = pos["quantity"]
            value = round(q * intrinsic * 100, 2)
            self.state["cash"] = round(self.state["cash"] + value, 2)
            self.state["positions"].remove(pos)
            trade = next(tr for tr in self.state["trades"] if tr["id"] == pos["trade_id"])
            trade["events"].append(
                {
                    "at": time.time(),
                    "kind": "expire" if intrinsic == 0 else "settle",
                    "order_id": None,
                    "net": value,
                    "commission": 0.0,
                    "legs": [
                        {"symbol": pos["symbol"], "quantity": -q, "price": round(intrinsic, 4)}
                    ],
                }
            )
            self._audit("settle", symbol=pos["symbol"], quantity=q, intrinsic=intrinsic)
            # Working orders on a settled leg can no longer fill.
            for o in self.state["orders"]:
                if o["status"] == "WORKING" and any(
                    leg["symbol"] == pos["symbol"] for leg in o["legs"]
                ):
                    o["status"], o["reason"] = "CANCELED", "Leg expired"
            self._close_trade_if_flat(trade)
            changed = True
        return changed

    # Account view --------------------------------------------------------------------

    def account(self, quotes: dict) -> dict:
        """The simulated account in the same shape as Schwab's account response."""
        st = self.state
        positions = []
        value = 0.0
        by_trade: dict[int, list] = {}
        for p in st["positions"]:
            occ = parse_option_symbol(p["symbol"])
            mark = _price(quotes, p["symbol"])
            mv = None if mark is None else round(p["quantity"] * mark * 100, 2)
            value += mv or 0.0
            positions.append(
                {
                    "longQuantity": max(p["quantity"], 0),
                    "shortQuantity": max(-p["quantity"], 0),
                    "averagePrice": p["avg_price"],
                    "marketValue": mv,
                    "tradeId": p["trade_id"],
                    "instrument": {
                        "assetType": "OPTION",
                        "symbol": p["symbol"],
                        "underlyingSymbol": occ.underlying,
                        "putCall": "PUT" if occ.put_call == "P" else "CALL",
                    },
                }
            )
            by_trade.setdefault(p["trade_id"], []).append((p, occ, mark))
        maint = 0.0
        for legs in by_trade.values():
            spot = _price(quotes, legs[0][1].underlying)
            if not spot:
                continue
            tl = [
                TradeLeg(o.put_call, o.strike, o.expiration, p["quantity"], m or 0.0)
                for p, o, m in legs
            ]
            width = [
                TradeLeg(o.put_call, o.strike, o.expiration, p["quantity"], 0.0) for p, o, m in legs
            ]
            worst = analyze(width, spot, self.clock().date())
            max_loss = -worst["max_loss"] if worst["max_loss"] is not None else float("-inf")
            maint += bp_estimate(tl, spot, max_loss)
        net_liq = round(st["cash"] + value, 2)
        return {
            "securitiesAccount": {
                "accountNumber": "SIM0",
                "type": "SIMULATED",
                "positions": positions,
                "currentBalances": {
                    "liquidationValue": net_liq,
                    "cashBalance": st["cash"],
                    "maintenanceRequirement": round(maint, 2),
                    "buyingPower": round(net_liq - maint, 2),
                },
            }
        }

    def trade_info(self) -> dict[int, dict]:
        """Per open trade: strategy name and net opened including rolls (for positions)."""
        out = {}
        for tr in self.state["trades"]:
            if tr["status"] == "open":
                out[tr["id"]] = {
                    "strategy": tr["strategy"],
                    "net": round(sum(e["net"] for e in tr["events"]), 2),
                    "rolls": sum(1 for e in tr["events"] if e["kind"] == "roll"),
                    "opened_at": tr["opened_at"],
                }
        return out


def stats(trades: list[dict]) -> list[dict]:
    """Closed-trade statistics by strategy."""
    groups: dict[str, list] = {}
    for tr in trades:
        # Chains carried in from before the history have no reliable opening price.
        if tr["status"] == "closed" and not tr.get("carried_in"):
            groups.setdefault(tr["strategy"], []).append(tr)
    out = []
    for name, ts in sorted(groups.items()):
        pls = [t["realized_pl"] for t in ts]
        # A trade held under a day counts as one day, so P/L per day stays meaningful.
        days = [max((t["closed_at"] - t["opened_at"]) / 86400, 1.0) for t in ts]
        out.append(
            {
                "strategy": name,
                "trades": len(ts),
                "win_rate": round(sum(1 for p in pls if p > 0) / len(pls), 4),
                "total_pl": round(sum(pls), 2),
                "avg_pl": round(sum(pls) / len(pls), 2),
                "avg_days": round(sum(days) / len(days), 1),
                "pl_per_day": round(sum(pls) / sum(days), 2),
            }
        )
    return out


def _price(quotes: dict, symbol: str) -> float | None:
    q = (quotes.get(symbol) or {}).get("quote", {})
    for k in ("mark", "lastPrice", "closePrice"):
        v = q.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    bid, ask = q.get("bidPrice"), q.get("askPrice")
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2
    return None


class SimDataSource:
    """Market data from the real source; the account from the simulated broker."""

    HASH = "SIM"

    def __init__(self, base, broker: SimBroker):
        self.base = base
        self.broker = broker

    async def account_numbers(self) -> list[dict]:
        return [{"accountNumber": "SIM0", "hashValue": self.HASH}]

    async def account(self, account_hash: str) -> dict:
        st = await self.broker.load()
        symbols = {p["symbol"] for p in st["positions"]}
        symbols |= {parse_option_symbol(s).underlying for s in symbols}
        quotes = await self.base.quotes(sorted(symbols)) if symbols else {}
        return self.broker.account(quotes)

    async def quotes(self, symbols: list[str]) -> dict:
        return await self.base.quotes(symbols)

    async def daily_closes(self, symbol, days):
        return await self.base.daily_closes(symbol, days)

    async def option_chain(self, *args, **kwargs):
        return await self.base.option_chain(*args, **kwargs)

    async def expirations(self, symbol):
        return await self.base.expirations(symbol)

    async def market_open(self, day):
        return await self.base.market_open(day)
