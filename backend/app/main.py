import asyncio
import copy
import functools
import hmac
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.sessions import SessionMiddleware

from . import (
    apikeys,
    combined,
    guards,
    guides,
    indexweights,
    mcp_server,
    plain,
    portfolio_view,
    sheet_import,
    signals,
    targets,
)
from . import cash as cash_mod
from . import chain as chain_mod
from . import chains as chains_mod
from . import dashboard as dash
from . import scanner as scan_mod
from .accounts import AccountRegistry
from .analysis import TradeLeg, analyze
from .analytics import liquidity_stars
from .automation import (
    AUTO_STEPS,
    LEVELS,
    RULES,
    SETTING_FIELDS,
    SETTINGS_KEY,
    AutomationService,
    as_run,
    auto_fields,
    auto_rules_values,
    merged_settings,
    parse_settings,
    rule_hints,
    rules_map,
)
from .automation import (
    DEFAULT_SETTINGS as AUTO_DEFAULTS,
)
from .chain_service import ChainService
from .config import Settings, get_settings
from .datasource import SchwabDataSource
from .earnings import EarningsService
from .extremes import PriceHistory
from .extremes import compute as compute_extreme
from .history_service import PriceHistoryService, export_rows, export_xlsx
from .ivref import IvReference
from .jobs import DailyJobs
from .live import LiveBroker
from .market import MARKETS, MarketService
from .notify import PushoverNotifier, ReminderService
from .options import nonstandard, parse_option_symbol
from .order_check import OrderCheckService
from .orders import (
    Ticket,
    TicketError,
    TicketLeg,
    apply_quotes,
    prices,
    schwab_order,
    validate_shape,
)
from .orders import strategy_name as ticket_strategy
from .pacing import run_in_background
from .portfolio import BENCH, PortfolioService
from .preferences import (
    _SYMBOL,
    BAND_FIELDS,
    MARGIN_DEFAULTS,
    MARGIN_SCALE,
    MARGIN_TYPES,
    RULE_FIELDS,
    RULE_NOTES,
    Preferences,
    parse_symbols,
)
from .schwab_service import (
    ConnectionStatus,
    SchwabError,
    SchwabNotConnected,
    SchwabService,
    callback_list,
    pick_callback,
)
from .sim import SimBroker, SimDataSource, stats
from .store import PgStore
from .strategies import Rules, free_cover, not_tasty, past_cover
from .streaming import StreamService
from .token_store import TokenStore
from .watchlist import WatchlistService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC_DIR = Path(__file__).parent / "static"

SESSION_MAX_AGE_S = 12 * 3600
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_S = 60
# Settings key: whether the header's account picker leaves out the simulated account.
HIDE_SIM_KEY = "accounts.hide_sim"
# Settings key: "account:order id" -> the name of the API key that placed the order.
KEY_ORDERS_KEY = "api.key_orders"
MAX_KEY_ORDERS = 2000


class LegIn(BaseModel):
    put_call: str | None = Field(None, pattern="^[PC]$")
    strike: float | None = None
    expiration: date | None = None
    quantity: float
    price: float = Field(ge=0)
    iv: float | None = None
    delta: float | None = None
    theta: float | None = None
    vega: float | None = None


class OrderLegIn(BaseModel):
    symbol: str = Field(max_length=32)
    quantity: int
    effect: Literal["open", "close"]


class OrderIn(BaseModel):
    account: str = "sim"  # "sim" or a Schwab account key ("live", "live-5678")
    intent: Literal["open", "close", "roll", "adjust"] = "open"
    trade_id: int | str | None = None
    # Schwab's four-leg limit is checked with a clear message in validate_shape.
    legs: list[OrderLegIn] = Field(min_length=1, max_length=8)
    limit_net: float | None = None
    duration: Literal["DAY", "GTC"] = "DAY"
    profit_target: float | None = Field(None, ge=0.05, le=0.95)
    # Opening orders: switch automanage on for the new trade once it fills.
    automanage: bool = False
    # The new trade's view for automanage: neutral, bullish, bearish (None: the portfolio's).
    view: str | None = None
    # An earnings play from the scanner ("earnings:<date>"): automanage closes it after the
    # report.
    play: str | None = Field(None, max_length=32)
    # Placed from a proposal's Review: that proposal is then done.
    proposal_id: str | None = Field(None, max_length=64)
    # A working order this one replaces (changed on the ticket from History).
    replaces: int | str | None = None


class LinkIn(BaseModel):
    group: str = Field(max_length=64)
    chain: str = Field(max_length=80)


class SplitIn(BaseModel):
    group: str = Field(max_length=64)


class AutoTradeIn(BaseModel):
    trade_id: int | str
    enabled: bool | None = None
    # The trade's own view: neutral, bullish or bearish; "portfolio" follows the bias.
    view: str | None = None


class WatchlistAddIn(BaseModel):
    text: str
    mode: Literal["add", "replace"] = "add"


class WatchlistRemoveIn(BaseModel):
    symbol: str


class EarningsSetIn(BaseModel):
    symbol: str
    date: str | None = Field(None, max_length=10)  # YYYY-MM-DD; none removes your date
    time: str | None = Field(None, max_length=3)  # bmo, amc, or none


class PauseIn(BaseModel):
    paused: bool


class ModeIn(BaseModel):
    mode: Literal["off", "propose", "auto"]


class ResetIn(BaseModel):
    start_cash: float = Field(gt=0, le=100_000_000)
    confirm: str


def summarize_preview(raw) -> dict:
    """Pulls the useful parts out of Schwab's preview response, whatever its nesting."""
    fields, messages = {}, []
    wanted = {
        "projectedCommission",
        "projectedBuyingPower",
        "projectedAvailableFund",
        "orderValue",
        "projectedMarginRequirement",
    }
    levels = {
        "rejects": "error",
        "warns": "warning",
        "alerts": "warning",
        "reviews": "info",
        "accepts": "ok",
    }

    def walk(node, parent=None):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in wanted and isinstance(v, int | float):
                    fields.setdefault(k, v)
                if k in levels and isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            text = (
                                item.get("message")
                                or item.get("activityMessage")
                                or item.get("title")
                                or str(item)
                            )
                            messages.append({"level": levels[k], "text": str(text)[:300]})
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, parent)

    walk(raw)
    return {
        "ok": not any(m["level"] == "error" for m in messages),
        "fields": fields,
        "messages": [m for m in messages if m["level"] != "ok"],
        "raw": raw,
    }


def nonstandard_checks(t, held: dict, errors: list[str], warnings: list[str]) -> None:
    """Non-standard options (adjusted after a corporate action) are closing only: opening
    one, or rolling it, is blocked; a plain close is warned about. `held`: symbol -> why,
    for the positions the snapshot marks."""
    for x in t.legs:
        if x.equity:
            continue
        why = held.get(x.symbol) or nonstandard(x.symbol, t.underlying)
        if not why:
            continue
        name = f"{x.symbol.split()[0]} {x.strike:g}{x.put_call}"
        if x.effect == "open" or t.intent in ("open", "roll", "adjust"):
            errors.append(f"{name} is a non-standard option ({why}): closing only")
        else:
            warnings.append(
                f"{name} is a non-standard option ({why}): check what it delivers in "
                "thinkorswim before closing"
            )


OVERSOLD = ("oversold", "overbought")


async def _expected_oversold(preview: dict, t, broker, warnings: list[str]) -> dict:
    """A closing order or roll previewed while a working closing order (say the GTC profit
    target) holds the same contracts: Schwab's preview counts both and rejects the new one
    as oversold. Placing it replaces that order, so this rejection is expected and does not
    block; the ticket says which order goes. Any other rejection still blocks."""
    symbols = {x.symbol for x in t.legs}
    holding = [
        o
        for o in await broker.working()
        if o.get("intent") in ("close", "roll") and symbols & {x["symbol"] for x in o["legs"]}
    ]
    errors = [m for m in preview.get("messages", []) if m["level"] == "error"]
    if not holding or not errors or preview.get("error"):
        return preview
    if not all(any(w in m["text"].lower() for w in OVERSOLD) for m in errors):
        return preview
    ids = ", ".join(f"#{o['id']}" for o in holding)
    extra = [x["symbol"] for o in holding for x in o["legs"] if x["symbol"] not in symbols]
    warnings.append(
        f"Placing this replaces your working closing order {ids} on these contracts (Schwab's "
        "preview counted both, hence its oversold message)"
        + (
            "; that order also closed other legs, which are left without a closing order "
            "until a new profit target is placed"
            if extra
            else ""
        )
    )
    return {
        **preview,
        "ok": True,
        "messages": [m if m not in errors else {**m, "level": "info"} for m in preview["messages"]],
    }


class CopiesIn(BaseModel):
    """An opening order and the other accounts to copy it to."""

    order: OrderIn
    # Accounts to preview the copy in; the others are listed with their scaled size only.
    preview: list[str] = Field(default_factory=list, max_length=10)
    # A size chosen on the ticket instead of the net-liq scaled one, by account.
    units: dict[str, int] = Field(default_factory=dict, max_length=10)


class AnalyzeIn(BaseModel):
    symbol: str
    spot: float = Field(gt=0)
    vol: float | None = None
    legs: list[LegIn] = Field(min_length=1, max_length=8)
    # Shares already held that short calls are written against (a covered call).
    held_shares: int = Field(0, ge=0, le=1_000_000)
    # Short shares (or a put ZEBRA's) a covered put is written against.
    held_short_shares: int = Field(0, ge=0, le=1_000_000)
    # The account whose "portfolio after" figures to show ("sim" or a Schwab account key).
    account: str = "live"


def _check_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if not _SYMBOL.match(symbol):
        raise HTTPException(status_code=400, detail="Not a valid symbol")
    return symbol


# Built files carry a content hash in their names (index-3S7LdCXx.js), so a name never
# changes content: the browser keeps them instead of asking again on every launch.
_HASHED = re.compile(r"-[A-Za-z0-9_-]{8}\.(js|css)$")


class HashedStatic(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        if _HASHED.search(str(full_path)):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


BACKGROUND = (
    "daily jobs",
    "watchlist",
    "earnings dates",
    "price extremes",
    "price history",
    "scanner",
)


async def supervise(name: str, fn, health: dict, restart_delay: float = 10.0) -> None:
    """Runs a background service forever, restarting it if it crashes.

    `health[name]` holds the last crash for the Settings page; None while running cleanly.
    """
    health[name] = None
    while True:
        try:
            await fn()
            log.warning("Background service %s stopped; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Background service %s crashed; restarting in %ss", name, restart_delay)
            health[name] = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(e).__name__}: {e}"[:300]
        await asyncio.sleep(restart_delay)


class LoginThrottle:
    """Slows password guessing. Single user, so one global counter is enough."""

    def __init__(self):
        self.failures = 0
        self.locked_until = 0.0

    def locked(self) -> bool:
        return time.monotonic() < self.locked_until

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= MAX_LOGIN_FAILURES:
            self.locked_until = time.monotonic() + LOGIN_LOCKOUT_S
            self.failures = 0

    def reset(self) -> None:
        self.failures = 0


def price_at_limit(legs: list[TradeLeg], limit: float | None, units: int) -> list[TradeLeg]:
    """The legs repriced so their net per unit is the ticket's limit (positive: credit),
    for probabilities and break-evens at the price the order would fill. The difference
    from mid is spread over the option legs, a short leg priced up and a long one down for
    more credit. Mid when there is no limit."""
    options = [x for x in legs if x.put_call]
    if limit is None or not options or units <= 0:
        return legs
    mid = sum(-x.quantity * x.price for x in options)  # per share, all units
    size = sum(abs(x.quantity) for x in options)
    k = (limit * units - mid) / size
    return [
        replace(x, price=max(0.01, x.price + (k if x.quantity < 0 else -k))) if x.put_call else x
        for x in legs
    ]


