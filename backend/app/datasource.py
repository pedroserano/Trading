"""Where account and market data come from: Schwab, or a simulator for demo mode."""

import math
import random
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from .options import bs, option_symbol, parse_option_symbol
from .schwab_service import SchwabService

VIX = "$VIX"


class DataSource(Protocol):
    async def account_numbers(self) -> list[dict]: ...
    async def account(self, account_hash: str) -> dict: ...
    async def quotes(self, symbols: list[str]) -> dict: ...
    async def daily_closes(self, symbol: str, days: int) -> list[tuple[date, float]]: ...
    async def option_chain(
        self, symbol: str, from_date: date, to_date: date, strike_count: int | None = 6
    ) -> dict: ...
    async def expirations(self, symbol: str) -> dict: ...
    async def transactions(self, account_hash: str, start: datetime, end: datetime) -> list: ...
    async def market_open(self, day: date) -> bool | None: ...
    async def orders(self, account_hash: str, start: datetime, end: datetime) -> list: ...


class SchwabDataSource:
    def __init__(self, schwab: SchwabService):
        self.schwab = schwab

    async def account_numbers(self) -> list[dict]:
        return await self.schwab.call("get_account_numbers") or []

    async def account(self, account_hash: str) -> dict:
        c = self.schwab.client()
        return await self.schwab.call(
            "get_account", account_hash, fields=[c.Account.Fields.POSITIONS]
        )

    async def quotes(self, symbols: list[str]) -> dict:
        out = {}
        # Keep each request modest in size.
        for i in range(0, len(symbols), 200):
            out.update(await self.schwab.call("get_quotes", symbols[i : i + 200]) or {})
        return out

    async def daily_closes(self, symbol: str, days: int) -> list[tuple[date, float]]:
        start = datetime.now(UTC) - timedelta(days=days)
        raw = await self.schwab.call("get_price_history_every_day", symbol, start_datetime=start)
        out = []
        for c in (raw or {}).get("candles", []):
            d = datetime.fromtimestamp(c["datetime"] / 1000, UTC).date()
            out.append((d, float(c["close"])))
        return out

    async def option_chain(
        self, symbol: str, from_date: date, to_date: date, strike_count: int | None = 6
    ) -> dict:
        """strike_count None returns every strike."""
        c = self.schwab.client()
        return await self.schwab.call(
            "get_option_chain",
            symbol,
            contract_type=c.Options.ContractType.ALL,
            strike_count=strike_count,
            from_date=from_date,
            to_date=to_date,
        )

    async def expirations(self, symbol: str) -> dict:
        return await self.schwab.call("get_option_expiration_chain", symbol)

    async def transactions(self, account_hash: str, start: datetime, end: datetime) -> list:
        c = self.schwab.client()
        types = [
            c.Transactions.TransactionType.TRADE,
            c.Transactions.TransactionType.RECEIVE_AND_DELIVER,
        ]
        return (
            await self.schwab.call(
                "get_transactions",
                account_hash,
                start_date=start,
                end_date=end,
                transaction_types=types,
            )
            or []
        )

    async def orders(self, account_hash: str, start: datetime, end: datetime) -> list:
        return (
            await self.schwab.call(
                "get_orders_for_account",
                account_hash,
                from_entered_datetime=start,
                to_entered_datetime=end,
                max_results=500,
            )
            or []
        )

    async def market_open(self, day: date) -> bool | None:
        c = self.schwab.client()
        raw = await self.schwab.call("get_market_hours", [c.MarketHours.Market.OPTION], date=day)
        try:
            markets = raw["option"]
            return any(m.get("isOpen") for m in markets.values())
        except (KeyError, TypeError, AttributeError):
            return None


# Demo simulator ---------------------------------------------------------------------------

_DEMO_UNDERLYINGS = {
    # symbol: (price, base IV as fraction, beta)
    "SPY": (590.0, 0.15, 1.0),
    "QQQ": (510.0, 0.19, 1.15),
    "IWM": (225.0, 0.22, 1.2),
    "AAPL": (230.0, 0.24, 1.1),
}


