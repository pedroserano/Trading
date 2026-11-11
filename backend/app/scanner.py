"""Scanner: finds new trades the tastytrade way and hands them to automanage's levels.

Once a day (and on Scan now) it looks at each candidate underlying and opens premium only
when IV rank is high enough: a 16-delta strangle when IV rank is high, a 16-delta iron
condor collecting at least a third of its width otherwise, in the monthly expiration nearest
45 DTE. It skips thin markets, single stocks without a known earnings date (the earnings
buffer), underlyings already traded, and trades that would take too much buying power.

What it finds goes through the same levels as automanage: Notify, Propose (waits for a tap)
or Auto (simulated account only; live stops at Propose). Every order uses the same ticket,
guards and preview as a manual one.
"""

import asyncio
import copy
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import chain as chain_mod
from .automation import LEVELS, Action, _hm

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
SETTINGS_KEY = "scanner_settings"
STATE_KEY = "scanner"
DEFAULTS = {
    "level_sim": "propose",
    "level_live": "off",
    "min_ivr": 30,
    "undefined_ivr": 50,
    "target_dte": 45,
    "min_dte": 30,
    "max_dte": 60,
    "monthly_only": True,
    "short_delta": 0.16,
    "ic_short_delta": 0.16,
    "min_ic_credit": 1 / 3,
    "earnings_days": 7,
    "max_spread": 0.05,
    "min_oi": 1000,
    "max_bp_share": 0.05,
    "max_per_underlying": 1,
    "max_new_per_day": 3,
    "contracts": 1,
    "scan_time": "10:15",
    "automanage_new": True,
}
# (key, label, low, high, kind, unit) for the Settings page.
FIELDS = [
    ("min_ivr", "Open only if IV rank is at least", 0, 100, "int", "IVR"),
    ("undefined_ivr", "Strangle (undefined risk) if IV rank is at least", 0, 100, "int", "IVR"),
    ("target_dte", "Expiration nearest", 7, 120, "int", "DTE"),
    ("min_dte", "No sooner than", 1, 120, "int", "DTE"),
    ("max_dte", "No later than", 7, 180, "int", "DTE"),
    ("short_delta", "Strangle short strikes at", 0.05, 0.5, "delta", "Δ"),
    ("ic_short_delta", "Iron condor short strikes at", 0.05, 0.5, "delta", "Δ"),
    ("min_ic_credit", "Iron condor credit, at least", 10, 60, "percent", "% of width"),
    (
        "earnings_days",
        "Single stocks: open only if earnings are more than",
        0,
        60,
        "int",
        "sessions away",
    ),
    ("max_spread", "At-the-money bid/ask spread, at most", 1, 50, "percent", "% of mark"),
    ("min_oi", "At-the-money open interest, at least", 0, 1_000_000, "int", "contracts"),
    ("max_bp_share", "Buying power for one trade, at most", 0.5, 50, "percent", "% net liq"),
    ("max_per_underlying", "Open trades per underlying, at most", 1, 10, "int", "trades"),
    ("max_new_per_day", "New trades per day, at most", 0, 50, "int", "trades"),
    ("contracts", "Contracts per trade", 1, 50, "int", "contracts"),
]
FUNDS = {"ETF", "ETN", "COLLECTIVE_INVESTMENT", "INDEX", "MUTUAL_FUND"}


def merged(saved: dict | None) -> dict:
    s = copy.deepcopy(DEFAULTS)
    s.update({k: v for k, v in (saved or {}).items() if k in DEFAULTS})
    return s


