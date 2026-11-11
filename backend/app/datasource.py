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
    async def market_open(self, day: date) -> bool | None: ...


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