def _friday_after(d: date) -> date:
    return d + timedelta(days=(4 - d.weekday()) % 7)


class DemoDataSource:
    """A plausible account that moves. Prices random-walk; options priced by Black-Scholes."""

    HASH = "DEMOHASH"

    def __init__(self, today: date | None = None, seed: int = 7):
        self.rng = random.Random(seed)  # noqa: S311 - simulated prices
        self.today = today or date.today()
        self.prices = {s: p for s, (p, _, _) in _DEMO_UNDERLYINGS.items()}
        self.prev_close = dict(self.prices)
        self.vix = 16.4
        self.positions = self._build_positions()

    def _exp(self, days: int) -> date:
        return _friday_after(self.today + timedelta(days=days))

    def _build_positions(self) -> list[dict]:
        def opt(under, days, pc, strike, qty, avg):
            sym = option_symbol(under, self._exp(days), pc, strike)
            return {
                "longQuantity": max(qty, 0),
                "shortQuantity": max(-qty, 0),
                "averagePrice": avg,
                "instrument": {
                    "assetType": "OPTION",
                    "symbol": sym,
                    "underlyingSymbol": under,
                    "putCall": "PUT" if pc == "P" else "CALL",
                },
            }

        return [
            # SPY strangle, mid-cycle, near the profit target.
            opt("SPY", 38, "P", 555, -1, 4.85),
            opt("SPY", 38, "C", 610, -1, 3.95),
            # SPY put vertical inside 21 DTE.
            opt("SPY", 16, "P", 575, -2, 3.40),
            opt("SPY", 16, "P", 570, 2, 2.35),
            # QQQ iron condor, freshly opened.
            opt("QQQ", 45, "P", 470, 1, 2.10),
            opt("QQQ", 45, "P", 475, -1, 2.75),
            opt("QQQ", 45, "C", 540, -1, 2.30),
            opt("QQQ", 45, "C", 545, 1, 1.60),
            # IWM short put, tested.
            opt("IWM", 25, "P", 224, -2, 3.10),
            # AAPL covered call.
            {
                "longQuantity": 100,
                "shortQuantity": 0,
                "averagePrice": 218.40,
                "instrument": {"assetType": "EQUITY", "symbol": "AAPL"},
            },
            opt("AAPL", 30, "C", 240, -1, 3.20),
        ]

    def _vol(self, under: str, strike: float, pc: str) -> float:
        base = _DEMO_UNDERLYINGS[under][1] * self.vix / 16.4
        moneyness = math.log(strike / self.prices[under])
        return max(0.05, base - 0.9 * moneyness * (1 if moneyness < 0 else 0.4))

    def _option_quote(self, sym: str) -> dict:
        occ = parse_option_symbol(sym)
        s = self.prices[occ.underlying]
        t = (occ.expiration - self.today).days / 365
        vol = self._vol(occ.underlying, occ.strike, occ.put_call)
        g = bs(occ.put_call, s, occ.strike, t, vol)
        spread = max(0.01, round(g["price"] * 0.02, 2))
        return {
            "assetMainType": "OPTION",
            "quote": {
                "mark": round(g["price"], 2),
                "bidPrice": round(max(g["price"] - spread / 2, 0), 2),
                "askPrice": round(g["price"] + spread / 2, 2),
                "delta": round(g["delta"], 4),
                "gamma": round(g["gamma"], 5),
                "theta": round(g["theta"], 4),
                "vega": round(g["vega"], 4),
                "volatility": round(vol * 100, 2),
                "underlyingPrice": round(s, 2),
            },
        }

    def step(self):
        for s in self.prices:
            beta = _DEMO_UNDERLYINGS.get(s, (0, 0, 1.0))[2]
            self.prices[s] *= math.exp(self.rng.gauss(0, 0.0004 * beta))
        self.vix = max(10.0, self.vix * math.exp(self.rng.gauss(0, 0.002)))

    async def account_numbers(self) -> list[dict]:
        return [{"accountNumber": "00001234", "hashValue": self.HASH}]

    async def account(self, account_hash: str) -> dict:
        positions = []
        maint = 0.0
        for p in self.positions:
            inst = p["instrument"]
            qty = p["longQuantity"] - p["shortQuantity"]
            if inst["assetType"] == "OPTION":
                mark = self._option_quote(inst["symbol"])["quote"]["mark"]
                mv = qty * mark * 100
                if qty < 0:
                    occ = parse_option_symbol(inst["symbol"])
                    maint += 0.2 * self.prices[occ.underlying] * 100 * abs(qty) * 0.2
            else:
                mv = qty * self.prices[inst["symbol"]]
                maint += abs(mv) * 0.25
            positions.append({**p, "marketValue": round(mv, 2)})
        return {
            "securitiesAccount": {
                "accountNumber": "00001234",
                "type": "MARGIN",
                "positions": positions,
                "currentBalances": {
                    "liquidationValue": round(78500 + sum(x["marketValue"] for x in positions), 2),
                    "cashBalance": 78500.0,
                    "buyingPower": 140000.0,
                    "maintenanceRequirement": round(maint, 2),
                },
            }
        }

    async def quotes(self, symbols: list[str]) -> dict:
        self.step()
        out = {}
        for sym in symbols:
            if sym == VIX:
                out[sym] = {"assetMainType": "INDEX", "quote": {"lastPrice": round(self.vix, 2)}}
            elif sym in self.prices or not parse_option_symbol(sym):
                p = self._price_of(sym)
                out[sym] = {
                    "assetMainType": "EQUITY",
                    # As Schwab marks funds; AAPL is a single stock.
                    "assetSubType": "ETF" if sym in ("SPY", "QQQ", "IWM") else None,
                    "quote": {
                        "mark": round(p, 2),
                        "lastPrice": round(p, 2),
                        "closePrice": round(self.prev_close[sym], 2),
                        "netPercentChange": round((p / self.prev_close[sym] - 1) * 100, 2),
                    },
                }
            elif parse_option_symbol(sym):
                out[sym] = self._option_quote(sym)
        return out

    async def daily_closes(self, symbol: str, days: int) -> list[tuple[date, float]]:
        rng = random.Random(sum(map(ord, symbol)))  # noqa: S311 - simulated prices
        out, v = [], 1.0
        start = self.today - timedelta(days=days)
        d = start
        while d < self.today:
            if d.weekday() < 5:
                out.append((d, v))
            v *= math.exp(rng.gauss(0, 0.01))
            d += timedelta(days=1)
        if symbol == VIX:
            # Map the walk onto a VIX-like range of roughly 12 to 30.
            lo, hi = min(x for _, x in out), max(x for _, x in out)
            return [(d, 12 + (x - lo) / (hi - lo) * 18) for d, x in out]
        base = self.prices.get(symbol, 100.0)
        return [(d, base * x / out[-1][1]) for d, x in out]

    def _price_of(self, symbol: str) -> float:
        if symbol not in self.prices:
            # Unknown demo symbols get a stable made-up price.
            self.prices[symbol] = 50.0 + sum(map(ord, symbol)) % 250
            self.prev_close[symbol] = self.prices[symbol]
        return self.prices[symbol]

    def _demo_expirations(self, until: date) -> list[date]:
        out, d = [], _friday_after(self.today + timedelta(days=1))
        while d <= until:
            out.append(d)
            d += timedelta(days=7)
        return out

    async def expirations(self, symbol: str) -> dict:
        self._price_of(symbol)
        return {
            "expirationList": [
                {"expirationDate": d.isoformat(), "daysToExpiration": (d - self.today).days}
                for d in self._demo_expirations(self.today + timedelta(days=120))
            ]
        }

    async def option_chain(
        self, symbol: str, from_date: date, to_date: date, strike_count: int | None = 6
    ) -> dict:
        s = self._price_of(symbol)
        if strike_count is None:
            # Every strike: roughly 40% either side, like a real listed chain.
            step0 = 5 if s > 200 else (1 if s > 50 else 0.5)
            strike_count = int(s * 0.4 / step0)
        calls, puts = {}, {}
        step = 5 if s > 200 else (1 if s > 50 else 0.5)
        atm = round(s / step) * step
        strikes = [atm + step * i for i in range(-strike_count, strike_count + 1)]
        for d in self._demo_expirations(to_date):
            if d < from_date:
                continue
            days = (d - self.today).days
            key = f"{d.isoformat()}:{days}"
            calls[key], puts[key] = {}, {}
            for k in strikes:
                if k <= 0:
                    continue
                for pc, m in (("C", calls), ("P", puts)):
                    vol = self._vol(symbol, k, pc) if symbol in _DEMO_UNDERLYINGS else 0.3
                    g = bs(pc, s, k, days / 365, vol)
                    spread = max(0.01, round(g["price"] * 0.03, 2))
                    m[key][f"{k:.1f}"] = [
                        {
                            "symbol": option_symbol(symbol, d, pc, k),
                            "bid": round(max(g["price"] - spread / 2, 0), 2),
                            "ask": round(g["price"] + spread / 2, 2),
                            "mark": round(g["price"], 2),
                            "delta": round(g["delta"], 3),
                            "gamma": round(g["gamma"], 4),
                            "theta": round(g["theta"], 3),
                            "vega": round(g["vega"], 3),
                            "volatility": round(vol * 100, 2),
                            "openInterest": int(5000 * math.exp(-abs(math.log(k / s)) * 12)),
                            "totalVolume": int(800 * math.exp(-abs(math.log(k / s)) * 15)),
                            "daysToExpiration": days,
                            "strikePrice": k,
                        }
                    ]
        return {"underlyingPrice": round(s, 2), "callExpDateMap": calls, "putExpDateMap": puts}

    async def market_open(self, day: date) -> bool | None:
        return day.weekday() < 5

    async def orders(self, account_hash: str, start: datetime, end: datetime) -> list:
        return _demo_orders(self)

    async def transactions(self, account_hash: str, start: datetime, end: datetime) -> list:
        return [
            x
            for x in _demo_history(self)
            if start.timestamp() <= datetime.fromisoformat(x["time"]).timestamp() <= end.timestamp()
        ]