def parse_form(form: dict, current: dict | None) -> dict:
    """Validates the Settings form. Raises ValueError with a message for the page."""
    out = merged(current)
    for acct in ("sim", "live"):
        v = form.get(f"level_{acct}")
        if v is not None:
            if v not in LEVELS:
                raise ValueError("Scanner: unknown level")
            out[f"level_{acct}"] = v
    for key, label, lo, hi, kind, _unit in FIELDS:
        if key not in form:
            continue
        try:
            v = float(form[key])
        except ValueError:
            raise ValueError(f"{label}: not a number") from None
        if not lo <= v <= hi:
            raise ValueError(f"{label}: must be between {lo:g} and {hi:g}")
        out[key] = v / 100 if kind == "percent" else int(v) if kind == "int" else v
    if form.get("_form") == "scanner":
        out["monthly_only"] = form.get("monthly_only") in ("on", "true", "1")
        out["automanage_new"] = form.get("automanage_new") in ("on", "true", "1")
    if "scan_time" in form:
        try:
            h, m = _hm(form["scan_time"])
            assert (9, 30) <= (h, m) < (16, 0)
        except (ValueError, AssertionError):
            raise ValueError("Scan time: between 09:30 and 16:00") from None
        out["scan_time"] = f"{h:02d}:{m:02d}"
    if out["min_dte"] > out["target_dte"] or out["target_dte"] > out["max_dte"]:
        raise ValueError("Expiration: the DTE range must hold the target")
    return out


def pick_expiration(exps: list[dict], s: dict) -> dict | None:
    ok = [e for e in exps if s["min_dte"] <= e["dte"] <= s["max_dte"]]
    if s["monthly_only"]:
        ok = [e for e in ok if e["monthly"]]
    return min(ok, key=lambda e: abs(e["dte"] - s["target_dte"])) if ok else None


def nearest_delta(rows: list[dict], side: str, target: float) -> dict | None:
    cands = [r for r in rows if r[side] and r[side]["delta"] is not None and r[side]["symbol"]]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(abs(r[side]["delta"]) - target))


def _mid(c: dict | None) -> float | None:
    if not c:
        return None
    if c.get("mark") is not None:
        return c["mark"]
    if c.get("bid") is not None and c.get("ask") is not None:
        return (c["bid"] + c["ask"]) / 2
    return None


def build_trade(rows: list[dict], strangle: bool, s: dict) -> tuple[list[dict] | None, str]:
    """Legs (symbol, quantity per unit, effect) for one unit, or None and the reason."""
    if strangle:
        put = nearest_delta(rows, "put", s["short_delta"])
        call = nearest_delta(rows, "call", s["short_delta"])
        if not put or not call or put["strike"] >= call["strike"]:
            return None, "no strikes near the target delta"
        return [
            {"symbol": put["put"]["symbol"], "quantity": -1, "effect": "open"},
            {"symbol": call["call"]["symbol"], "quantity": -1, "effect": "open"},
        ], f"Strangle {put['strike']:g}/{call['strike']:g}"
    sp = nearest_delta(rows, "put", s["ic_short_delta"])
    sc = nearest_delta(rows, "call", s["ic_short_delta"])
    if not sp or not sc or sp["strike"] >= sc["strike"]:
        return None, "no strikes near the target delta"
    by_strike = {r["strike"]: r for r in rows}
    puts = sorted(k for k, r in by_strike.items() if k < sp["strike"] and r["put"])
    calls = sorted(k for k, r in by_strike.items() if k > sc["strike"] and r["call"])
    best_ratio = 0.0
    # Narrowest wings first: the first width that collects enough is the iron condor.
    widths = sorted(
        {round(sp["strike"] - k, 4) for k in puts} & {round(k - sc["strike"], 4) for k in calls}
    )
    for w in widths:
        lp, lc = by_strike[sp["strike"] - w], by_strike[sc["strike"] + w]
        prices = [_mid(sp["put"]), _mid(lp["put"]), _mid(sc["call"]), _mid(lc["call"])]
        if any(p is None for p in prices):
            continue
        credit = prices[0] - prices[1] + prices[2] - prices[3]
        ratio = credit / w if w else 0
        best_ratio = max(best_ratio, ratio)
        if ratio >= s["min_ic_credit"]:
            return [
                {"symbol": lp["put"]["symbol"], "quantity": 1, "effect": "open"},
                {"symbol": sp["put"]["symbol"], "quantity": -1, "effect": "open"},
                {"symbol": sc["call"]["symbol"], "quantity": -1, "effect": "open"},
                {"symbol": lc["call"]["symbol"], "quantity": 1, "effect": "open"},
            ], f"Iron condor {lp['strike']:g}/{sp['strike']:g}/{sc['strike']:g}/{lc['strike']:g}"
    return None, (
        f"no wing width collects {s['min_ic_credit']:.0%} of the width "
        f"(best {best_ratio:.0%}); lower the minimum or move the short strikes closer"
    )


