"""Keeps a live picture of the selected account and pushes it to browsers.

Positions and balances are refreshed every `positions_interval_s`; quotes and greeks for
every held contract, the underlyings, SPY and VIX every `quote_interval_s` while the market
is open, and once a minute when it is closed. Everything is one REST call per refresh, well
inside Schwab's limits.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import analytics
from .datasource import VIX, DataSource
from .options import Leg, leg_from_schwab_position
from .schwab_service import SchwabAuthError, SchwabError, SchwabNotConnected
from .strategies import Rules, evaluate, group_all

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
BENCH = "SPY"
MIN_IVR_DAYS = 20


class NeedsSetup(Exception):
    def __init__(self, what: str, message: str):
        super().__init__(message)
        self.what = what


def market_session(now: datetime, open_today: bool | None) -> str:
    """'open', 'closed'. Uses the exchange calendar answer when known."""
    et = now.astimezone(ET)
    if open_today is False or et.weekday() >= 5:
        return "closed"
    minutes = et.hour * 60 + et.minute
    return "open" if 9 * 60 + 30 <= minutes < 16 * 60 + 15 else "closed"


class PortfolioService:
    def __init__(
        self,
        source: DataSource,
        store,
        settings,
        connection: Callable[[], dict],
        account_resolver: Callable,
        mode: str,
        rules: Rules | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(ET),
        preferences=None,
        notifier=None,
    ):
        self.source = source
        self.store = store
        self.settings = settings
        self.connection = connection
        self.resolve_account = account_resolver
        self.mode = mode
        self.rules = rules or Rules()
        self.clock = clock
        self.preferences = preferences
        self.notifier = notifier
        self.stream = None  # StreamService, attached when streaming is available
        self.watchlist: list[str] = []
        self._symbols: set[str] = set()
        self._full_poll_at = 0.0
        self._extras: dict = {}
        self._dirty = False
        self._subscribers: set[asyncio.Queue] = set()
        self._wake = asyncio.Event()

        self.account_raw: dict | None = None
        self.legs: list[Leg] = []
        self.quotes: dict = {}
        self.quotes_at: float | None = None
        self.positions_at: float | None = None
        self.error: str | None = None
        self.needs: dict | None = None
        self.open_today: tuple[date, bool | None] | None = None
        self.vix_history: tuple[date, list[float]] | None = None
        self.snapshot: dict = {}

    # Subscribers -----------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _publish(self) -> None:
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(self.snapshot)

    def wanted_symbols(self) -> set[str]:
        """Everything that needs live quotes; used by the streamer."""
        return set(self._symbols)

    def on_stream_quotes(self, batch: dict[str, dict]) -> None:
        for sym, fields in batch.items():
            entry = self.quotes.setdefault(sym, {})
            entry.setdefault("quote", {}).update(fields)
        self.quotes_at = time.time()
        self._dirty = True

    async def publisher(self) -> None:
        """Pushes streamed updates to browsers at most once a second."""
        while True:
            await asyncio.sleep(1)
            if self._dirty and self.snapshot:
                self._dirty = False
                try:
                    self._build_and_publish(self._extras)
                except Exception:
                    log.exception("Publishing a streamed update failed")

    def streaming(self) -> bool:
        return self.stream is not None and self.stream.status == "live"

    def refresh_soon(self) -> None:
        """Forces a positions refresh on the next loop, e.g. after an account change."""
        self.positions_at = None
        self._wake.set()

    # Loop ----------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Portfolio refresh failed unexpectedly")
                self.error = "Internal error while refreshing; see the app log."
                self._build_and_publish()
            interval = (
                self.settings.quote_interval_s
                if self._session() == "open"
                else max(60.0, self.settings.quote_interval_s)
            )
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass

    def _session(self) -> str:
        open_today = None
        if self.open_today and self.open_today[0] == self.clock().date():
            open_today = self.open_today[1]
        return market_session(self.clock(), open_today)

    async def tick(self) -> None:
        self.needs = None
        if self.preferences is not None:
            self.rules = await self.preferences.rules()
            self.watchlist = await self.preferences.watchlist()
        try:
            account_hash = await self.resolve_account()
            today = self.clock().date()
            if not self.open_today or self.open_today[0] != today:
                try:
                    self.open_today = (today, await self.source.market_open(today))
                except SchwabError:
                    self.open_today = (today, None)
            due = self.positions_at is None or (
                time.time() - self.positions_at >= self.settings.positions_interval_s
            )
            if due:
                self.account_raw = await self.source.account(account_hash)
                acct = (self.account_raw or {}).get("securitiesAccount", {})
                self.legs = [
                    leg
                    for p in acct.get("positions", [])
                    if (leg := leg_from_schwab_position(p)) is not None
                ]
                self.positions_at = time.time()
            symbols = (
                {leg.symbol for leg in self.legs}
                | {leg.underlying for leg in self.legs}
                | {BENCH, VIX}
                | set(self.watchlist)
            )
            self._symbols = symbols
            now = time.time()
            full = not self.streaming() or now - self._full_poll_at >= 60
            if full:
                poll = sorted(symbols)
            else:
                # Streamed symbols are fresh already; poll only the rest (e.g. VIX).
                poll = sorted(s for s in symbols if not self.stream.fresh(s, now))
            if poll:
                fetched = await self.source.quotes(poll)
                if full:
                    self.quotes = {k: v for k, v in self.quotes.items() if k in symbols}
                    self._full_poll_at = now
                for sym, q in fetched.items():
                    entry = self.quotes.setdefault(sym, {})
                    entry.update({k: v for k, v in q.items() if k != "quote"})
                    entry.setdefault("quote", {}).update(q.get("quote", {}))
            self.quotes_at = time.time()
            await self._refresh_vix_history(today)
            self.error = None
        except NeedsSetup as e:
            self.needs = {"what": e.what, "message": str(e)}
        except SchwabNotConnected:
            self.needs = {"what": "connect", "message": "Schwab is not connected."}
        except SchwabAuthError as e:
            self.needs = {"what": "connect", "message": str(e)}
        except SchwabError as e:
            self.error = str(e)
        await self._build_and_publish_async()

    async def _refresh_vix_history(self, today: date) -> None:
        if self.vix_history and self.vix_history[0] == today:
            return
        try:
            closes = await self.source.daily_closes(VIX, 370)
            self.vix_history = (today, [c for _, c in closes])
        except SchwabError as e:
            log.warning("VIX history unavailable: %s", e)
            self.vix_history = (today, [])

    # Snapshot ------------------------------------------------------------------------

    def _quote(self, symbol: str) -> dict:
        return (self.quotes.get(symbol) or {}).get("quote", {})

    def _price(self, symbol: str) -> float | None:
        q = self._quote(symbol)
        for k in ("mark", "lastPrice", "closePrice"):
            if q.get(k):
                return float(q[k])
        return None

    def _apply_quotes(self) -> None:
        for leg in self.legs:
            q = self._quote(leg.symbol)
            if not q:
                continue
            if leg.is_option:
                leg.mark = _num(q.get("mark"), leg.mark)
                leg.delta = _num(q.get("delta"))
                leg.gamma = _num(q.get("gamma"))
                leg.theta = _num(q.get("theta"))
                leg.vega = _num(q.get("vega"))
                leg.iv = _num(q.get("volatility"))
            else:
                leg.mark = self._price(leg.symbol) or leg.mark

    async def _build_and_publish_async(self) -> None:
        # IV rank for non-index underlyings and betas come from the store.
        extras = {}
        unders = sorted({leg.underlying for leg in self.legs} | {BENCH})
        since = self.clock().date() - timedelta(days=366)
        for u in unders:
            b = await self.store.get_beta(u)
            hist = [v for _, v in await self.store.iv_history(u, since)]
            extras[u] = {"beta": 1.0 if u == BENCH else (b[0] if b else None), "iv_hist": hist}
        self._extras = extras
        self._build_and_publish(extras)
        await self._notify_flags()

    async def _notify_flags(self) -> None:
        """One push notification per flag per strategy, the first time it appears."""
        if self.mode != "live" or not self.rules.notify_flags:
            return
        if self.notifier is None or not self.notifier.enabled:
            return
        for u in self.snapshot.get("underlyings", []):
            for st in u["strategies"]:
                n = st["numbers"]
                strikes = "/".join(
                    f"{x['strike']:g}{x['put_call']}" for x in st["legs"] if x["strike"]
                )
                ident = f"{u['symbol']}|{st['name']}|{n['expiration']}|{strikes}"
                for f in st["flags"]:
                    if f["kind"] == "data":
                        continue
                    if not await self.store.mark_notified(f"flag:{ident}:{f['kind']}"):
                        continue
                    title = f"{u['symbol']} {st['name']} {n['expiration'] or ''}".strip()
                    try:
                        await self.notifier.send(
                            title, f"{f['detail']}. {strikes}", self.settings.app_url
                        )
                    except Exception as e:
                        log.warning("Flag notification failed: %s", e)

    def _build_and_publish(self, extras: dict | None = None) -> None:
        extras = extras or {}
        self._apply_quotes()
        today = self.clock().date()
        conn = self.connection()
        snap: dict = {
            "generated_at": time.time(),
            "mode": self.mode,
            "connection": conn,
            "needs": self.needs,
            "error": self.error,
            "session": self._session(),
            "data": {
                "quotes_at": self.quotes_at,
                "positions_at": self.positions_at,
                "source": "stream" if self.streaming() else "poll",
                "stream_detail": None if self.stream is None else self.stream.detail,
            },
        }
        vix = self._price(VIX)
        spy = self._price(BENCH)
        vix_hist = self.vix_history[1] if self.vix_history else []
        spy_ivr = analytics.iv_rank(vix, vix_hist)
        if spy_ivr:
            spy_ivr["source"] = "VIX"
        snap["market"] = {
            "vix": vix,
            "spy": spy,
            "spy_change_pct": _num(self._quote(BENCH).get("netPercentChange")),
        }

        acct = (self.account_raw or {}).get("securitiesAccount")
        if acct:
            bal = acct.get("currentBalances", {})
            nl = _num(bal.get("liquidationValue"))
            maint = _num(bal.get("maintenanceRequirement"))
            snap["account"] = {
                "last4": str(acct.get("accountNumber", ""))[-4:],
                "type": acct.get("type"),
                "net_liq": nl,
                "cash": _num(bal.get("cashBalance")),
                "buying_power": _num(bal.get("buyingPower")),
                "maintenance": maint,
                "bp_used_pct": (maint / nl) if (maint is not None and nl) else None,
                "bp_ceiling_pct": analytics.bp_ceiling(vix),
            }
        else:
            snap["account"] = None

        groups = group_all(self.legs)
        underlyings = []
        tot = {"pl": 0.0, "theta": 0.0, "delta_bw": 0.0}
        missing = {"pl": False, "theta": False, "delta_bw": False}
        for u, strategies in groups.items():
            price = self._price(u)
            q = self._quote(u)
            ex = extras.get(u, {})
            beta = ex.get("beta")
            if u == BENCH:
                ivr = spy_ivr
            else:
                hist = ex.get("iv_hist") or []
                ivr = analytics.iv_rank(hist[-1], hist) if hist else None
                if ivr:
                    ivr["source"] = "recorded"
                    if ivr["days"] < MIN_IVR_DAYS:
                        # A rank over a few days is noise; show only how much history exists.
                        ivr["rank"] = ivr["percentile"] = None
            u_tot = {"pl": 0.0, "delta": 0.0, "theta": 0.0}
            u_missing = {k: False for k in u_tot}
            out_strats = []
            for st in strategies:
                evaluate(st, today, price, self.rules)
                n = st.numbers
                for k, nk in (("pl", "pl"), ("delta", "delta"), ("theta", "theta")):
                    if n[nk] is None:
                        u_missing[k] = True
                    else:
                        u_tot[k] += n[nk]
                out_strats.append(_strategy_json(st, today))
            delta_bw = None
            if not u_missing["delta"] and beta is not None and price and spy:
                delta_bw = u_tot["delta"] * beta * price / spy
            for k, v in (("pl", u_tot["pl"]), ("theta", u_tot["theta"]), ("delta_bw", delta_bw)):
                if v is None or (k != "delta_bw" and u_missing[k]):
                    missing[k] = True
                else:
                    tot[k] += v
            out_strats.sort(key=lambda s: (s["numbers"]["dte"] is None, s["numbers"]["dte"]))
            underlyings.append(
                {
                    "symbol": u,
                    "price": price,
                    "change_pct": _num(q.get("netPercentChange")),
                    "beta": None if beta is None else round(beta, 2),
                    "ivr": ivr,
                    "pl": None if u_missing["pl"] else round(u_tot["pl"], 2),
                    "delta": None if u_missing["delta"] else round(u_tot["delta"], 1),
                    "delta_bw": None if delta_bw is None else round(delta_bw, 1),
                    "theta": None if u_missing["theta"] else round(u_tot["theta"], 2),
                    "strategies": out_strats,
                }
            )
        snap["underlyings"] = underlyings
        nl = (snap["account"] or {}).get("net_liq")
        snap["totals"] = {
            "pl": None if missing["pl"] else round(tot["pl"], 2),
            "theta": None if missing["theta"] else round(tot["theta"], 2),
            "theta_pct_nl": (tot["theta"] / nl) if (nl and not missing["theta"]) else None,
            "delta_bw": None if missing["delta_bw"] else round(tot["delta_bw"], 1),
            "flags": sum(len(s["flags"]) for u in underlyings for s in u["strategies"]),
        }
        snap["ivr"] = {"SPY": spy_ivr}
        self.snapshot = snap
        self._publish()


def _strategy_json(st, today: date) -> dict:
    return {
        "name": st.name,
        "defined_risk": st.defined_risk,
        "numbers": st.numbers,
        "flags": st.flags,
        "legs": [
            {
                "symbol": leg.symbol,
                "kind": leg.kind,
                "put_call": leg.put_call,
                "strike": leg.strike,
                "expiration": leg.expiration.isoformat() if leg.expiration else None,
                "dte": leg.dte(today),
                "quantity": leg.quantity,
                "avg_price": round(leg.avg_price, 4),
                "mark": None if leg.mark is None else round(leg.mark, 4),
                "delta": leg.delta,
                "theta": leg.theta,
                "iv": leg.iv,
            }
            for leg in st.legs
        ],
    }


def _num(v, default=None):
    try:
        if v is None:
            return default
        f = float(v)
        return default if f != f else f  # NaN
    except (TypeError, ValueError):
        return default