def _demo_orders(src: "DemoDataSource") -> list[dict]:
    """Orders as Schwab returns them, standing in for ones placed in thinkorswim."""

    def leg(under, days, pc, strike, instruction, qty):
        return {
            "orderLegType": "OPTION",
            "instruction": instruction,
            "quantity": qty,
            "positionEffect": "OPENING" if instruction.endswith("OPEN") else "CLOSING",
            "instrument": {
                "assetType": "OPTION",
                "symbol": option_symbol(under, src._exp(days), pc, strike),
                "underlyingSymbol": under,
                "putCall": "PUT" if pc == "P" else "CALL",
            },
        }

    def order(oid, days_ago, status, **kw):
        when = datetime.combine(src.today - timedelta(days=days_ago), datetime.min.time())
        return {
            "orderId": oid,
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "status": status,
            "enteredTime": when.replace(hour=15).isoformat() + "+0000",
            "accountNumber": DemoDataSource.HASH,
            **kw,
        }

    return [
        order(
            9001,
            0,
            "CANCELED",
            orderType="NET_CREDIT",
            complexOrderStrategyType="VERTICAL",
            price=1.05,
            quantity=2,
            orderLegCollection=[
                leg("SPY", 45, "P", 570, "SELL_TO_OPEN", 2),
                leg("SPY", 45, "P", 565, "BUY_TO_OPEN", 2),
            ],
        ),
        order(
            9002,
            0,
            "CANCELED",
            orderType="LIMIT",
            complexOrderStrategyType="NONE",
            price=0.05,
            quantity=2,
            duration="GOOD_TILL_CANCEL",
            orderLegCollection=[leg("IWM", 25, "P", 224, "BUY_TO_CLOSE", 2)],
        ),
        order(
            9003,
            1,
            "CANCELED",
            orderType="NET_CREDIT",
            complexOrderStrategyType="CALENDAR",
            price=0.85,
            quantity=1,
            orderLegCollection=[
                leg("IWM", 25, "P", 224, "BUY_TO_CLOSE", 1),
                leg("IWM", 60, "P", 224, "SELL_TO_OPEN", 1),
            ],
        ),
        order(
            9004,
            2,
            "CANCELED",
            orderType="LIMIT",
            complexOrderStrategyType="NONE",
            orderStrategyType="OCO",
            price=0.50,
            quantity=1,
            orderLegCollection=[leg("SPY", 38, "C", 610, "BUY_TO_CLOSE", 1)],
        ),
    ]