class Scanner:
    TICK_S = 60

    def __init__(
        self,
        store,
        source,
        portfolios,
        automations,
        watchlist,
        preferences,
        live_broker,
        clock=lambda: datetime.now(ET),
        earnings=None,
    ):
        self.store = store
        self.source = source
        self.portfolios = portfolios
        self.automations = automations
        self.watchlist = watchlist
        self.preferences = preferences
        self.live_broker = live_broker
        self.clock = clock
        self.earnings = earnings
        self.lock = asyncio.Lock()

    async def settings(self) -> dict:
        return merged(await self.store.get_setting(SETTINGS_KEY))

    async def _state(self) -> dict:
        return await self.store.get_setting(STATE_KEY) or {}

    def level(self, s: dict, account: str) -> str:
        lv = s[f"level_{account}"]
        cap = self.automations[account].max_level
        return LEVELS[min(LEVELS.index(lv), LEVELS.index(cap))]

    async def view(self, account: str) -> dict:
        s = await self.settings()
        st = await self._state()
        return {"settings": s, "level": self.level(s, account), "last": st.get(account)}

    async def symbols(self, account: str) -> list[str]:
        if account == "live":
            return list((await self.live_broker.config())["symbols"])
        out = ["SPY"] + [s for s in await self.preferences.watchlist() if s != "SPY"]
        return out

    async def _ivr(self, account: str, symbol: str) -> float | None:
        if symbol == "SPY":
            snap = self.portfolios["live"].snapshot or {}
            return ((snap.get("ivr") or {}).get("SPY") or {}).get("rank")
        row = next((r for r in await self.watchlist.rows() if r["symbol"] == symbol), None)
        return ((row or {}).get("ivr") or {}).get("rank")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scanner tick failed")
            await asyncio.sleep(self.TICK_S)

    async def tick(self) -> list[str]:
        """Runs the daily scan for each account once its scan time has passed."""
        s = await self.settings()
        now = self.clock().astimezone(ET)
        today = now.date().isoformat()
        done = []
        if now.weekday() >= 5 or (now.hour, now.minute) < _hm(s["scan_time"]):
            return done
        st = await self._state()
        for account in ("sim", "live"):
            if self.level(s, account) == "off":
                continue
            if (st.get(account) or {}).get("day") == today:
                continue
            if (self.portfolios[account].snapshot or {}).get("session") != "open":
                continue
            await self.scan(account, act=True)
            done.append(account)
        return done

    async def scan(self, account: str, act: bool = True) -> dict:
        """Looks for trades now. With act, applies the account's scanner level to them."""
        async with self.lock:
            s = await self.settings()
            now = self.clock().astimezone(ET)
            today = now.date().isoformat()
            st = await self._state()
            prev = st.get(account) or {}
            placed_today = prev.get("new_today", 0) if prev.get("day") == today else 0
            level = self.level(s, account)
            auto = self.automations[account]
            snap = self.portfolios[account].snapshot or {}
            acct = snap.get("account") or {}
            net_liq = acct.get("net_liq")
            ceiling = acct.get("bp_ceiling_pct")
            held = {
                u["symbol"]: sum(1 for x in u["strategies"] if x.get("trade_id") is not None)
                for u in snap.get("underlyings", [])
            }
            rules = await self.preferences.rules()
            candidates, skipped = [], []
            symbols = await self.symbols(account)
            try:
                quotes = await self.source.quotes(symbols)
            except Exception as e:
                quotes = {}
                log.warning("Scanner quotes failed: %s", e)
            for sym in symbols:
                why = await self._check(account, sym, s, held, quotes)
                if isinstance(why, str):
                    skipped.append({"symbol": sym, "reason": why})
                    continue
                ivr, exp, rows = why
                strangle = ivr >= s["undefined_ivr"]
                legs, label = build_trade(rows, strangle, s)
                if legs is None:
                    skipped.append({"symbol": sym, "reason": label})
                    continue
                n = s["contracts"]
                if account == "live":
                    n = min(n, (await self.live_broker.config())["max_contracts"])
                body = {
                    "account": account,
                    "intent": "open",
                    "duration": "DAY",
                    "profit_target": rules.profit_target,
                    "legs": [{**x, "quantity": x["quantity"] * n} for x in legs],
                }
                try:
                    pv = await auto.submit({**body, "limit_net": None}, False)
                except ValueError as e:
                    skipped.append({"symbol": sym, "reason": f"preview failed: {e}"})
                    continue
                problems = list(pv["errors"]) + list(pv.get("live_blocks") or [])
                bp = pv.get("bp_effect")
                if bp is not None and net_liq and bp > s["max_bp_share"] * net_liq:
                    problems.append(
                        f"buying power {bp:,.0f} is over {s['max_bp_share']:.0%} of net liq"
                    )
                after = pv.get("bp_used_after")
                if after is not None and ceiling is not None and after > ceiling:
                    problems.append(
                        f"buying power in use would be {after:.0%}, over the {ceiling:.0%} "
                        "ceiling for this VIX"
                    )
                if problems:
                    skipped.append({"symbol": sym, "reason": "; ".join(problems)})
                    continue
                candidates.append(
                    {
                        "symbol": sym,
                        "ivr": round(ivr, 1),
                        "expiration": exp["date"],
                        "dte": exp["dte"],
                        "label": label,
                        "strategy": "Strangle" if strangle else "Iron condor",
                        "mid": pv["prices"]["mid"],
                        "bp": bp,
                        "pop": (pv.get("entry") or {}).get("pop"),
                        "body": body,
                    }
                )
            acted = []
            if act and level != "off":
                for c in sorted(candidates, key=lambda c: -c["ivr"]):
                    if placed_today >= s["max_new_per_day"]:
                        c["note"] = "daily limit of new trades reached"
                        continue
                    what = await self._act(account, level, c, s, today)
                    if what:
                        acted.append(what)
                        placed_today += 1
            result = {
                "at": time.time(),
                "day": today if act else prev.get("day"),
                "new_today": placed_today,
                "level": level,
                "candidates": candidates,
                "skipped": skipped,
                "acted": acted,
            }
            st[account] = result
            await self.store.set_setting(STATE_KEY, st)
            return result

    async def _check(self, account, sym, s, held, quotes):
        """(IV rank, expiration, chain rows) when the underlying qualifies, else the reason."""
        if held.get(sym, 0) >= s["max_per_underlying"]:
            return f"already {held[sym]} open trade{'s' if held[sym] > 1 else ''} here"
        ivr = await self._ivr(account, sym)
        if ivr is None:
            return "no IV rank yet (it needs IV history)"
        if ivr < s["min_ivr"]:
            return f"IV rank {ivr:.0f}, under {s['min_ivr']}"
        q = quotes.get(sym) or {}
        kind = {q.get("assetMainType"), q.get("assetSubType"), q.get("assetType")}
        fund = bool(kind & FUNDS) or sym.startswith("$")
        if not fund and self.earnings is not None and self.earnings.is_fund(sym):
            fund = True
        if not fund:
            # The earnings buffer: a stock opens only when its next report is known and far
            # enough away.
            info = self.earnings.info(sym) if self.earnings is not None else None
            if info is None:
                return (
                    "single stock with no earnings date found, so the earnings buffer cannot "
                    "be checked (add it under Settings, Earnings dates)"
                )
            if info["sessions_left"] <= s["earnings_days"]:
                return (
                    f"earnings {info['date']} {info['label']}: within the "
                    f"{s['earnings_days']}-session buffer"
                )
        today = self.clock().astimezone(ET).date()
        exps = chain_mod.expirations(await self.source.expirations(sym), today)
        exp = pick_expiration(exps, s)
        if exp is None:
            return (
                f"no {'monthly ' if s['monthly_only'] else ''}expiration in "
                f"{s['min_dte']}-{s['max_dte']} DTE"
            )
        d = date.fromisoformat(exp["date"])
        chain = chain_mod.chain_rows(
            await self.source.option_chain(sym, d, d, strike_count=None) or {}
        )
        rows, spot = chain["rows"], chain["underlying_price"]
        if not rows or not spot:
            return "no option chain"
        atm = min(rows, key=lambda r: abs(r["strike"] - spot))
        spreads, oi = [], 0.0
        for c in (atm["call"], atm["put"]):
            if c and c["bid"] is not None and c["ask"] and c["mark"]:
                spreads.append((c["ask"] - c["bid"]) / c["mark"])
            oi += (c or {}).get("oi") or 0
        if spreads and sum(spreads) / len(spreads) > s["max_spread"]:
            return f"at-the-money spread {sum(spreads) / len(spreads):.1%} is too wide"
        if oi < s["min_oi"]:
            return f"at-the-money open interest {oi:,.0f}, under {s['min_oi']:,}"
        return ivr, exp, rows

    async def _act(self, account, level, c, s, today) -> str | None:
        auto = self.automations[account]
        key = f"scan:{account}:{c['symbol']}:{today}"
        async with auto.lock:
            st = await auto.load()
            if key in st["acted"] or any(p["key"] == key for p in st["proposals"]):
                return None
            title = f"{c['symbol']} {c['strategy']}"
            reason = (
                f"IV rank {c['ivr']:.0f}: {c['label']}, {c['expiration']} ({c['dte']} DTE), "
                f"about {abs(c['mid'] or 0):.2f} credit"
            )
            if level == "notify":
                if await auto._notify(key, f"Scanner: {title}", reason):
                    auto._log(None, "scan", f"Found: {title}: {reason}")
                    await auto._save()
                    return f"notify {c['symbol']}"
                return None
            if level == "propose":
                st["proposals"].append(
                    {
                        "id": f"p{int(time.time() * 1000)}{len(st['proposals'])}",
                        "key": key,
                        "trade_id": f"new:{c['symbol']}",
                        "rule": "scan",
                        "kind": "open",
                        "underlying": c["symbol"],
                        "strategy": c["strategy"],
                        "reason": f"New trade. {reason}",
                        "bodies": [c["body"]],
                        "status": "pending",
                        "created_at": time.time(),
                        "due_at": None,
                        "automanage": s["automanage_new"],
                    }
                )
                auto._log(None, "scan", f"Proposed: {title}: {reason}")
                await auto._notify(
                    f"prop:{key}",
                    f"Scanner: {title}",
                    f"Proposed: {reason}. Open the app to review.",
                )
                await auto._save()
                return f"propose {c['symbol']}"
            # Auto (simulated account only).
            st["acted"][key] = time.time()
            done: list[str] = []
            await auto._place_all(
                f"new:{c['symbol']}",
                Action("scan", "open", reason),
                [c["body"]],
                await auto.settings(),
                done,
                automanage=s["automanage_new"],
            )
            await auto._save()
            return f"place {c['symbol']}" if done else None