class McpPath:
    """Serves /mcp itself, not only /mcp/: MCP clients post to the URL as given."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    schwab: SchwabService | None = None,
    store=None,
    source=None,
    notifier=None,
    start_background: bool = True,
    static_dir: Path = STATIC_DIR,
) -> FastAPI:
    settings = settings or get_settings()
    if schwab is None:
        schwab = SchwabService(
            settings.schwab_app_key,
            settings.schwab_app_secret,
            settings.schwab_callback_url,
            TokenStore(settings.data_dir / "schwab_token.enc", settings.secret_key),
        )
    if store is None:
        store = PgStore(
            settings.db_host,
            settings.db_port,
            settings.db_name,
            settings.db_user,
            settings.db_password,
        )
    if source is None:
        source = SchwabDataSource(schwab)
    notifier = notifier or PushoverNotifier(settings.pushover_user_key, settings.pushover_app_token)

    def connection_status() -> ConnectionStatus:
        return schwab.status()

    # Every Schwab account ticked at the sign-in, each by its key ("live" for the first).
    registry = AccountRegistry(store, source, connection_status)

    def connection_json() -> dict:
        s = connection_status()
        return {
            "connected": s.connected,
            "expires_in_days": s.expires_in_days,
            "detail": s.detail,
            "visible_accounts": registry.visible_count,
        }

    preferences = Preferences(store, settings)
    portfolio = PortfolioService(
        source,
        store,
        settings,
        connection_json,
        registry.resolver("live"),
        mode="live",
        preferences=preferences,
        notifier=notifier,
    )
    stream = StreamService(
        schwab, portfolio.wanted_symbols, portfolio.on_stream_quotes, portfolio.refresh_soon
    )
    portfolio.stream = stream
    chain_service = ChainService(
        source,
        store,
        portfolio,
        registry.resolver("live"),
        sheet_label=lambda: registry.sheet_label("live"),
    )
    order_check = OrderCheckService(
        source, store, registry.resolver("live"), enabled=lambda: connection_status().connected
    )
    live_broker = LiveBroker(
        store,
        schwab,
        source,
        registry.resolver("live"),
        server_allowed=settings.live_trading,
        notifier=notifier,
        app_url=settings.app_url,
        server_auto=settings.live_auto,
        name=lambda: registry.name("live"),
    )
    portfolio.trade_info = chain_service.trade_info
    portfolio.chain_holdings = chain_service.holdings
    portfolio.chain_list = lambda: chain_service.result.get("chains", [])
    # Each Schwab account's own services, by key; the first ("live") is built here and the
    # others when the registry is read (add_account, below).
    chains = {"live": chain_service}
    checks = {"live": order_check}
    brokers = {"live": live_broker}
    if stream is not None:

        def on_activity():
            for k in list(brokers):
                portfolios[k].refresh_soon()
                chains[k].refresh_soon()
                checks[k].refresh_soon()
                brokers[k].refresh_soon()

        stream.on_account_activity = on_activity
    broker = SimBroker(
        store, start_cash=settings.sim_start_cash, commission=settings.sim_commission
    )
    sim_source = SimDataSource(source, broker)

    async def sim_account() -> str:
        return SimDataSource.HASH

    sim_portfolio = PortfolioService(
        sim_source,
        store,
        settings,
        connection_json,
        sim_account,
        mode="sim",
        preferences=preferences,
        include_watchlist=False,
        extra_symbols=broker.working_symbols,
        after_quotes=broker.on_quotes,
        trade_info=lambda: broker.trade_info() if broker.state else {},
    )
    portfolios = {"live": portfolio, "sim": sim_portfolio}
    if stream is not None:
        sim_portfolio.stream = stream

        def wanted():
            out = set()
            for pf in list(portfolios.values()):
                out |= pf.wanted_symbols()
            return out

        def every(batch):
            for pf in list(portfolios.values()):
                pf.on_stream_quotes(batch)

        stream.wanted = wanted
        stream.on_quotes = every
    limits = guards.Limits(max_contracts=settings.max_contracts_per_leg)

    def margin_limits(rules) -> guards.Limits:
        """The ticket's size guides and hard stop, halved under portfolio margin."""
        k = MARGIN_SCALE.get(rules.margin_type, 1.0)
        return guards.Limits(
            max_contracts=limits.max_contracts,
            size_warn_defined=limits.size_warn_defined * k,
            size_warn_undefined=limits.size_warn_undefined * k,
            size_block=limits.size_block * k,
        )

    watchlist = WatchlistService(source, store, portfolio, preferences)

    async def earnings_symbols() -> set[str]:
        out = set(await preferences.watchlist())
        for pf in list(portfolios.values()):
            out |= {u["symbol"] for u in (pf.snapshot or {}).get("underlyings", [])}
        for b in list(brokers.values()):
            out |= set((await b.config())["symbols"])
        return out

    earnings = EarningsService(
        store,
        source,
        earnings_symbols,
        clock=lambda: portfolio.clock(),
        nasdaq=True,
    )
    portfolio.earnings = sim_portfolio.earnings = watchlist.earnings = earnings.info
    iv_reference = IvReference(store, earnings_symbols, clock=lambda: portfolio.clock())
    portfolio.ivref = sim_portfolio.ivref = watchlist.ivref = iv_reference.rank
    history = PriceHistory(store, source, earnings_symbols, clock=lambda: portfolio.clock())
    market = MarketService(source, clock=lambda: portfolio.clock())

    async def watch_extreme(sym, price, change, iv30):
        closes = history.get(sym)
        if closes is None:
            return None  # read in the background; shown from the next refresh
        s = scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY))
        return compute_extreme(closes, price, change, iv30, s)

    watchlist.extremes = watch_extreme

    async def watch_shock(sym, change):
        """The scanner's possible-shock check, for the Watchlist's badge."""
        closes = history.get(sym)
        if closes is None:
            return None
        s = scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY))
        today = portfolio.clock().date()
        iv_hist = await store.iv_history(sym, today - timedelta(days=40))
        return signals.shock(closes, iv_hist, earnings.last_report(sym), s, change, today)

    watchlist.shock = watch_shock
    health: dict[str, str | None] = {}
    jobs = DailyJobs(source, store, portfolio, settings)
    reminders = ReminderService(notifier, store, connection_status, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.open()
        await broker.load()
        await earnings.load()
        await iv_reference.load()
        schwab.load()
        await registry.load()
        registry.sheet_labels = sorted(await store.sheet_accounts())
        if connection_status().connected:
            try:
                await registry.refresh()
            except Exception as e:  # the services retry; the app still starts
                log.warning("Could not read the Schwab accounts: %s", e)
        for key in registry.keys():
            if key not in brokers:
                add_account(key)
        await apply_live_cap()
        log.info("Started. Schwab connected: %s", schwab.status().connected)
        tasks = []
        if start_background:
            services = {
                "portfolio": portfolio.run,
                "daily jobs": jobs.run,
                "reminders": reminders.run,
                "publisher": portfolio.publisher,
                "watchlist": watchlist.run,
                "simulated account": sim_portfolio.run,
                "trade chains": chain_service.run,
                "earnings dates": earnings.run,
                "IV ranks from DoltHub": iv_reference.run,
                "price extremes": history.run,
                "market strip": market.run,
                "order check": order_check.run,
                "live orders": live_broker.run,
                "price history": prices_service.run,
                "simulated publisher": sim_portfolio.publisher,
                "automation": automations["sim"].run,
                "live automation": automations["live"].run,
                "scanner": scanner.run,
            }
            if stream is not None:
                services["streaming"] = stream.run
            # Bulk market-data work, paced so it never runs into Schwab's limit.
            for name in BACKGROUND:
                if name in services:
                    services[name] = functools.partial(run_in_background, services[name])
            tasks = [
                asyncio.create_task(supervise(name, fn, health), name=name)
                for name, fn in services.items()
            ]
            running["on"] = True
            for key in list(brokers):
                if key != "live":
                    start_account(key)
        mcp_run = mcp_srv.session_manager.run()
        await mcp_run.__aenter__()
        try:
            yield
        finally:
            await mcp_run.__aexit__(None, None, None)
        running["on"] = False
        every_task = tasks + [t for ts in account_tasks.values() for t in ts]
        for t in every_task:
            t.cancel()
        for t in every_task:
            with suppress(asyncio.CancelledError):
                await t
        await schwab.close()
        await store.close()

    app = FastAPI(
        title="Trading",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # served at /api/openapi.json, to the signed in and to keys
    )
    app.state.portfolio = portfolio
    app.state.chain_service = chain_service
    app.state.registry = registry
    app.state.accounts = {"portfolios": portfolios, "chains": chains, "brokers": brokers}
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=SESSION_MAX_AGE_S,
        # Lax, not strict, so the session survives the redirect back from Schwab's sign-in.
        # Every state-changing route is a POST, which lax never sends cross-site.
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    throttle = LoginThrottle()
    key_throttle = LoginThrottle()
    api_keys = apikeys.ApiKeys(store)
    app.state.api_keys = api_keys

    async def key_for(request: Request) -> dict | None:
        """The API key presented in the Authorization header, or None. 401 for a bad one."""
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        if key_throttle.locked():
            raise HTTPException(status_code=429, detail="Too many bad keys; wait a minute")
        rec = await api_keys.check(auth[7:].strip())
        if rec is None:
            key_throttle.fail()
            raise HTTPException(status_code=401, detail="Unknown API key")
        return rec

    app.state.check_key = key_for
    mcp_srv, mcp_asgi = mcp_server.build(app)
    app.mount("/mcp", mcp_asgi)

    async def require_login(request: Request) -> None:
        """A signed-in session, or for /api/* an API key whose level covers the call."""
        if request.session.get("auth"):
            return
        path = request.url.path
        if path.startswith("/api/"):
            rec = await key_for(request)
            if rec is None:
                raise HTTPException(status_code=401, detail="Not signed in")
            kind = apikeys.route_kind(request.method, path)
            account = request.query_params.get("account")
            if account is None and kind == "trade":
                try:
                    body = await request.json()
                except ValueError:
                    body = None
                account = body.get("account") if isinstance(body, dict) else None
            account = account or "sim"
            if not apikeys.allows(rec["level"], kind, account):
                why = (
                    "API keys can't change settings; use the app"
                    if kind == "refused"
                    else f"Key {rec['name']!r} ({rec['level']}) can't trade on {account!r}"
                )
                raise HTTPException(status_code=403, detail=why)
            request.state.api_key = rec
            return
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    @app.get("/healthz")
    async def healthz():
        # Unauthenticated, for the container health check. Reveals nothing.
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    async def login(request: Request, password: str = Form()):
        if throttle.locked():
            error = "Too many attempts. Wait a minute and try again."
        elif hmac.compare_digest(password.encode(), settings.app_password.encode()):
            throttle.reset()
            request.session["auth"] = True
            return RedirectResponse("/", status_code=303)
        else:
            throttle.fail()
            error = "Wrong password."
        return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=401)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", dependencies=[Depends(require_login)])
    async def index():
        page = static_dir / "index.html"
        if page.exists():
            return FileResponse(page, headers={"Cache-Control": "no-cache"})
        return RedirectResponse("/connect", status_code=303)

    if (static_dir / "assets").is_dir():
        app.mount("/assets", HashedStatic(directory=static_dir / "assets"), name="assets")

    # Where phones and browsers look for an icon without reading the page (an iPhone's
    # home-screen icon among them); no sign-in, like the rest of /assets.
    icons = static_dir / "assets" / "icons"

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    async def touch_icon():
        if not (icons / "apple-touch-icon.png").exists():
            raise HTTPException(status_code=404)
        return FileResponse(icons / "apple-touch-icon.png", media_type="image/png")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        if not (icons / "icon-32.png").exists():
            raise HTTPException(status_code=404)
        return FileResponse(icons / "icon-32.png", media_type="image/png")

    # Connection and settings page ------------------------------------------------------

    def settings_url(section: str = "") -> str:
        """Where the Settings page lives: inside the app when the app is built."""
        if (static_dir / "index.html").exists():
            return f"/#/settings/{section}" if section else "/#/settings"
        return f"/connect#{section}" if section else "/connect"

    @app.get("/connect", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def connect_page(request: Request, fragment: bool = False):
        rules_now = await preferences.rules()
        # The app shows Settings inside its own page, with the same menu as every other
        # page; /connect on its own is only for when the app's front end is not built.
        if not fragment and (static_dir / "index.html").exists():
            return RedirectResponse("/#/settings", status_code=303)
        status = connection_status()
        callbacks_all = callback_list(settings.schwab_callback_url) or ["https://127.0.0.1:8182"]
        here_host = (request.headers.get("host") or "").split(":")[0].lower()
        here_cb = pick_callback(callbacks_all, here_host)
        error = request.session.pop("flash", None)
        notice = request.session.pop("notice", None)
        accounts = [{**a, "sheet_label": registry.sheet_label(a["key"])} for a in registry.in_use()]
        new_key = shown_keys.pop(request.session.pop("new_key", ""), None)
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        here_base = f"{proto}://{request.headers.get('host') or request.url.netloc}"
        keys_now = [{**k, "last_used_text": ago(k.get("last_used"))} for k in await api_keys.all()]
        return templates.TemplateResponse(
            request,
            "connect_body.html" if fragment else "connect.html",
            {
                "status": status,
                "accounts": accounts,
                "sheet_labels": registry.sheet_labels,
                "hide_sim": await sim_hidden(),
                "sim_start_cash": (await broker.snapshot())["start_cash"],
                "new_key": new_key,
                "here_base": here_base,
                "api_keys": keys_now,
                "api_level_names": apikeys.LEVEL_NAMES,
                "error": error,
                "notice": notice,
                "db_error": await store.ping(),
                "callback_url": here_cb,
                "one_tap": here_cb.rstrip("/").endswith("/schwab/callback"),
                "here_host": here_host,
                "here_unregistered": here_host
                and not any(
                    (urlsplit(c).hostname or "").lower() == here_host for c in callbacks_all
                ),
                "pushover": notifier.enabled,
                "reminder_error": reminders.last_error,
                "reminder_day": [
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                    "Saturday",
                    "Sunday",
                ][settings.reminder_weekday],
                "reminder_hour": settings.reminder_hour,
                "watchlist": await preferences.watchlist(),
                "rules": rules_now,
                "rule_fields": RULE_FIELDS,
                "rule_notes": RULE_NOTES,
                "margin_types": MARGIN_TYPES,
                "band_fields": BAND_FIELDS,
                "biases": targets.BIASES,
                "auto": merged_settings(await store.get_setting(SETTINGS_KEY)),
                "live": await live_broker.status(),
                "live_accounts": [
                    {"key": k, "name": n, **(await brokers[k].config())}
                    for k, n in schwab_accounts()
                    if k in brokers
                ],
                "scan": scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY)),
                "scan_steps_html": await scan_steps_html(rules_now),
                "scan_presets": scan_mod.PRESETS,
                "scan_preset": scan_mod.preset_of(
                    scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY))
                ),
                "scan_fields": scan_mod.FIELDS,
                "auto_rules": RULES,
                "auto_rule_hints": rule_hints(
                    rules_now, merged_settings(await store.get_setting(SETTINGS_KEY))
                ),
                "auto_steps_html": auto_steps_html(
                    rules_now, merged_settings(await store.get_setting(SETTINGS_KEY))
                ),
                "rmap": rules_map(
                    rules_now,
                    as_run(
                        merged_settings(await store.get_setting(SETTINGS_KEY)),
                        live_broker.auto_cap() == "auto",
                    ),
                    schwab_accounts(),
                ),
                "runs_by_itself": await runs_by_itself(),
                "auto_levels": LEVELS,
                "auto_fields": SETTING_FIELDS,
                "auto_map": {f[0]: f for f in SETTING_FIELDS},
                "auto_defaults": copy.deepcopy(AUTO_DEFAULTS),
                "scan_map": {f[0]: f for f in scan_mod.FIELDS},
                "scan_defaults": scan_defaults(rules_now),
                "rule_map": {f[0]: f for f in RULE_FIELDS + BAND_FIELDS},
                "rule_defaults": rule_defaults(rules_now),
                "stream": None if stream is None else (stream.status, stream.detail),
                "health": health,
            },
        )

    @app.get("/schwab/authorize", dependencies=[Depends(require_login)])
    async def schwab_authorize(request: Request):
        # Schwab returns the browser to the registered callback on this same address, so a
        # sign-in started at the Tailscale name comes back there, and at home to the NAS.
        url = schwab.authorization_url(host=request.headers.get("host"))
        return RedirectResponse(url, status_code=303)

    async def _finish_sign_in(request: Request, received_url: str):
        try:
            await schwab.complete_authorization(received_url)
            try:
                await sync_accounts()
            except Exception as e:
                log.warning("Could not read the Schwab accounts after sign-in: %s", e)
            for pf in list(portfolios.values()):
                pf.refresh_soon()
            request.session["notice"] = "Schwab sign-in renewed."
        except SchwabError as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url("connection"), status_code=303)

    @app.post("/schwab/complete", dependencies=[Depends(require_login)])
    async def schwab_complete(request: Request, received_url: str = Form()):
        return await _finish_sign_in(request, received_url)

    @app.get("/schwab/callback", dependencies=[Depends(require_login)])
    async def schwab_callback(request: Request):
        # One-tap sign-in: Schwab redirects the browser here when the app's registered
        # callback is this address. The OAuth state check ties it to the sign-in started
        # by /schwab/authorize.
        return await _finish_sign_in(request, str(request.url))

    @app.post("/schwab/disconnect", dependencies=[Depends(require_login)])
    async def schwab_disconnect():
        await schwab.disconnect()
        return RedirectResponse(settings_url("connection"), status_code=303)

    @app.post("/accounts/sheet-label", dependencies=[Depends(require_login)])
    async def account_sheet_label(request: Request, account: str = Form(), label: str = Form("")):
        """Which of the history sheet's Account labels is a Schwab account's imported
        history (empty: none)."""
        try:
            if label and label not in registry.sheet_labels:
                raise ValueError(f"The history sheet has no account labelled {label!r}")
            await registry.set_sheet_label(account, label)
            if account in chains:
                await chains[account].rebuild()
                portfolios[account].refresh_soon()
            request.session["notice"] = f"{registry.name(account)}: imported history " + (
                f"from the sheet's {label} rows." if label else "none."
            )
        except ValueError as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url("account"), status_code=303)

    @app.post("/accounts/hide-sim", dependencies=[Depends(require_login)])
    async def account_hide_sim(request: Request, hide_sim: str = Form("")):
        """Whether the header's account picker leaves out the simulated account."""
        hide = hide_sim in ("on", "true", "1")
        await store.set_setting(HIDE_SIM_KEY, hide)
        request.session["notice"] = (
            "The simulated account is hidden." if hide else "The simulated account is shown."
        )
        return RedirectResponse(settings_url("account"), status_code=303)

    async def reset_sim(start_cash: float) -> None:
        await broker.reset(start_cash)
        # Order numbers start again after a reset: drop the simulated account's key marks.
        marks = await store.get_setting(KEY_ORDERS_KEY) or {}
        await store.set_setting(
            KEY_ORDERS_KEY, {k: v for k, v in marks.items() if not k.startswith("sim:")}
        )
        sim_portfolio.refresh_soon()

    @app.post("/accounts/reset-sim", dependencies=[Depends(require_login)])
    async def account_reset_sim(
        request: Request, start_cash: str = Form(""), confirm: str = Form("")
    ):
        """Starts the simulated account over: its orders, positions and journal deleted."""
        try:
            cash = float(start_cash)
            ResetIn(start_cash=cash, confirm=confirm)
        except (ValueError, ValidationError):
            request.session["flash"] = "Starting cash must be more than 0."
            return RedirectResponse(settings_url("account"), status_code=303)
        if confirm.strip() != "RESET":
            request.session["flash"] = "Not reset: type RESET to confirm."
            return RedirectResponse(settings_url("account"), status_code=303)
        await reset_sim(cash)
        request.session["notice"] = f"The simulated account starts again with ${cash:,.0f}."
        return RedirectResponse(settings_url("account"), status_code=303)

    # A new key waits here for the one Settings page view that shows it, not in the cookie.
    shown_keys: dict[str, dict] = {}

    def ago(ts: int | None) -> str:
        if not ts:
            return "Never"
        s = max(0, int(time.time()) - ts)
        if s < 120:
            return "Just now"
        if s < 7200:
            return f"{s // 60} minutes ago"
        if s < 172800:
            return f"{s // 3600} hours ago"
        return f"{s // 86400} days ago"

    @app.get("/guide/{name}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def guide_page(request: Request, name: str):
        found = guides.render(name)
        if found is None:
            raise HTTPException(status_code=404, detail="No such guide")
        title, body = found
        back = settings_url("ai" if name == "ai" else "connection")
        return templates.TemplateResponse(
            request,
            "guide.html",
            {"title": title, "body": Markup(body), "back": back},  # noqa: S704 - our own docs, raw HTML off
        )

    @app.post("/api-keys/create", dependencies=[Depends(require_login)])
    async def api_key_create(request: Request, name: str = Form(), level: str = Form()):
        try:
            key, rec = await api_keys.create(name, level)
        except ValueError as e:
            request.session["flash"] = str(e)
        else:
            token = secrets.token_hex(8)
            shown_keys.clear()
            shown_keys[token] = {"name": rec["name"], "key": key}
            request.session["new_key"] = token
        return RedirectResponse(settings_url("ai"), status_code=303)

    @app.post("/api-keys/revoke", dependencies=[Depends(require_login)])
    async def api_key_revoke(request: Request, key_id: str = Form()):
        if await api_keys.revoke(key_id):
            request.session["notice"] = "Key revoked."
        else:
            request.session["flash"] = "That key was already gone."
        return RedirectResponse(settings_url("ai"), status_code=303)

    @app.post("/history/import", dependencies=[Depends(require_login)])
    async def history_import(request: Request, file: UploadFile = File(...)):
        """The trading-history workbook, every account's rows at once: Joint rows to the
        first account, other labels (Individual) to the account that takes them."""
        data = await file.read(25 * 1024 * 1024 + 1)
        if len(data) > 25 * 1024 * 1024:
            request.session["flash"] = "That file is larger than 25 MB."
            return RedirectResponse(settings_url("account"), status_code=303)
        try:
            rows, counts = sheet_import.read_workbook(data, None)
        except sheet_import.SheetError as e:
            request.session["flash"] = str(e)
            return RedirectResponse(settings_url("account"), status_code=303)
        result = await store.upsert_sheet_rows(rows)
        registry.sheet_labels = sorted(await store.sheet_accounts())
        for key, cs in list(chains.items()):
            await cs.rebuild()
            portfolios[key].refresh_soon()
        where = []
        for label, n in sorted(counts["by_account"].items()):
            to = [a["name"] for a in registry.in_use() if registry.sheet_label(a["key"]) == label]
            where.append(f"{n} {label} rows to {to[0] if to else 'no account yet (choose below)'}")
        request.session["notice"] = (
            f"Imported {'; '.join(where) or 'no rows'}: {result['new']} new, "
            f"{result['updated']} updated, {result['unchanged']} unchanged."
        )
        return RedirectResponse(settings_url("account"), status_code=303)

    @app.post("/notify/test", dependencies=[Depends(require_login)])
    async def notify_test(request: Request):
        try:
            await notifier.send(
                "Test", "Notifications from the trading app work.", settings.app_url
            )
            request.session["notice"] = "Test notification sent."
        except Exception as e:
            request.session["flash"] = f"Test notification failed: {e}"
        return RedirectResponse(settings_url("connection"), status_code=303)

    async def update_watchlist(content: str, mode: str = "add") -> str:
        """Add symbols from typed text or a thinkorswim export (or replace the list with them)."""
        symbols, skipped = parse_symbols(content)
        if not symbols:
            return "No symbols found." + (
                f" Skipped (not stocks, ETFs or indexes): {', '.join(skipped[:12])}."
                if skipped
                else ""
            )
        unknown = []
        try:
            quotes = await source.quotes(symbols)
            unknown = [s for s in symbols if s not in quotes]
            symbols = [s for s in symbols if s in quotes]
        except SchwabError:
            pass  # Not connected: accept as given; bad symbols just show no data.
        current = await preferences.watchlist()
        merged = (
            symbols if mode == "replace" else current + [s for s in symbols if s not in current]
        )
        await preferences.set_watchlist(merged)
        saved = await preferences.watchlist()
        portfolio.watchlist = saved
        portfolio.refresh_soon()
        added = [s for s in symbols if s not in current]
        if mode == "replace":
            msg = f"Watchlist now has {len(saved)} symbols."
        elif added:
            msg = f"Added {', '.join(added[:12])}{'…' if len(added) > 12 else ''}."
        elif symbols:
            msg = "Already on the watchlist."
        else:
            msg = ""
        if skipped:
            msg += f" Skipped (not stocks, ETFs or indexes): {', '.join(skipped[:12])}."
        if unknown:
            msg += f" Not recognised by Schwab: {', '.join(unknown[:12])}."
        return msg.strip()

    @app.post("/api/watchlist/add", dependencies=[Depends(require_login)])
    async def api_watchlist_add(body: WatchlistAddIn):
        message = await update_watchlist(body.text, body.mode)
        return {"watchlist": await preferences.watchlist(), "message": message}

    @app.post("/api/watchlist/upload", dependencies=[Depends(require_login)])
    async def api_watchlist_upload(file: UploadFile = File(), mode: str = Form("add")):
        data = await file.read(512 * 1024)
        message = await update_watchlist(data.decode("utf-8-sig", errors="replace"), mode)
        return {"watchlist": await preferences.watchlist(), "message": message}

    @app.post("/api/watchlist/remove", dependencies=[Depends(require_login)])
    async def api_watchlist_remove(body: WatchlistRemoveIn):
        current = await preferences.watchlist()
        await preferences.set_watchlist([s for s in current if s != body.symbol])
        portfolio.watchlist = await preferences.watchlist()
        portfolio.refresh_soon()
        return {"watchlist": portfolio.watchlist, "message": f"Removed {body.symbol}."}

    @app.post("/scanner/settings", dependencies=[Depends(require_login)])
    async def scanner_settings_save(request: Request):
        form = dict(await request.form())
        try:
            new = scan_mod.parse_form(
                form,
                await store.get_setting(scan_mod.SETTINGS_KEY),
                accounts=[k for k, _ in schwab_accounts() if k != "live"],
                live_auto=live_broker.auto_cap() == "auto",
            )
            await store.set_setting(scan_mod.SETTINGS_KEY, new)
            request.session["notice"] = "Scanner settings saved."
        except ValueError as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url("scanner"), status_code=303)

    @app.post("/scanner/preset", dependencies=[Depends(require_login)])
    async def scanner_preset_save(request: Request):
        form = dict(await request.form())
        try:
            key = str(form.get("preset") or "")
            new = scan_mod.apply_preset(await store.get_setting(scan_mod.SETTINGS_KEY), key)
            await store.set_setting(scan_mod.SETTINGS_KEY, new)
            request.session["notice"] = f"Scanner set to {scan_mod.PRESETS[key][0]}."
        except ValueError as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url("scanner"), status_code=303)

    @app.post("/live/settings", dependencies=[Depends(require_login)])
    async def live_settings_save(request: Request):
        form = dict(await request.form())
        try:
            account = str(form.get("account") or "live")
            if account not in brokers:
                raise ValueError("No such Schwab account")
            await brokers[account].set_config(form)
            notice = f"{registry.name(account)}: Schwab limits saved."
            request.session["notice"] = notice
        except ValueError as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url("live"), status_code=303)

    @app.post("/automation/settings", dependencies=[Depends(require_login)])
    async def automation_settings_save(request: Request):
        form = dict(await request.form())
        try:
            new = parse_settings(
                form,
                await store.get_setting(SETTINGS_KEY),
                live_auto=live_broker.auto_cap() == "auto",
                accounts=[k for k, _ in schwab_accounts() if k != "live"],
            )
            await store.set_setting(SETTINGS_KEY, new)
            request.session["notice"] = "Automanage settings saved."
        except ValueError as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url("automation"), status_code=303)

    @app.post("/rules", dependencies=[Depends(require_login)])
    async def rules_save(request: Request):
        form = dict(await request.form())
        try:
            prior = await preferences.rules()
            before = prior.margin_type
            rules = await preferences.set_rules(form)
            portfolio.refresh_soon()
            sim_portfolio.refresh_soon()
            request.session["notice"] = "Rules saved."
            if rules.delta_bias != prior.delta_bias:
                request.session["notice"] = (
                    f"Delta bias set to {rules.delta_bias}: the band is now "
                    f"{rules.delta_min:+.0%} to {rules.delta_max:+.0%} of net liq."
                )
            if rules.margin_type != before:
                # One trade's buying power: the scanner's and live trading's limits follow.
                share = 0.05 * MARGIN_SCALE[rules.margin_type]
                scan = scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY))
                await store.set_setting(scan_mod.SETTINGS_KEY, {**scan, "max_bp_share": share})
                for b in list(brokers.values()):
                    live = await b.config()
                    await b.set_config(
                        {
                            "all_symbols": live["all_symbols"],
                            "symbols": " ".join(live["symbols"]),
                            "max_contracts": live["max_contracts"],
                            "max_bp_share": share * 100,
                        }
                    )
                request.session["notice"] = (
                    f"Margin type set to {MARGIN_TYPES[rules.margin_type]}: buying-power limits "
                    f"reset to its defaults (one underlying {rules.max_underlying_bp:.1%}, an "
                    f"index {rules.max_index_bp:.0%}, one trade {share:.1%}, VIX ceiling "
                    f"{'halved' if rules.margin_type == 'pm' else 'as tastytrade sets it'})."
                )
        except ValueError as e:
            request.session["flash"] = str(e)
        section = "targets" if form.get("_form") == "targets" else "rules"
        return RedirectResponse(settings_url(section), status_code=303)

    # API -------------------------------------------------------------------------------

    @app.get("/api/status", dependencies=[Depends(require_login)])
    async def api_status():
        db_error = await store.ping()
        return {
            "schwab": {k: v for k, v in connection_json().items() if k != "visible_accounts"},
            "database": {"ok": db_error is None, "error": db_error},
        }

    @app.get("/api/accounts", dependencies=[Depends(require_login)])
    async def api_accounts():
        try:
            return await schwab.accounts()
        except SchwabNotConnected:
            raise HTTPException(status_code=409, detail="Schwab is not connected") from None
        except SchwabError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    async def sim_hidden() -> bool:
        """Whether the simulated account is out of the picker: hidden until shown in
        Settings."""
        v = await store.get_setting(HIDE_SIM_KEY)
        return True if v is None else bool(v)

    def all_snapshot() -> dict:
        """The Schwab accounts' snapshots as one ("All" in the picker; never the simulated
        account), each strategy tagged with its account."""
        parts = [(k, n, portfolios[k].snapshot) for k, n in schwab_accounts() if k in portfolios]
        if not parts:
            return {"pending": True}
        return combined.combine(parts, portfolios[parts[0][0]].rules)

    @app.get("/api/snapshot", dependencies=[Depends(require_login)])
    async def api_snapshot(account: str = "live"):
        if account == combined.ALL:
            return JSONResponse(all_snapshot())
        known(account)
        return JSONResponse(portfolios[account].snapshot or {"pending": True})

    def schwab_accounts() -> list[tuple[str, str]]:
        """The Schwab accounts the app runs, as (key, name), the first account first."""
        out = [(a["key"], a["name"]) for a in registry.in_use() if a["key"] in portfolios]
        out.sort(key=lambda x: x[0] != "live")
        return out or [("live", registry.name("live") if registry.entry("live") else "Schwab")]

    def known(account: str) -> None:
        """A route's account: "sim" or a Schwab account key the app runs."""
        if account not in portfolios:
            raise HTTPException(status_code=404, detail=f"No account {account!r}")

    def _schwab_errors(e: Exception) -> HTTPException:
        if isinstance(e, SchwabNotConnected):
            return HTTPException(status_code=409, detail="Schwab is not connected")
        return HTTPException(status_code=502, detail=str(e))

    @app.get("/api/market", dependencies=[Depends(require_login)])
    async def api_market():
        if market.at is None:
            try:
                await market.refresh()
            except Exception as e:
                market.error = f"{type(e).__name__}: {e}"
        return market.view()

    @app.get("/api/watchlist", dependencies=[Depends(require_login)])
    async def api_watchlist():
        rows = await watchlist.rows()
        sc = await scanner.settings()
        for r in rows:
            sym = r["symbol"]
            r["fund"] = earnings.is_fund(sym)
            r["earnings_quiet"] = earnings.quiet_until(
                sym, sc.get("quiet_recent_days", 0), sc.get("quiet_gap_days", 0)
            )
            r["earnings_mine"] = sym in earnings.manual
        return {
            "rows": rows,
            "earnings": earnings.status(),
        }

    @app.post("/api/earnings/set", dependencies=[Depends(require_login)])
    async def api_earnings_set(body: EarningsSetIn):
        symbol = _check_symbol(body.symbol)
        try:
            await earnings.set_one(symbol, body.date, body.time)
        except ValueError:
            raise HTTPException(status_code=400, detail="Not a valid date") from None
        portfolio.refresh_soon()
        sim_portfolio.refresh_soon()
        return {"earnings": earnings.info(symbol)}

    @app.post("/api/earnings/refresh", dependencies=[Depends(require_login)])
    async def api_earnings_refresh():
        try:
            n = await earnings.refresh()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}") from e
        return {"found": n, "earnings": earnings.status()}

    @app.get("/api/trade/{symbol:path}/expirations", dependencies=[Depends(require_login)])
    async def api_expirations(symbol: str):
        symbol = _check_symbol(symbol)
        today = portfolio.clock().date()
        try:
            exps = chain_mod.expirations(await source.expirations(symbol), today)
        except SchwabError as e:
            raise _schwab_errors(e) from e
        if not exps:
            raise HTTPException(status_code=404, detail=f"No options found for {symbol}")
        return {
            "symbol": symbol,
            "expirations": exps,
            "default": chain_mod.default_expiration(exps),
        }

    candle_cache: dict[tuple[str, date], list[dict]] = {}

    @app.get("/api/portfolio", dependencies=[Depends(require_login)])
    async def api_portfolio(account: str = "live"):
        if account == combined.ALL:
            snap = all_snapshot()
        else:
            known(account)
            snap = portfolios[account].snapshot or {}
        rules = await preferences.rules()
        return portfolio_view.build(
            snap, portfolio.clock().date(), earnings.industries, rules.manage_dte
        )

    @app.get("/api/trade/{symbol:path}/chart", dependencies=[Depends(require_login)])
    async def api_chart(symbol: str, account: str = "live"):
        """A year of daily candles, with what the chart draws over them: the account's
        option strikes in this underlying, the next earnings report, the 52-week range."""
        known(account)
        symbol = _check_symbol(symbol)
        today = portfolio.clock().date()
        key = (symbol, today)
        if key not in candle_cache:
            try:
                candles = await source.daily_candles(symbol, 370)
            except SchwabError as e:
                raise _schwab_errors(e) from e
            if len(candle_cache) > 200:
                candle_cache.clear()
            candle_cache[key] = candles
        candles = candle_cache[key]
        strikes = [
            {
                "strike": leg.strike,
                "put_call": leg.put_call,
                "quantity": leg.quantity,
                "expiration": leg.expiration.isoformat() if leg.expiration else None,
            }
            for leg in portfolios[account].legs
            if leg.underlying == symbol and leg.is_option
        ]
        closes = [c["close"] for c in candles]
        return {
            "symbol": symbol,
            "candles": [{**c, "date": c["date"].isoformat()} for c in candles],
            "strikes": strikes,
            "earnings": earnings.info(symbol),
            "high52": max(closes) if closes else None,
            "low52": min(closes) if closes else None,
        }

    @app.get("/api/market/chart", dependencies=[Depends(require_login)])
    async def api_market_chart(label: str):
        """A year of daily candles for one market on the strip. Futures roll from contract
        to contract, so the chart is of the index, or the fund that stands in for it."""
        entry = next((m for m in MARKETS if m[0] == label), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="Unknown market")
        symbol = entry[2] or entry[1]
        today = portfolio.clock().date()
        key = (symbol, today)
        if key not in candle_cache:
            try:
                candles = await source.daily_candles(symbol, 370)
            except SchwabError as e:
                raise _schwab_errors(e) from e
            if len(candle_cache) > 200:
                candle_cache.clear()
            candle_cache[key] = candles
        candles = candle_cache[key]
        closes = [c["close"] for c in candles]
        return {
            "label": label,
            "symbol": symbol,
            "candles": [{**c, "date": c["date"].isoformat()} for c in candles],
            "strikes": [],
            "earnings": None,
            "high52": max(closes) if closes else None,
            "low52": min(closes) if closes else None,
        }

    # The Trade screen reloads its chain every 10 seconds, per open tab: answers are reused
    # for a few seconds rather than asking Schwab for the whole chain each time.
    chain_cache: dict[tuple, tuple[float, dict]] = {}
    CHAIN_TTL_S = 5.0

    @app.get("/api/trade/{symbol:path}/chain", dependencies=[Depends(require_login)])
    async def api_chain(symbol: str, expiration: date, strikes: int = 0):
        """strikes: how many either side of the money; 0 returns every strike."""
        symbol = _check_symbol(symbol)
        count = None if strikes <= 0 else max(4, min(strikes, 200))
        key = (symbol, expiration, count)
        hit = chain_cache.get(key)
        if hit and time.monotonic() - hit[0] < CHAIN_TTL_S:
            return hit[1]
        try:
            raw = await source.option_chain(symbol, expiration, expiration, strike_count=count)
        except SchwabError as e:
            raise _schwab_errors(e) from e
        out = chain_mod.chain_rows(raw or {})
        out.update({"symbol": symbol, "expiration": expiration.isoformat()})
        liq = chain_mod.atm_liquidity(out.get("rows") or [], out.get("underlying_price"))
        liq["stars"] = liquidity_stars(liq["spread_pct"], liq["width"], liq["oi"])
        out["liquidity"] = liq
        held = next(
            (u for u in portfolio.snapshot.get("underlyings", []) if u["symbol"] == symbol), None
        )
        out["ivr"] = (held or {}).get("ivr")
        if symbol == BENCH:
            out["ivr"] = (portfolio.snapshot.get("ivr") or {}).get("SPY")
        else:
            wl = await watchlist.row(symbol)
            out["ivr"] = out["ivr"] or (wl or {}).get("ivr")
        now = time.monotonic()
        if len(chain_cache) > 50:
            for k in [k for k, v in chain_cache.items() if now - v[0] >= CHAIN_TTL_S]:
                del chain_cache[k]
        chain_cache[key] = (now, out)
        return out

    @app.post("/api/analyze", dependencies=[Depends(require_login)])
    async def api_analyze(body: AnalyzeIn):
        symbol = _check_symbol(body.symbol)
        today = portfolio.clock().date()
        legs = [TradeLeg(**leg.model_dump()) for leg in body.legs]
        for leg in legs:
            if leg.put_call and (leg.strike is None or leg.expiration is None):
                raise HTTPException(status_code=400, detail="Option legs need strike and date")
            if leg.expiration and leg.expiration > today + timedelta(days=1200):
                raise HTTPException(status_code=400, detail="Expiration too far out")
        try:
            result = analyze(
                legs,
                body.spot,
                today,
                body.vol,
                held_shares=body.held_shares,
                held_short=body.held_short_shares,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        snap = (portfolios.get(body.account) or portfolio).snapshot or {}
        totals = snap.get("totals") or {}
        acct = snap.get("account") or {}
        beta = 1.0 if symbol == BENCH else None
        if beta is None:
            b = await store.get_beta(symbol)
            beta = b[0] if b else None
        spy = (snap.get("market") or {}).get("spy")
        after = {
            "delta_bw": None,
            "theta": None,
            "bp_used_pct": None,
            "bp_ceiling_pct": acct.get("bp_ceiling_pct"),
        }
        if (
            result["delta"] is not None
            and beta is not None
            and spy
            and totals.get("delta_bw") is not None
        ):
            after["delta_bw"] = round(
                totals["delta_bw"] + result["delta"] * beta * body.spot / spy, 1
            )
        if result["theta"] is not None and totals.get("theta") is not None:
            after["theta"] = round(totals["theta"] + result["theta"], 2)
        if acct.get("net_liq") and acct.get("maintenance") is not None:
            after["bp_used_pct"] = (acct["maintenance"] + result["bp_estimate"]) / acct["net_liq"]
        result["portfolio_after"] = after
        result["beta"] = beta
        return result

    # Orders ----------------------------------------------------------------------------

    async def build_ticket(body: OrderIn) -> tuple[Ticket, dict, dict]:
        try:
            legs = [TicketLeg.parse(x.symbol, x.quantity, x.effect) for x in body.legs]
            t = Ticket(
                legs=legs,
                intent=body.intent,
                duration=body.duration,
                limit_net=body.limit_net,
                trade_id=body.trade_id,
                profit_target=body.profit_target if body.intent == "open" else None,
            )
            validate_shape(t)
        except TicketError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if t.stock and body.account == "sim":
            raise HTTPException(status_code=400, detail="The simulated account holds no stock")
        if (
            body.intent in ("close", "roll", "adjust")
            and body.account == "sim"
            and body.trade_id is None
        ):
            raise HTTPException(status_code=400, detail="Closing a simulated trade needs its id")
        try:
            quotes = await source.quotes(sorted({x.symbol for x in legs} | {t.underlying}))
        except SchwabError as e:
            raise _schwab_errors(e) from e
        apply_quotes(t, quotes)
        p = prices(t)
        if t.limit_net is None:
            t.limit_net = p["mid"]
        else:
            t.limit_net = round(t.limit_net, 2)
        return t, p, quotes

    async def assess(body: OrderIn, t: Ticket, p: dict, quotes: dict) -> dict:
        known(body.account)
        today = portfolio.clock().date()
        pf = portfolios[body.account]
        acct = (pf.snapshot or {}).get("account") or {}
        if body.account == "sim":
            held = await broker.held(body.trade_id)
        else:
            held = {}
            for leg in pf.legs:
                held[leg.symbol] = held.get(leg.symbol, 0) + int(leg.quantity)
        spot = None
        uq = (quotes.get(t.underlying) or {}).get("quote", {})
        for k in ("mark", "lastPrice", "closePrice"):
            if uq.get(k):
                spot = float(uq[k])
                break
        bp, defined, a = None, True, None
        if spot and t.intent in ("open", "adjust") and all(x.mark is not None for x in t.legs):
            tl = price_at_limit(
                [
                    TradeLeg(x.put_call, x.strike, x.expiration, x.quantity, x.mark, iv=x.iv)
                    for x in t.legs
                ],
                t.limit_net,
                t.units,
            )
            # Short options covered by shares or a ZEBRA already held are not naked.
            cover = {"C": 0.0, "P": 0.0}
            held_st = pf.strategies_for(t.underlying) if hasattr(pf, "strategies_for") else []
            for pc in cover:
                mine = [x for x in t.legs if x.put_call == pc and x.quantity < 0]
                if mine and held_st:
                    cover[pc] = free_cover(held_st, max(x.expiration for x in mine), pc)
            a = analyze(tl, spot, today, held_shares=cover["C"], held_short=cover["P"])
            bp, defined = a["bp_estimate"], not a["unlimited_loss"]
        # Conditions at the time, kept with the trade for the dashboard's analysis.
        market = (portfolio.snapshot or {}).get("market") or {}
        ivr = None
        if t.underlying in ("SPY", "SPX", "XSP"):
            ivr = ((portfolio.snapshot or {}).get("ivr") or {}).get("SPY")
        else:
            row = await watchlist.row(t.underlying)
            ivr = (row or {}).get("ivr")
        entry = {
            "spot": spot,
            "vix": market.get("vix"),
            "ivr": (ivr or {}).get("rank"),
            "pop": a["pop"] if a else None,
            "delta": a["delta"] if a else None,
            "iv": a["vol"] if a else None,
            "bp": bp,
            "bp_share": (bp / acct["net_liq"]) if (bp and acct.get("net_liq")) else None,
            "earnings_days": (earnings.info(t.underlying) or {}).get("days"),
        }
        nl, maint = acct.get("net_liq"), acct.get("maintenance")
        after = (maint + bp) / nl if (bp is not None and nl and maint is not None) else None
        rules = await preferences.rules()
        # Buying power already in this underlying, estimated like a new trade's, plus this one.
        under_after = None
        if bp is not None and spot and t.intent in ("open", "adjust"):
            mine = [x for x in pf.legs if x.underlying == t.underlying]
            existing = 0.0
            if mine:
                try:
                    existing = analyze(
                        [
                            TradeLeg(x.put_call, x.strike, x.expiration, x.quantity, x.mark or 0)
                            for x in mine
                        ],
                        spot,
                        today,
                    )["bp_estimate"]
                except (ValueError, ZeroDivisionError):
                    existing = None
            under_after = None if existing is None else existing + bp
        # Look-through: SPY and QQQ positions hold part of their largest stocks.
        look_bp, look_notional, via = 0.0, 0.0, []
        opening = t.intent in ("open", "adjust")
        single = t.underlying not in guards.INDEX_UNDERLYINGS and not t.stock
        if opening and single and indexweights.covered(t.underlying):
            look_bp, look_notional, via = indexweights.look_through(
                t.underlying,
                pf.legs,
                lambda sym: pf._price(sym) if hasattr(pf, "_price") else None,
                today,
                guards.naked_notional,
            )
        if under_after is not None:
            under_after += look_bp
        # Notional: uncovered short options on a single stock, with what is already held there
        # and its share of index positions.
        notional_after = None
        new_naked = guards.naked_notional(
            [(x.put_call, x.strike, x.quantity) for x in t.legs if not x.equity]
        )
        if opening and single and new_naked > 0:
            held_legs = [x for x in pf.legs if x.underlying == t.underlying and x.is_option]
            notional_after = (
                guards.naked_notional(
                    [(x.put_call, x.strike, x.quantity) for x in held_legs]
                    + [(x.put_call, x.strike, x.quantity) for x in t.legs if not x.equity]
                )
                + look_notional
            )
        look_text = (
            f", including {look_notional:,.0f} through {' and '.join(via)}" if look_notional else ""
        )
        under_cap = (
            rules.max_index_bp
            if t.underlying in guards.INDEX_UNDERLYINGS
            else rules.max_underlying_bp
        )
        errors, warnings = guards.check(
            t,
            p,
            today,
            limits=margin_limits(rules),
            held=held,
            net_liq=nl,
            bp_effect=bp,
            defined_risk=defined,
            bp_used_after=after,
            bp_ceiling=acct.get("bp_ceiling_pct"),
            block_over_ceiling=rules.block_over_ceiling,
            underlying_bp_after=under_after,
            underlying_cap=under_cap,
            notional_after=notional_after,
            notional_cap=getattr(rules, "max_notional", 0.0),
            look_through=look_text,
        )
        # Non-standard options (adjusted after a corporate action): closing only.
        held_ns = {
            x["symbol"]: st["numbers"]["nonstandard"]
            for u in (pf.snapshot or {}).get("underlyings", [])
            for st in u["strategies"]
            if st["numbers"].get("nonstandard")
            for x in st["legs"]
        }
        nonstandard_checks(t, held_ns, errors, warnings)
        # Portfolio targets: warn when the trade moves delta or theta further outside its band.
        band_after = None
        if a is not None and nl:
            beta = 1.0 if t.underlying == BENCH else None
            if beta is None:
                b = await store.get_beta(t.underlying)
                beta = b[0] if b else None
            d_delta = (
                a["delta"] * beta * spot / nl
                if a.get("delta") is not None and beta is not None
                else None
            )
            d_theta = a["theta"] / nl if a.get("theta") is not None else None
            band_after = targets.after((pf.snapshot or {}).get("bands"), d_delta, d_theta)
            warnings.extend(band_after["warnings"])
        # A wide or thin market: advice, whatever the order does.
        watch_row = await watchlist.row(t.underlying)
        warnings.extend(guards.liquidity(t, (watch_row or {}).get("atm_oi")))
        if t.intent != "close":
            fl = await watchlist.float_of(t.underlying)
            if fl and fl["low"]:
                warnings.append(
                    f"Low float: only {fl['shares'] / 1e6:.1f} million shares trade freely, "
                    "so the stock can gap sharply on news or a short squeeze"
                )
        if body.account == "sim" and t.intent in ("close", "roll") and t.trade_id is not None:
            for o in await broker.replaceable(t.trade_id, {x.symbol for x in t.legs}):
                price = f"{abs(o['limit_net']):.2f} {'credit' if o['limit_net'] > 0 else 'debit'}"
                warnings.append(
                    f"Replaces working order #{o['id']} ({o['intent']} at {price}, "
                    f"{o['duration']}); it will be cancelled when this one is placed"
                )
        if t.intent == "roll" and hasattr(pf, "strategies_for"):
            past = past_cover(
                pf.strategies_for(t.underlying),
                {x.symbol for x in t.legs if x.effect == "close"},
                [
                    (x.put_call, x.expiration, x.quantity)
                    for x in t.legs
                    if x.effect == "open" and not x.equity
                ],
            )
            if past:
                warnings.append(past)
        if t.stock and t.legs[0].quantity < 0:
            left = held.get(t.underlying, 0) + t.legs[0].quantity
            short_calls = -sum(
                q
                for sym, q in held.items()
                if q < 0
                and (o := parse_option_symbol(sym))
                and o.underlying == t.underlying
                and o.put_call == "C"
            )
            if short_calls and left < short_calls * 100:
                warnings.append(
                    f"Selling these shares leaves {short_calls} short call"
                    f"{'s' if short_calls > 1 else ''} without enough shares to cover; "
                    "close or roll the calls first"
                )
        if t.intent == "open" and body.view == "would_own":
            need = sum(
                x.strike * 100 * -x.quantity for x in t.legs if x.put_call == "P" and x.quantity < 0
            )
            snap_ = pf.snapshot or {}
            auto_state = automations[body.account].state or {}
            reserved = sum(
                cash_mod.put_cost(x)
                for u in snap_.get("underlyings", [])
                for x in u["strategies"]
                if x.get("trade_id") is not None
                and auto_state.get("trades", {}).get(str(x["trade_id"]), {}).get("view")
                == "would_own"
            )
            free = cash_mod.free_cash(snap_.get("account")) - reserved
            if need and need > free:
                warnings.append(
                    f"Would own: not cash-secured. Assignment would need {need:,.0f}; "
                    f"{max(free, 0):,.0f} of cash and sweep is free after your other Would own "
                    "puts. Automanage will close it on expiration day rather than use margin."
                )
        if t.intent == "open" and not t.stock:
            kind = ticket_strategy(t)
            if not_tasty(kind, 1.0) or (
                kind in ("Put vertical", "Call vertical")
                and (body.limit_net if body.limit_net is not None else p.get("mid") or 0) < 0
            ):
                warnings.append(
                    "Not a tastylive trade: debit verticals and long options are not managed "
                    "by automanage"
                )
            if kind in ("Jade lizard", "Big lizard", "Reverse jade lizard", "Reverse big lizard"):
                pc = "P" if kind.startswith("Reverse") else "C"
                ks = sorted(x.strike for x in t.legs if x.put_call == pc)
                spread, side = ("put", "downside") if pc == "P" else ("call", "upside")
                credit = body.limit_net if body.limit_net is not None else p.get("mid")
                if credit is not None and credit < ks[-1] - ks[0] - 1e-9:
                    warnings.append(
                        f"The {credit:.2f} credit is less than the {spread} spread's "
                        f"{ks[-1] - ks[0]:g} width, so the {side} is not free of risk: "
                        f"a {kind.lower()} wants a credit of at least the width"
                    )
        body_json = None
        try:
            body_json = schwab_order(t)
        except TicketError as e:
            errors.append(str(e))
        preview = None
        # Schwab can only preview against the real account: opening orders, or live closes.
        if body_json and (t.intent == "open" or body.account != "sim"):
            try:
                preview = summarize_preview(
                    await schwab.call(
                        "preview_order",
                        # A simulated order is previewed against the first Schwab account.
                        await registry.resolver(
                            "live" if body.account == "sim" else body.account
                        )(),
                        body_json,
                    )
                )
            except Exception as e:
                preview = {"ok": False, "error": str(e)[:300], "fields": {}, "messages": []}
        contracts = sum(abs(x.quantity) for x in t.legs if not x.equity)
        live_blocks = []
        if body.account != "sim":
            live_blocks = await brokers[body.account].gate(t, bp, nl)
            if preview is not None and not preview.get("ok") and t.intent in ("close", "roll"):
                preview = await _expected_oversold(preview, t, brokers[body.account], warnings)
            if preview is not None and not preview.get("ok"):
                live_blocks.append(
                    "Schwab's preview did not accept the order: "
                    + (
                        preview.get("error")
                        or "; ".join(
                            m["text"] for m in preview.get("messages", []) if m["level"] == "error"
                        )
                        or "see the preview"
                    )
                )
        return {
            "account": body.account,
            "live_blocks": live_blocks,
            "intent": t.intent,
            "strategy": ticket_strategy(t),
            "underlying": t.underlying,
            "spot": spot,
            "units": t.units,
            "multiplier": t.multiplier,
            "prices": p,
            "limit_net": t.limit_net,
            "duration": t.duration,
            "legs": [
                {
                    "symbol": x.symbol,
                    "instruction": x.instruction,
                    "quantity": abs(x.quantity),
                    "bid": x.bid,
                    "ask": x.ask,
                    "mark": x.mark,
                    "put_call": x.put_call,
                    "strike": x.strike,
                    "expiration": x.expiration.isoformat() if x.expiration else None,
                }
                for x in t.legs
            ],
            "bp_effect": bp,
            # Probability of profit and break-evens at the limit set (opening trades only).
            "pop": a["pop"] if a else None,
            "breakevens": a["breakevens"] if a else None,
            # The profit and loss chart at the limit set, as on the Trade page.
            "payoff": {k: a[k] for k in ("curve", "breakevens", "dte", "expected_move")}
            if a
            else None,
            "entry": entry,
            "defined_risk": defined,
            "bp_used_after": after,
            "band_after": band_after,
            "commission": round(contracts * broker.commission, 2),
            "errors": errors,
            "warnings": warnings,
            "schwab_json": body_json,
            "schwab_preview": preview,
        }

    @app.post("/api/orders/preview", dependencies=[Depends(require_login)])
    async def api_order_preview(body: OrderIn):
        t, p, quotes = await build_ticket(body)
        return await assess(body, t, p, quotes)

    def net_liq(account: str) -> float | None:
        v = ((portfolios[account].snapshot or {}).get("account") or {}).get("net_liq")
        return float(v) if v else None

    @app.post("/api/orders/copies", dependencies=[Depends(require_login)])
    async def api_order_copies(body: CopiesIn):
        """The same opening order for each other account, sized to its net liq: the
        number of spreads times that account's net liq over this one's, rounded, at least one.
        Each copy
        asked for is previewed against its own account, with its own guards."""
        src = body.order
        known(src.account)
        if src.intent != "open":
            raise HTTPException(
                status_code=400, detail="Only opening orders copy to other accounts"
            )
        t, _, _ = await build_ticket(src)
        hide_sim = await sim_hidden()
        targets = [a["key"] for a in registry.in_use() if a["key"] in portfolios]
        if not hide_sim:
            targets.append("sim")
        base = net_liq(src.account)
        rows = []
        for acct in targets:
            if acct == src.account:
                continue
            nl = net_liq(acct)
            # At least one spread: a small account still gets the trade if its own guards allow it.
            scaled = max(1, round(t.units * nl / base)) if base and nl else None
            units = body.units.get(acct, scaled)
            row = {
                "account": acct,
                "name": "Simulated" if acct == "sim" else registry.name(acct),
                "net_liq": nl,
                "scaled": scaled,
                "units": units,
                "legs": None,
                "preview": None,
                "errors": [],
            }
            if nl is None or base is None:
                row["errors"].append("No net liq for this account yet, so the size can't be scaled")
            elif not units or units < 0:
                row["errors"].append(
                    f"At this account's net liq ({nl:,.0f}) the trade scales to less than one"
                )
            else:
                row["legs"] = [
                    {"symbol": x.symbol, "quantity": r * units, "effect": x.effect}
                    for x, r in zip(t.legs, t.ratios(), strict=True)
                ]
                if acct in body.preview:
                    copy = src.model_copy(
                        update={
                            "account": acct,
                            "legs": [OrderLegIn(**leg) for leg in row["legs"]],
                            "limit_net": t.limit_net,
                            "proposal_id": None,
                        }
                    )
                    try:
                        ct, cp, cq = await build_ticket(copy)
                        row["preview"] = await assess(copy, ct, cp, cq)
                    except HTTPException as e:
                        row["errors"].append(str(e.detail))
            rows.append(row)
        return {"net_liq": base, "units": t.units, "copies": rows}

    async def mark_key_order(account: str, order_id, name: str) -> None:
        """Remember that an API key placed this order, for the Activity list."""
        if order_id is None:
            return
        marks = dict(await store.get_setting(KEY_ORDERS_KEY) or {})
        marks[f"{account}:{order_id}"] = name
        # Oldest first in insertion order; keep the most recent.
        if len(marks) > MAX_KEY_ORDERS:
            marks = dict(list(marks.items())[-MAX_KEY_ORDERS:])
        await store.set_setting(KEY_ORDERS_KEY, marks)

    async def with_key_marks(account: str, orders: list[dict]) -> list[dict]:
        """Orders, each placed by an API key carrying placed_by_key: the key's name."""
        marks = await store.get_setting(KEY_ORDERS_KEY) or {}
        return [
            {**o, "placed_by_key": marks[f"{account}:{o['id']}"]}
            if f"{account}:{o['id']}" in marks
            else o
            for o in orders
        ]

    async def key_order_notice(key: dict, body: OrderIn) -> None:
        """Tell the user an API key placed an order. Never raises: the order already went."""
        where = "simulated account" if body.account == "sim" else registry.name(body.account)
        legs = ", ".join(f"{leg.quantity:+d} {leg.symbol}" for leg in body.legs)
        msg = f"API key {key['name']!r} placed a {body.intent} order on the {where}: {legs}"
        log.info(msg)
        if notifier.enabled:
            try:
                await notifier.send("Order placed by an API key", msg)
            except Exception as e:  # noqa: BLE001
                log.warning("API key order notification failed: %s", e)

    async def place_order(body: OrderIn) -> dict:
        if body.limit_net is None:
            raise HTTPException(status_code=400, detail="Set a limit price")
        t, p, quotes = await build_ticket(body)
        result = await assess(body, t, p, quotes)
        if result["errors"]:
            raise HTTPException(status_code=400, detail="; ".join(result["errors"]))
        if body.account != "sim":
            if result["live_blocks"]:
                raise HTTPException(status_code=403, detail="; ".join(result["live_blocks"]))
            try:
                order = await brokers[body.account].place(
                    t,
                    trade_id=body.trade_id,
                    profit_target=t.profit_target,
                    entry=result["entry"],
                    replaces=body.replaces,
                )
            except SchwabError as e:
                raise _schwab_errors(e) from e
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            portfolios[body.account].refresh_soon()
            chains[body.account].refresh_soon()
            return order
        try:
            order = await broker.place(t, quotes, entry=result["entry"], replaces=body.replaces)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        sim_portfolio.refresh_soon()
        return order

    @app.post("/api/orders", dependencies=[Depends(require_login)])
    async def api_order_place(body: OrderIn, request: Request):
        order = await place_order(body)
        key = getattr(request.state, "api_key", None)
        if key is not None:
            await mark_key_order(body.account, order.get("id"), key["name"])
            await key_order_notice(key, body)
        if body.replaces is not None:
            # The old order's "automanage on fill" goes with it; the ticket's choice stands.
            await automations[body.account].forget_entry(body.replaces)
        if body.automanage and body.intent == "open":
            view = body.view if body.view in targets.VIEWS else None
            await automations[body.account].watch_entry(order, view, body.play)
        if body.proposal_id:
            await automations[body.account].placed_from_ticket(body.proposal_id)
        return order

    async def auto_submit(raw: dict, place: bool) -> dict:
        """Automanage's way in: the same ticket, guards and checks as a manual order.
        Previews when place is false. Raises ValueError when refused."""
        try:
            body = OrderIn(**raw)
            if place:
                return await place_order(body)
            t, p, quotes = await build_ticket(body)
            return await assess(body, t, p, quotes)
        except HTTPException as e:
            raise ValueError(str(e.detail)) from e
        except ValueError as e:
            raise ValueError(str(e)) from e

    automations = {
        "sim": AutomationService(
            store,
            sim_portfolio,
            broker,
            source,
            preferences,
            notifier,
            auto_submit,
            app_url=settings.app_url,
            clock=lambda: sim_portfolio.clock(),
        ),
        # Live automanage proposes at most (every live order waits for your tap) unless
        # LIVE_AUTO is set and Schwab orders allow automatic ones; set at start and on save.
        "live": AutomationService(
            store,
            portfolio,
            live_broker,
            source,
            preferences,
            notifier,
            auto_submit,
            app_url=settings.app_url,
            clock=lambda: portfolio.clock(),
            account="live",
            max_level="propose",
            name=lambda: registry.name("live"),
        ),
    }
    app.state.accounts["automations"] = automations
    app.state.automation = automations["sim"]
    running = {"on": False}
    account_tasks: dict[str, list[asyncio.Task]] = {}

    def add_account(key: str) -> None:
        """Builds a Schwab account's own services: its portfolio, trade chains, order check,
        orders and automanage. The first account ("live") is built above."""
        resolve = registry.resolver(key)
        pf = PortfolioService(
            source,
            store,
            settings,
            connection_json,
            resolve,
            mode="live",
            preferences=preferences,
            notifier=notifier,
            account=key,
            name=lambda: registry.name(key),
        )
        pf.stream = stream
        pf.earnings = earnings.info
        pf.ivref = iv_reference.rank
        cs = ChainService(
            source, store, pf, resolve, account=key, sheet_label=lambda: registry.sheet_label(key)
        )
        pf.trade_info = cs.trade_info
        pf.chain_holdings = cs.holdings
        pf.chain_list = lambda: cs.result.get("chains", [])
        portfolios[key] = pf
        chains[key] = cs
        checks[key] = OrderCheckService(
            source, store, resolve, enabled=lambda: connection_status().connected, account=key
        )
        brokers[key] = LiveBroker(
            store,
            schwab,
            source,
            resolve,
            server_allowed=settings.live_trading,
            notifier=notifier,
            app_url=settings.app_url,
            server_auto=settings.live_auto,
            account=key,
            name=lambda: registry.name(key),
        )
        automations[key] = AutomationService(
            store,
            pf,
            brokers[key],
            source,
            preferences,
            notifier,
            auto_submit,
            app_url=settings.app_url,
            clock=lambda: portfolio.clock(),
            account=key,
            max_level=brokers[key].auto_cap(),
            name=lambda: registry.name(key),
        )

    def start_account(key: str) -> None:
        """Starts a Schwab account's background services (when the app's are running)."""
        if not running["on"] or account_tasks.get(key):
            return
        name = registry.name(key)
        jobs_ = {
            f"{name}: portfolio": portfolios[key].run,
            f"{name}: publisher": portfolios[key].publisher,
            f"{name}: trade chains": chains[key].run,
            f"{name}: order check": checks[key].run,
            f"{name}: orders": brokers[key].run,
            f"{name}: automation": automations[key].run,
        }
        account_tasks[key] = [
            asyncio.create_task(supervise(n, fn, health), name=n) for n, fn in jobs_.items()
        ]

    def stop_account(key: str) -> None:
        for t in account_tasks.pop(key, []):
            t.cancel()

    async def sync_accounts() -> None:
        """After a sign-in: start the accounts newly ticked, stop the ones no longer ticked."""
        added, gone = await registry.refresh()
        for key in added:
            if key not in brokers:
                add_account(key)
            start_account(key)
        for key in gone:
            if key != "live":
                stop_account(key)
        await apply_live_cap()

    def rule_defaults(rules: Rules) -> dict:
        """Each rule's default for the current margin type and delta bias, for the Settings
        page's changed-from-default markers and Reset buttons."""
        d = asdict(Rules())
        d.update(MARGIN_DEFAULTS[rules.margin_type])
        d["delta_min"], d["delta_max"] = targets.BIAS_BANDS[rules.delta_bias]
        return d

    def scan_defaults(rules: Rules) -> dict:
        d = copy.deepcopy(scan_mod.DEFAULTS)
        d["max_bp_share"] = 0.05 * MARGIN_SCALE[rules.margin_type]
        return d

    async def apply_live_cap() -> None:
        for key, b in brokers.items():
            automations[key].max_level = b.auto_cap()

    Acct = str

    @app.get("/api/automation", dependencies=[Depends(require_login)])
    async def api_automation(account: Acct = "sim", log: bool = True):
        """log=0 leaves out the recent actions: the app's own poll never shows them."""
        known(account)
        return await automations[account].view(log=log)

    async def allowed_symbols() -> dict:
        """Each Schwab account's allowed underlyings (None: any), by its name."""
        out = {}
        for key, name in schwab_accounts():
            c = await brokers[key].config() if key in brokers else {"all_symbols": True}
            out[name] = None if c.get("all_symbols") else list(c["symbols"])
        return out

    async def scanner_map() -> dict:
        live_syms = await allowed_symbols()
        return scan_mod.scan_map(
            scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY)),
            live_syms,
            (await preferences.rules()).delta_bias,
            await preferences.rules(),
        )

    async def scan_steps_html(rules_now) -> list:
        """The scanner's step list for Settings, with each setting editable in place."""
        live_syms = await allowed_symbols()
        s = scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY))
        fields = scan_mod.scan_fields(
            s, live_broker.auto_cap() == "auto", scan_defaults(rules_now), schwab_accounts()
        )
        rv = scan_mod.rules_values(rules_now)
        return [
            (title, [plain.as_html(line, fields, rv, "s") for line in lines])
            for title, lines in scan_mod.scan_steps(rules_now.delta_bias, live_syms)
        ]

    async def runs_by_itself() -> list[tuple[str, list[str]]]:
        """What happens on each account without a tap, in one line each, for the top of
        the Automation and Scanner tabs: every gate that is on or off, in one place."""
        scan_s = scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY))
        live = await live_broker.status()
        out = []
        for acct, name in [("sim", "Simulated"), *schwab_accounts()]:
            if acct not in automations:
                continue
            v = await automations[acct].view(log=False)
            parts = []
            mode = v.get("mode", "auto")
            if mode == "off" or v["paused"]:
                parts.append("Automation Off: nothing automatic")
            elif mode == "propose":
                parts.append("Automation Propose: every rule and the scanner at most propose")
            else:
                parts.append("Automation Auto: each rule and the scanner as set")
            if v.get("blocked") and mode != "off":
                parts.append(f"holding: {v['blocked']}")
            if acct != "sim":
                if not live["on"]:
                    parts.append(
                        "Schwab orders not allowed: add LIVE_TRADING=true to .env and restart"
                    )
                elif live["auto_cap"] == "auto":
                    parts.append("Schwab orders allowed, automatic ones too (LIVE_AUTO=true)")
                else:
                    parts.append(
                        "Schwab orders allowed (LIVE_TRADING=true), automatic ones not: add "
                        "LIVE_AUTO=true to .env and restart; until then Auto is not offered"
                    )
            n = sum(1 for t in v["trades"].values() if t.get("enabled"))
            parts.append(f"automanage on {n} trade{'' if n == 1 else 's'}")
            k = sum(1 for lv in v["settings"]["levels"].values() if lv == "auto")
            parts.append(
                Markup(  # noqa: S704 - only numbers and fixed words
                    f"{k} rule{'' if k == 1 else 's'} on Auto "
                    '(see <a href="#/settings/auto-rules">automation</a>)'
                )
            )
            parts.append(f"scanner {scanner.level(scan_s, acct).capitalize()}")
            out.append((name, parts))
        return out

    def auto_steps_html(rules_now, settings_now) -> list:
        """Automanage's step list for Settings, with each setting editable in place."""
        fields = auto_fields(settings_now, live_broker.auto_cap() == "auto", schwab_accounts())
        rv = auto_rules_values(rules_now)
        return [
            (title, [plain.as_html(x, fields, rv, "a") for x in lines])
            for title, lines in AUTO_STEPS
        ]

    @app.get("/api/scanner/map", dependencies=[Depends(require_login)])
    async def api_scanner_map():
        return await scanner_map()

    @app.get("/api/automation/map", dependencies=[Depends(require_login)])
    async def api_automation_map():
        settings_now = as_run(
            merged_settings(await store.get_setting(SETTINGS_KEY)),
            live_broker.auto_cap() == "auto",
        )
        return rules_map(await preferences.rules(), settings_now, schwab_accounts())

    @app.post("/api/automation/trade", dependencies=[Depends(require_login)])
    async def api_automation_trade(body: AutoTradeIn, account: Acct = "sim"):
        known(account)
        kwargs = {}
        if body.view is not None:
            kwargs["view"] = None if body.view == "portfolio" else body.view
        try:
            await automations[account].set_trade(body.trade_id, body.enabled, **kwargs)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return await automations[account].view()

    @app.post("/api/automation/mode", dependencies=[Depends(require_login)])
    async def api_automation_mode(body: ModeIn, account: Acct = "sim"):
        """The account's one switch: off, propose or auto."""
        known(account)
        try:
            await automations[account].set_mode(body.mode)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return await automations[account].view()

    @app.post("/api/automation/pause", dependencies=[Depends(require_login)])
    async def api_automation_pause(body: PauseIn, account: Acct = "sim"):
        known(account)
        await automations[account].set_paused(body.paused)
        return await automations[account].view()

    @app.post("/api/automation/proposal/{pid}/dismiss", dependencies=[Depends(require_login)])
    async def api_automation_dismiss(pid: str, account: Acct = "sim"):
        known(account)
        await automations[account].dismiss(pid)
        return await automations[account].view()

    @app.post("/api/automation/proposal/{pid}/accept", dependencies=[Depends(require_login)])
    async def api_automation_accept(pid: str, account: Acct = "sim"):
        known(account)
        try:
            await automations[account].accept(pid)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        portfolios[account].refresh_soon()
        return await automations[account].view()

    scanner = scan_mod.Scanner(
        store,
        source,
        portfolios,
        automations,
        watchlist,
        preferences,
        brokers,
        clock=lambda: portfolio.clock(),
        earnings=earnings,
        history=history,
    )
    app.state.scanner = scanner

    @app.get("/api/scanner", dependencies=[Depends(require_login)])
    async def api_scanner(account: Acct = "sim"):
        known(account)
        return await scanner.view(account)

    @app.post("/api/scanner/run", dependencies=[Depends(require_login)])
    async def api_scanner_run(account: Acct = "sim", act: bool = True):
        known(account)
        scanner.start(account, act=act)
        return await scanner.view(account)

    @app.post("/api/scanner/dismiss", dependencies=[Depends(require_login)])
    async def api_scanner_dismiss(symbol: str, account: Acct = "sim"):
        known(account)
        await scanner.dismiss(account, _check_symbol(symbol))
        return await scanner.view(account)

    @app.get("/api/live", dependencies=[Depends(require_login)])
    async def api_live(account: str = "live"):
        known(account)
        if account == "sim":
            raise HTTPException(status_code=404, detail="Live orders are for Schwab accounts")
        b = brokers[account]
        return {**await b.status(), "audit": await b.audit()}

    @app.get("/api/accounts/in-use", dependencies=[Depends(require_login)])
    async def api_accounts_in_use():
        """The Schwab accounts the app runs, by their Schwab name, for the header picker."""
        return {
            "accounts": [a for a in registry.in_use() if a["key"] in portfolios],
            "hide_sim": await sim_hidden(),
        }

    @app.get("/api/orders/{order_id}/entry", dependencies=[Depends(require_login)])
    async def api_order_entry(order_id: str, account: Acct = "sim"):
        """Whether a working order turns automanage on when it fills, and with which view
        and play: the ticket that replaces it starts from the same choice."""
        known(account)
        e = await automations[account].entry(order_id)
        return {
            "automanage": e is not None,
            "view": (e or {}).get("view"),
            "play": (e or {}).get("play"),
        }

    @app.post("/api/orders/{order_id}/cancel", dependencies=[Depends(require_login)])
    async def api_order_cancel(order_id: str, account: Acct = "sim"):
        known(account)
        if account != "sim":
            try:
                order = await brokers[account].cancel(order_id)
            except SchwabError as e:
                raise _schwab_errors(e) from e
            portfolios[account].refresh_soon()
            return order
        try:
            order = await broker.cancel(int(order_id))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        sim_portfolio.refresh_soon()
        return order

    def sim_chains(st: dict) -> list[dict]:
        held: dict[int, dict[str, int]] = {}
        for p in st["positions"]:
            held.setdefault(p["trade_id"], {})[p["symbol"]] = p["quantity"]
        out = []
        for tr in st["trades"]:
            events = [dict(e) for e in tr["events"]]
            if events:
                entry = tr.get("entry") or {}
                events[0]["meta"] = {
                    "source": "sim",
                    "entry": entry,
                    "spot": entry.get("spot"),
                    "bpr": entry.get("bp"),
                }
            out.append(
                {
                    **tr,
                    "events": events,
                    "source": "sim",
                    "carried_in": False,
                    "holdings": held.get(tr["id"], {}),
                }
            )
        return out

    sim_portfolio.chain_list = lambda: sim_chains(broker.state) if broker.state else []

    @app.get("/api/covered", dependencies=[Depends(require_login)])
    async def api_covered(symbol: str, account: str = "live"):
        """Shares' cost basis and the covered calls (covered puts, for short shares) written
        against them since the oldest shares still held were bought."""
        known(account)
        symbol = _check_symbol(symbol)
        pf = portfolios[account]
        u = next((x for x in pf.snapshot.get("underlyings", []) if x["symbol"] == symbol), None)
        shares = [
            s
            for s in (u or {}).get("strategies", [])
            if s["legs"] and all(x["kind"] == "EQUITY" for x in s["legs"])
        ]
        if not shares:
            raise HTTPException(status_code=404, detail=f"No shares of {symbol} held")
        # Long shares first: covered calls; short shares only when no long ones.
        long_ = [s for s in shares if s["legs"][0]["quantity"] > 0]
        sts, side = (long_, "long") if long_ else (shares, "short")
        q = sum(x["quantity"] for s in sts for x in s["legs"])
        avg = sum(x["quantity"] * x["avg_price"] for s in sts for x in s["legs"]) / q
        since = sts[0]["numbers"].get("covered_since")
        ids = [str(s["trade_id"]) for s in sts if s["trade_id"] is not None]
        cov = (
            chains_mod.covered_premium(pf.chain_list() or [], symbol, side, since, ids)
            if since is not None and pf.chain_list
            else {"premium": 0.0, "chains": []}
        )
        per = cov["premium"] / abs(q)
        return {
            "symbol": symbol,
            "side": side,
            "shares": q,
            "avg_price": round(avg, 4),
            "premium": cov["premium"],
            "per_share": round(per, 4),
            "adjusted": round(avg - per if side == "long" else avg + per, 4),
            "since": since,
            "chains": cov["chains"],
        }

    @app.get("/api/chains", dependencies=[Depends(require_login)])
    async def api_chains(account: str = "live"):
        known(account)
        if account == "sim":
            return {
                "chains": sim_chains(await broker.snapshot()),
                "proposals": [],
                "mismatches": [],
                "unmatched": [],
                "synced_at": None,
                "error": None,
            }
        cs = chains[account]
        return {**cs.result, "synced_at": cs.synced_at, "error": cs.error}

    @app.get("/api/chains/{chain_id}", dependencies=[Depends(require_login)])
    async def api_trade_chain(chain_id: str, account: str = "live"):
        known(account)
        pool = (
            sim_chains(await broker.snapshot())
            if account == "sim"
            else chains[account].result["chains"]
        )
        found = next((c for c in pool if str(c["id"]) == chain_id), None)
        if found is None:
            raise HTTPException(status_code=404, detail="That trade's history is not available")
        return found

    @app.get("/api/order-check", dependencies=[Depends(require_login)])
    async def api_order_check(account: str = "live"):
        known(account)
        if account == "sim":
            raise HTTPException(status_code=404, detail="Order check is for Schwab accounts")
        return await checks[account].view()

    @app.post("/api/order-check/refresh", dependencies=[Depends(require_login)])
    async def api_order_check_refresh(account: str = "live"):
        known(account)
        if account == "sim":
            raise HTTPException(status_code=404, detail="Order check is for Schwab accounts")
        try:
            await checks[account].sync()
        except SchwabError as e:
            raise _schwab_errors(e) from e
        return await checks[account].view()

    @app.post("/api/chains/link", dependencies=[Depends(require_login)])
    async def api_chain_link(body: LinkIn, account: str = "live"):
        known(account)
        try:
            await chains[account].link(body.group, body.chain)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        portfolios[account].refresh_soon()
        return {"ok": True}

    @app.post("/api/chains/split", dependencies=[Depends(require_login)])
    async def api_chain_split(body: SplitIn, account: str = "live"):
        known(account)
        if account == "sim":
            raise HTTPException(status_code=400, detail="Only Schwab trade histories split")
        await chains[account].split(body.group)
        portfolios[account].refresh_soon()
        return {"ok": True}

    @app.get("/api/activity", dependencies=[Depends(require_login)])
    async def api_activity(
        account: str = "live",
        source: Literal["all", "sheet", "schwab"] = "all",
        limit: int = 300,
    ):
        if account == combined.ALL:
            return await all_activity(source, limit)
        known(account)
        limit = max(1, min(limit, 20000))
        if account != "sim":
            cs, lb = chains[account], brokers[account]
            chs = cs.result["chains"]
            counts = {"all": len(chs)}
            for c in chs:
                counts[c.get("source") or "schwab"] = counts.get(c.get("source") or "schwab", 0) + 1
            shown = [c for c in chs if source == "all" or (c.get("source") or "schwab") == source]
            return {
                "account": account,
                "name": registry.name(account),
                # Orders at Schwab from the last week, working first, as the live broker
                # last read them (every 10 seconds while one of the app's orders works).
                "orders": await with_key_marks(
                    account,
                    sorted(
                        lb.state["orders"],
                        key=lambda o: (o["status"] != "WORKING", -(o["created_at"] or 0)),
                    )[:200],
                ),
                "live": await lb.status(),
                "trades": sorted(
                    shown,
                    # Open chains first, then the most recently closed.
                    key=lambda c: (c["status"] != "open", -(c["closed_at"] or c["opened_at"] or 0)),
                )[:limit],
                "trade_counts": counts,
                "trades_shown_of": len(shown),
                "sheet": {
                    "rows": cs.history.get("rows", 0),
                    "error": cs.history.get("error"),
                },
                "stats": stats(chs),
                "proposals": cs.result["proposals"],
                "mismatches": cs.result["mismatches"],
                "synced_at": cs.synced_at,
                "error": cs.error,
            }
        st = await broker.snapshot()
        acct = (sim_portfolio.snapshot or {}).get("account") or {}
        return {
            "account": "sim",
            "cash": st["cash"],
            "start_cash": st["start_cash"],
            "net_liq": acct.get("net_liq"),
            "orders": await with_key_marks(
                "sim", sorted(st["orders"], key=lambda o: -o["id"])[:300]
            ),
            "trades": sorted(sim_chains(st), key=lambda tr: -tr["id"])[:300],
            "stats": stats(st["trades"]),
        }

    async def all_activity(source: str, limit: int) -> dict:
        """Every Schwab account's Activity as one, each order and trade tagged."""
        parts = [(k, n, await api_activity(k, source, limit)) for k, n in schwab_accounts()]
        first = parts[0][2]
        orders = [o for k, n, a in parts for o in combined.tag(a["orders"], k, n)]
        orders.sort(key=lambda o: (o["status"] != "WORKING", -(o["created_at"] or 0)))
        trades = [t for k, n, a in parts for t in combined.tag(a["trades"], k, n)]
        trades.sort(key=lambda c: (c["status"] != "open", -(c["closed_at"] or c["opened_at"] or 0)))
        counts: dict[str, int] = {}
        for _, _, a in parts:
            for c, v in (a.get("trade_counts") or {}).items():
                counts[c] = counts.get(c, 0) + v
        everything = [c for k, _, _ in parts for c in chains[k].result["chains"]]
        errors = [f"{n}: {a['error']}" for _, n, a in parts if a.get("error")]
        sheet_errors = [f"{n}: {a['sheet']['error']}" for _, n, a in parts if a["sheet"]["error"]]
        synced = [a["synced_at"] for _, _, a in parts if a.get("synced_at")]
        return {
            **first,
            "account": combined.ALL,
            "name": "All accounts",
            "orders": orders[:400],
            "trades": trades[:limit],
            "trade_counts": counts,
            "trades_shown_of": sum(a["trades_shown_of"] for _, _, a in parts),
            "sheet": {
                "rows": sum(a["sheet"]["rows"] for _, _, a in parts),
                "error": "; ".join(sheet_errors) or None,
            },
            "stats": stats(everything),
            "proposals": [p for k, n, a in parts for p in combined.tag(a["proposals"], k, n)],
            "mismatches": [m for k, n, a in parts for m in combined.tag(a["mismatches"], k, n)],
            "synced_at": min(synced) if synced else None,
            "error": "; ".join(errors) or None,
        }

    # Trading history and dashboard ------------------------------------------------------

    prices_service = PriceHistoryService(
        source,
        store,
        chains,
        sim_trades=lambda: sim_chains(broker.state) if broker.state else [],
    )

    @app.get("/api/dashboard", dependencies=[Depends(require_login)])
    async def api_dashboard(
        account: str = "live",
        source: Literal["all", "sheet", "schwab"] = "all",
        futures: bool = False,
    ):
        closed_by_raw: dict = {}
        if account == combined.ALL:
            # Every Schwab account's history together.
            chs, realized, fut, starts = [], [], [], []
            for k, n in schwab_accounts():
                h = chains[k].history
                chs += combined.tag(chains[k].result["chains"], k, n)
                realized += h["realized"]
                fut += h["futures"]
                if h["window_start"]:
                    starts.append(h["window_start"])
                closed_by_raw.update((await automations[k].load()).get("closed_by") or {})
            window = min(starts) if starts else None
        elif account == "sim":
            known(account)
            chs = sim_chains(await broker.snapshot())
            realized, fut, window = [], [], None
        else:
            known(account)
            h = chains[account].history
            chs = chains[account].result["chains"]
            realized, fut, window = h["realized"], h["futures"], h["window_start"]
        if account != combined.ALL:
            closed_by_raw = (await automations[account].load()).get("closed_by") or {}
        symbols = {dash.price_symbol(c["underlying"]) for c in chs} | {"$VIX"}
        prices = await store.prices(symbols)
        out = dash.build(
            chs,
            realized,
            window,
            fut,
            prices,
            source=source if account != "sim" else "all",
            include_futures=futures,
            closed_by={tid: RULES.get(rule, rule) for tid, rule in closed_by_raw.items()},
        )
        out["prices_pending"] = prices_service.status.get("pending")
        out["account"] = account
        return out

    @app.get("/api/export/sheet", dependencies=[Depends(require_login)])
    async def api_export_sheet(start: date, end: date, account: str = "live"):
        if account == "sim":
            raise HTTPException(status_code=400, detail="Export is for Schwab accounts")
        # All: every Schwab account's trades in one sheet, each row with its account.
        keys = [k for k, _ in schwab_accounts()] if account == combined.ALL else [account]
        rows = []
        for k in keys:
            known(k)
            label = chains[k].sheet_label() or registry.name(k)
            rows += export_rows(chains[k].result["chains"], start, end, label)
        content = export_xlsx(rows)
        name = f"trades-{start.isoformat()}-to-{end.isoformat()}.xlsx"
        return StreamingResponse(
            iter([content]),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.post("/api/sim/reset", dependencies=[Depends(require_login)])
    async def api_sim_reset(body: ResetIn):
        if body.confirm != "RESET":
            raise HTTPException(status_code=400, detail="Type RESET to confirm")
        await reset_sim(body.start_cash)
        return {"ok": True}

    async def all_events(request: Request):
        """All accounts' snapshot, sent again whenever any Schwab account's changes."""
        pfs = [portfolios[k] for k, _ in schwab_accounts() if k in portfolios]
        queues = [(pf, pf.subscribe()) for pf in pfs]
        wake: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def forward(q: asyncio.Queue) -> None:
            while True:
                await q.get()
                if not wake.full():
                    wake.put_nowait(True)

        tasks = [asyncio.create_task(forward(q)) for _, q in queues]
        try:
            snap = all_snapshot()
            if not snap.get("pending"):
                yield f"data: {json.dumps(snap, default=str)}\n\n"
            while not await request.is_disconnected():
                try:
                    await asyncio.wait_for(wake.get(), timeout=15)
                    yield f"data: {json.dumps(all_snapshot(), default=str)}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            for t in tasks:
                t.cancel()
            for pf, q in queues:
                pf.unsubscribe(q)

    @app.get("/api/events", dependencies=[Depends(require_login)])
    async def api_events(request: Request, account: str = "live"):
        if account == combined.ALL:
            return StreamingResponse(
                all_events(request),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        known(account)
        pf = portfolios[account]
        queue = pf.subscribe()

        async def stream():
            try:
                if pf.snapshot:
                    yield f"data: {pf.snapshot_text()}\n\n"
                while not await request.is_disconnected():
                    try:
                        text = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {text}\n\n"
                    except TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                pf.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/openapi.json", dependencies=[Depends(require_login)])
    async def api_openapi():
        return JSONResponse(app.openapi())

    app.add_middleware(McpPath)
    return app