def _demo_txn(aid: int, when: datetime, legs: list, kind: str = "TRADE", desc: str = "") -> dict:
    """One transaction in Schwab's shape. legs: (symbol, signed qty, price, effect)."""
    items, cash, contracts = [], 0.0, 0
    for sym, qty, price, effect in legs:
        occ = parse_option_symbol(sym)
        cost = round(-qty * price * 100, 2)
        cash += cost
        contracts += abs(qty)
        items.append(
            {
                "instrument": {
                    "assetType": "OPTION",
                    "symbol": sym,
                    "underlyingSymbol": occ.underlying,
                    "putCall": "PUT" if occ.put_call == "P" else "CALL",
                },
                "amount": abs(qty),
                "cost": cost,
                "price": price,
                "positionEffect": "OPENING" if effect == "open" else "CLOSING",
            }
        )
    fee = round(0.65 * contracts, 2) if kind == "TRADE" else 0.0
    if fee:
        items.append(
            {
                "instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
                "amount": 0,
                "cost": -fee,
                "feeType": "COMMISSION",
            }
        )
    return {
        "activityId": aid,
        "orderId": aid * 10 if kind == "TRADE" else None,
        "time": when.isoformat(),
        "type": kind,
        "description": desc,
        "netAmount": round(cash - fee, 2),
        "transferItems": items,
    }


def _demo_history(src: "DemoDataSource") -> list[dict]:
    t = src.today

    def at(days: int, h: int = 15) -> datetime:
        return datetime(t.year, t.month, t.day, h, tzinfo=UTC) - timedelta(days=days)

    near = _friday_after(t - timedelta(days=5))
    old = _friday_after(t - timedelta(days=40))
    spy_p, spy_c = (p["instrument"]["symbol"] for p in src.positions[:2])
    vert_s, vert_l = (p["instrument"]["symbol"] for p in src.positions[2:4])
    iwm = src.positions[8]["instrument"]["symbol"]
    return [
        # A closed SPY iron condor, for the journal.
        _demo_txn(
            1,
            at(62),
            [
                (option_symbol("SPY", old, "P", 540), 1, 1.20, "open"),
                (option_symbol("SPY", old, "P", 545), -1, 1.70, "open"),
                (option_symbol("SPY", old, "C", 615), -1, 1.60, "open"),
                (option_symbol("SPY", old, "C", 620), 1, 1.05, "open"),
            ],
        ),
        _demo_txn(
            2,
            at(44),
            [
                (option_symbol("SPY", old, "P", 540), -1, 0.30, "close"),
                (option_symbol("SPY", old, "P", 545), 1, 0.45, "close"),
                (option_symbol("SPY", old, "C", 615), 1, 0.40, "close"),
                (option_symbol("SPY", old, "C", 620), -1, 0.20, "close"),
            ],
        ),
        # The SPY strangle: opened in the near cycle, rolled out when the call was tested.
        _demo_txn(
            3,
            at(33),
            [
                (option_symbol("SPY", near, "P", 545), -1, 3.10, "open"),
                (option_symbol("SPY", near, "C", 600), -1, 2.80, "open"),
            ],
        ),
        _demo_txn(
            4,
            at(12),
            [
                (option_symbol("SPY", near, "P", 545), 1, 1.20, "close"),
                (option_symbol("SPY", near, "C", 600), 1, 3.60, "close"),
                (spy_p, -1, 4.85, "open"),
                (spy_c, -1, 3.95, "open"),
            ],
        ),
        # SPY put vertical and IWM short put, opened normally. The rest are carried in.
        _demo_txn(5, at(9), [(vert_s, -2, 3.40, "open"), (vert_l, 2, 2.35, "open")]),
        _demo_txn(6, at(20), [(iwm, -2, 3.10, "open")]),
    ]
