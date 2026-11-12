import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

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
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import chain as chain_mod
from . import dashboard as dash
from . import guards, portfolio_view, targets
from . import scanner as scan_mod
from .accounts import AccountSelector
from .analysis import TradeLeg, analyze
from .automation import (
    LEVELS,
    RULES,
    SETTING_FIELDS,
    SETTINGS_KEY,
    AutomationService,
    merged_settings,
    parse_settings,
)
from .chain_service import REVIEW_KEY, SHEET_ACCOUNT, ChainService
from .config import Settings, get_settings
from .datasource import DemoDataSource, SchwabDataSource
from .earnings import EarningsService
from .extremes import PriceHistory
from .extremes import compute as compute_extreme
from .history_service import PriceHistoryService, export_rows, export_xlsx
from .jobs import DailyJobs
from .live import LiveBroker
from .market import MarketService
from .notify import PushoverNotifier, ReminderService
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
from .portfolio import BENCH, PortfolioService
from .preferences import (
    _SYMBOL,
    BAND_FIELDS,
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
)
from .sim import SimBroker, SimDataSource, stats
from .store import PgStore
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
    account: Literal["sim", "live"] = "sim"
    intent: Literal["open", "close", "roll", "adjust"] = "open"
    trade_id: int | str | None = None
    # Schwab's four-leg limit is checked with a clear message in validate_shape.
    legs: list[OrderLegIn] = Field(min_length=1, max_length=8)
    limit_net: float | None = None
    duration: Literal["DAY", "GTC"] = "DAY"
    profit_target: float | None = Field(None, ge=0.05, le=0.95)


class LinkIn(BaseModel):
    group: str = Field(max_length=64)
    chain: str = Field(max_length=80)


class SplitIn(BaseModel):
    group: str = Field(max_length=64)


class ReviewIn(BaseModel):
    id: str = Field(max_length=200)
    decision: Literal["accept", "ignore"] | None = None


class AutoTradeIn(BaseModel):
    trade_id: int | str
    enabled: bool


class PauseIn(BaseModel):
    paused: bool


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
                            text = item.get("message") or item.get("title") or str(item)
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


class AnalyzeIn(BaseModel):
    symbol: str
    spot: float = Field(gt=0)
    vol: float | None = None
    legs: list[LegIn] = Field(min_length=1, max_length=8)
    # Shares already held that short calls are written against (a covered call).
    held_shares: int = Field(0, ge=0, le=1_000_000)


def _check_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if not _SYMBOL.match(symbol):
        raise HTTPException(status_code=400, detail="Not a valid symbol")
    return symbol


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
    demo = settings.demo_mode
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
        source = DemoDataSource() if demo else SchwabDataSource(schwab)
    notifier = notifier or PushoverNotifier(settings.pushover_user_key, settings.pushover_app_token)

    def connection_status() -> ConnectionStatus:
        if demo:
            return ConnectionStatus(connected=True, token_age_s=86400.0, created_at=0)
        return schwab.status()

    selector = AccountSelector(
        store, source, connection_status, demo_hash=DemoDataSource.HASH if demo else None
    )

    def connection_json() -> dict:
        s = connection_status()
        return {
            "connected": s.connected,
            "expires_in_days": s.expires_in_days,
            "detail": s.detail,
            "visible_accounts": selector.visible_count,
        }

    preferences = Preferences(store, settings)
    portfolio = PortfolioService(
        source,
        store,
        settings,
        connection_json,
        selector,
        mode="demo" if demo else "live",
        preferences=preferences,
        notifier=notifier,
    )
    stream = None
    if not demo:
        stream = StreamService(
            schwab, portfolio.wanted_symbols, portfolio.on_stream_quotes, portfolio.refresh_soon
        )
        portfolio.stream = stream
    chain_service = ChainService(source, store, portfolio, selector)
    order_check = OrderCheckService(
        source, store, selector, enabled=lambda: connection_status().connected
    )
    live_broker = LiveBroker(
        store,
        schwab,
        source,
        selector,
        server_allowed=settings.live_trading,
        notifier=notifier,
        app_url=settings.app_url,
        demo=demo,
        server_auto=settings.live_auto,
    )
    portfolio.trade_info = chain_service.trade_info
    portfolio.chain_holdings = chain_service.holdings
    if stream is not None:

        def on_activity():
            portfolio.refresh_soon()
            chain_service.refresh_soon()
            order_check.refresh_soon()
            live_broker.refresh_soon()

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
    if stream is not None:
        sim_portfolio.stream = stream
        stream.wanted = lambda: portfolio.wanted_symbols() | sim_portfolio.wanted_symbols()

        def both(batch):
            portfolio.on_stream_quotes(batch)
            sim_portfolio.on_stream_quotes(batch)

        stream.on_quotes = both
    portfolios = {"live": portfolio, "sim": sim_portfolio}
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
        for pf in (portfolio, sim_portfolio):
            out |= {u["symbol"] for u in (pf.snapshot or {}).get("underlyings", [])}
        out |= set((await live_broker.config())["symbols"])
        return out

    earnings = EarningsService(
        store,
        source,
        earnings_symbols,
        finnhub_key=settings.finnhub_api_key,
        clock=lambda: portfolio.clock(),
        demo=demo,
    )
    portfolio.earnings = sim_portfolio.earnings = watchlist.earnings = earnings.info
    history = PriceHistory(store, source, earnings_symbols, clock=lambda: portfolio.clock())
    market = MarketService(source, clock=lambda: portfolio.clock())

    async def watch_extreme(sym, price, change, iv30):
        closes = history.get(sym)
        if closes is None:
            return None  # read in the background; shown from the next refresh
        s = scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY))
        return compute_extreme(closes, price, change, iv30, s)

    watchlist.extremes = watch_extreme
    health: dict[str, str | None] = {}
    jobs = DailyJobs(source, store, portfolio, settings)
    reminders = ReminderService(notifier, store, connection_status, settings, demo)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.open()
        await broker.load()
        await earnings.load()
        await apply_live_cap()
        schwab.load()
        log.info("Started. Demo: %s. Schwab connected: %s", demo, schwab.status().connected)
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
            tasks = [
                asyncio.create_task(supervise(name, fn, health), name=name)
                for name, fn in services.items()
            ]
        yield
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                await t
        await schwab.close()
        await store.close()

    app = FastAPI(title="Trading", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.portfolio = portfolio
    app.state.selector = selector
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

    def require_login(request: Request) -> None:
        if not request.session.get("auth"):
            if request.url.path.startswith("/api/"):
                raise HTTPException(status_code=401, detail="Not signed in")
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
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    # Connection and settings page ------------------------------------------------------

    def settings_url(section: str = "") -> str:
        """Where the Settings page lives: inside the app when the app is built."""
        if (static_dir / "index.html").exists():
            return f"/#/settings/{section}" if section else "/#/settings"
        return f"/connect#{section}" if section else "/connect"

    @app.get("/connect", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def connect_page(request: Request, fragment: bool = False):
        # The app shows Settings inside its own page, with the same menu as every other
        # page; /connect on its own is only for when the app's front end is not built.
        if not fragment and (static_dir / "index.html").exists():
            return RedirectResponse("/#/settings", status_code=303)
        status = connection_status()
        error = request.session.pop("flash", None)
        notice = request.session.pop("notice", None)
        accounts, selected = [], await selector.selected()
        if status.connected and not demo:
            try:
                accounts = await selector.visible()
            except SchwabError as e:
                error = error or str(e)
        return templates.TemplateResponse(
            request,
            "connect_body.html" if fragment else "connect.html",
            {
                "demo": demo,
                "status": status,
                "accounts": accounts,
                "selected": selected,
                "error": error,
                "notice": notice,
                "db_error": await store.ping(),
                "callback_url": settings.schwab_callback_url,
                "one_tap": settings.schwab_callback_url.endswith("/schwab/callback"),
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
                "rules": await preferences.rules(),
                "rule_fields": RULE_FIELDS,
                "rule_notes": RULE_NOTES,
                "margin_types": MARGIN_TYPES,
                "band_fields": BAND_FIELDS,
                "biases": targets.BIASES,
                "auto": merged_settings(await store.get_setting(SETTINGS_KEY)),
                "live": await live_broker.status(),
                "scan": scan_mod.merged(await store.get_setting(scan_mod.SETTINGS_KEY)),
                "scan_fields": scan_mod.FIELDS,
                "earnings": earnings.status(),
                "earnings_manual": await earnings.manual_text(),
                "auto_rules": RULES,
                "auto_levels": LEVELS,
                "auto_fields": SETTING_FIELDS,
                "stream": None if stream is None else (stream.status, stream.detail),
                "health": health,
            },
        )

    @app.get("/schwab/authorize", dependencies=[Depends(require_login)])
    async def schwab_authorize():
        return RedirectResponse(schwab.authorization_url(), status_code=303)

    async def _finish_sign_in(request: Request, received_url: str):
        try:
            await schwab.complete_authorization(received_url)
            portfolio.refresh_soon()
            request.session["notice"] = "Schwab sign-in renewed."
        except SchwabError as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url(), status_code=303)

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
        return RedirectResponse(settings_url(), status_code=303)

    @app.post("/account/select", dependencies=[Depends(require_login)])
    async def account_select(request: Request, number: str = Form()):
        try:
            await selector.select(number)
            portfolio.refresh_soon()
            request.session["notice"] = f"Using the account ending {number[-4:]}."
        except (ValueError, SchwabError) as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url(), status_code=303)

    @app.post("/notify/test", dependencies=[Depends(require_login)])
    async def notify_test(request: Request):
        try:
            await notifier.send(
                "Test", "Notifications from the trading app work.", settings.app_url
            )
            request.session["notice"] = "Test notification sent."
        except Exception as e:
            request.session["flash"] = f"Test notification failed: {e}"
        return RedirectResponse(settings_url(), status_code=303)

    @app.post("/watchlist/import", dependencies=[Depends(require_login)])
    async def watchlist_import(
        request: Request,
        text: str = Form(""),
        file: UploadFile | None = File(None),
        mode: str = Form("replace"),
    ):
        content = text
        if file is not None and file.filename:
            data = await file.read(512 * 1024)
            content = data.decode("utf-8-sig", errors="replace")
        symbols, skipped = parse_symbols(content)
        if not symbols:
            request.session["flash"] = "No symbols found in that file or text."
            return RedirectResponse(settings_url("watchlist"), status_code=303)
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
        portfolio.watchlist = await preferences.watchlist()
        portfolio.refresh_soon()
        msg = f"Watchlist now has {len(merged)} symbols."
        if skipped:
            msg += f" Skipped (not stocks, ETFs or indexes): {', '.join(skipped[:12])}."
        if unknown:
            msg += f" Not recognised by Schwab: {', '.join(unknown[:12])}."
        request.session["notice"] = msg
        return RedirectResponse(settings_url("watchlist"), status_code=303)

    @app.post("/watchlist/remove", dependencies=[Depends(require_login)])
    async def watchlist_remove(request: Request, symbol: str = Form()):
        current = await preferences.watchlist()
        await preferences.set_watchlist([s for s in current if s != symbol])
        portfolio.watchlist = await preferences.watchlist()
        portfolio.refresh_soon()
        return RedirectResponse(settings_url("watchlist"), status_code=303)

    @app.post("/earnings/manual", dependencies=[Depends(require_login)])
    async def earnings_manual_save(request: Request, text: str = Form("")):
        bad = await earnings.set_manual(text[:20000])
        portfolio.refresh_soon()
        sim_portfolio.refresh_soon()
        if bad:
            request.session["flash"] = (
                "Could not read: "
                + "; ".join(bad[:5])
                + (". Use one line each: SYMBOL YYYY-MM-DD bmo|amc")
            )
        else:
            request.session["notice"] = "Earnings dates saved."
        return RedirectResponse(settings_url("earnings"), status_code=303)

    @app.post("/earnings/refresh", dependencies=[Depends(require_login)])
    async def earnings_refresh(request: Request):
        try:
            n = await earnings.refresh()
            request.session["notice"] = f"Earnings dates refreshed: {n} found."
        except Exception as e:
            request.session["flash"] = f"Earnings refresh failed: {type(e).__name__}: {e}"
        return RedirectResponse(settings_url("earnings"), status_code=303)

    @app.post("/scanner/settings", dependencies=[Depends(require_login)])
    async def scanner_settings_save(request: Request):
        form = dict(await request.form())
        try:
            new = scan_mod.parse_form(form, await store.get_setting(scan_mod.SETTINGS_KEY))
            await store.set_setting(scan_mod.SETTINGS_KEY, new)
            request.session["notice"] = "Scanner settings saved."
        except ValueError as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url("scanner"), status_code=303)

    @app.post("/live/settings", dependencies=[Depends(require_login)])
    async def live_settings_save(request: Request):
        form = dict(await request.form())
        try:
            c = await live_broker.set_config(form)
            await apply_live_cap()
            if not c["enabled"]:
                notice = "Schwab orders are off."
            elif not c["auto"]:
                notice = "Schwab orders: manual only. Automation proposes."
            elif live_broker.auto_cap(c) == "auto":
                notice = "Schwab orders: manual and automatic."
            else:
                notice = (
                    "Schwab orders: manual and automatic, but automatic orders also need "
                    "LIVE_AUTO=true in .env; until then automation proposes."
                )
            request.session["notice"] = notice
        except ValueError as e:
            request.session["flash"] = str(e)
        return RedirectResponse(settings_url("live"), status_code=303)

    @app.post("/automation/settings", dependencies=[Depends(require_login)])
    async def automation_settings_save(request: Request):
        form = dict(await request.form())
        try:
            new = parse_settings(form, await store.get_setting(SETTINGS_KEY))
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
                live = await live_broker.config()
                await live_broker.set_config(
                    {
                        "enabled": live["enabled"],
                        "all_symbols": live["all_symbols"],
                        "auto": live["auto"],
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
        return RedirectResponse(settings_url("rules"), status_code=303)

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

    @app.get("/api/snapshot", dependencies=[Depends(require_login)])
    async def api_snapshot(account: Literal["live", "sim"] = "live"):
        return JSONResponse(portfolios[account].snapshot or {"pending": True})

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
        return {"rows": await watchlist.rows()}

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
    async def api_portfolio(account: Literal["live", "sim"] = "live"):
        snap = portfolios[account].snapshot or {}
        rules = await preferences.rules()
        return portfolio_view.build(
            snap, portfolio.clock().date(), earnings.industries, rules.manage_dte
        )

    @app.get("/api/trade/{symbol:path}/chart", dependencies=[Depends(require_login)])
    async def api_chart(symbol: str, account: Literal["live", "sim"] = "live"):
        """A year of daily candles, with what the chart draws over them: the account's
        option strikes in this underlying, the next earnings report, the 52-week range."""
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

    @app.get("/api/trade/{symbol:path}/chain", dependencies=[Depends(require_login)])
    async def api_chain(symbol: str, expiration: date, strikes: int = 0):
        """strikes: how many either side of the money; 0 returns every strike."""
        symbol = _check_symbol(symbol)
        count = None if strikes <= 0 else max(4, min(strikes, 200))
        try:
            raw = await source.option_chain(symbol, expiration, expiration, strike_count=count)
        except SchwabError as e:
            raise _schwab_errors(e) from e
        out = chain_mod.chain_rows(raw or {})
        out.update({"symbol": symbol, "expiration": expiration.isoformat()})
        held = next(
            (u for u in portfolio.snapshot.get("underlyings", []) if u["symbol"] == symbol), None
        )
        out["ivr"] = (held or {}).get("ivr")
        if symbol == BENCH:
            out["ivr"] = (portfolio.snapshot.get("ivr") or {}).get("SPY")
        else:
            wl = next((r for r in await watchlist.rows() if r["symbol"] == symbol), None)
            out["ivr"] = out["ivr"] or (wl or {}).get("ivr")
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
            result = analyze(legs, body.spot, today, body.vol, held_shares=body.held_shares)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        snap = portfolio.snapshot or {}
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
        today = portfolio.clock().date()
        pf = portfolios[body.account]
        acct = (pf.snapshot or {}).get("account") or {}
        if body.account == "sim":
            held = await broker.held(body.trade_id)
        else:
            held = {}
            for leg in portfolio.legs:
                held[leg.symbol] = held.get(leg.symbol, 0) + int(leg.quantity)
        spot = None
        uq = (quotes.get(t.underlying) or {}).get("quote", {})
        for k in ("mark", "lastPrice", "closePrice"):
            if uq.get(k):
                spot = float(uq[k])
                break
        bp, defined, a = None, True, None
        if spot and t.intent in ("open", "adjust") and all(x.mark is not None for x in t.legs):
            tl = [TradeLeg(x.put_call, x.strike, x.expiration, x.quantity, x.mark) for x in t.legs]
            a = analyze(tl, spot, today)
            bp, defined = a["bp_estimate"], not a["unlimited_loss"]
        # Conditions at the time, kept with the trade for the dashboard's analysis.
        market = (portfolio.snapshot or {}).get("market") or {}
        ivr = None
        if t.underlying in ("SPY", "SPX", "XSP"):
            ivr = ((portfolio.snapshot or {}).get("ivr") or {}).get("SPY")
        else:
            row = next((r for r in await watchlist.rows() if r["symbol"] == t.underlying), None)
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
        )
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
        if body.account == "sim" and t.intent in ("close", "roll") and t.trade_id is not None:
            for o in await broker.replaceable(t.trade_id, {x.symbol for x in t.legs}):
                price = f"{abs(o['limit_net']):.2f} {'credit' if o['limit_net'] > 0 else 'debit'}"
                warnings.append(
                    f"Replaces working order #{o['id']} ({o['intent']} at {price}, "
                    f"{o['duration']}); it will be cancelled when this one is placed"
                )
        body_json = None
        try:
            body_json = schwab_order(t)
        except TicketError as e:
            errors.append(str(e))
        preview = None
        # Schwab can only preview against the real account: opening orders, or live closes.
        if body_json and (t.intent == "open" or body.account == "live"):
            if demo:
                preview = {"ok": True, "simulated": True, "fields": {}, "messages": [], "raw": None}
            else:
                try:
                    preview = summarize_preview(
                        await schwab.call("preview_order", await selector(), body_json)
                    )
                except Exception as e:
                    preview = {"ok": False, "error": str(e)[:300], "fields": {}, "messages": []}
        contracts = sum(abs(x.quantity) for x in t.legs)
        live_blocks = []
        if body.account == "live":
            live_blocks = await live_broker.gate(t, bp, nl)
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

    async def place_order(body: OrderIn) -> dict:
        if body.limit_net is None:
            raise HTTPException(status_code=400, detail="Set a limit price")
        t, p, quotes = await build_ticket(body)
        result = await assess(body, t, p, quotes)
        if result["errors"]:
            raise HTTPException(status_code=400, detail="; ".join(result["errors"]))
        if body.account == "live":
            if result["live_blocks"]:
                raise HTTPException(status_code=403, detail="; ".join(result["live_blocks"]))
            try:
                order = await live_broker.place(
                    t,
                    trade_id=body.trade_id,
                    profit_target=t.profit_target,
                    entry=result["entry"],
                )
            except SchwabError as e:
                raise _schwab_errors(e) from e
            portfolio.refresh_soon()
            chain_service.refresh_soon()
            return order
        try:
            order = await broker.place(t, quotes, entry=result["entry"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        sim_portfolio.refresh_soon()
        return order

    @app.post("/api/orders", dependencies=[Depends(require_login)])
    async def api_order_place(body: OrderIn):
        return await place_order(body)

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
        ),
    }
    app.state.automation = automations["sim"]

    async def apply_live_cap() -> None:
        automations["live"].max_level = live_broker.auto_cap(await live_broker.config())

    Acct = Literal["sim", "live"]

    @app.get("/api/automation", dependencies=[Depends(require_login)])
    async def api_automation(account: Acct = "sim"):
        return await automations[account].view()

    @app.post("/api/automation/trade", dependencies=[Depends(require_login)])
    async def api_automation_trade(body: AutoTradeIn, account: Acct = "sim"):
        await automations[account].set_trade(body.trade_id, body.enabled)
        return await automations[account].view()

    @app.post("/api/automation/pause", dependencies=[Depends(require_login)])
    async def api_automation_pause(body: PauseIn, account: Acct = "sim"):
        await automations[account].set_paused(body.paused)
        return await automations[account].view()

    @app.post("/api/automation/proposal/{pid}/dismiss", dependencies=[Depends(require_login)])
    async def api_automation_dismiss(pid: str, account: Acct = "sim"):
        await automations[account].dismiss(pid)
        return await automations[account].view()

    @app.post("/api/automation/proposal/{pid}/accept", dependencies=[Depends(require_login)])
    async def api_automation_accept(pid: str, account: Acct = "sim"):
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
        live_broker,
        clock=lambda: portfolio.clock(),
    )

    @app.get("/api/scanner", dependencies=[Depends(require_login)])
    async def api_scanner(account: Acct = "sim"):
        return await scanner.view(account)

    @app.post("/api/scanner/run", dependencies=[Depends(require_login)])
    async def api_scanner_run(account: Acct = "sim", act: bool = True):
        try:
            await scanner.scan(account, act=act)
        except SchwabError as e:
            raise _schwab_errors(e) from e
        return await scanner.view(account)

    @app.get("/api/live", dependencies=[Depends(require_login)])
    async def api_live():
        return {**await live_broker.status(), "audit": await live_broker.audit()}

    @app.post("/api/orders/{order_id}/cancel", dependencies=[Depends(require_login)])
    async def api_order_cancel(order_id: str, account: Acct = "sim"):
        if account == "live":
            try:
                order = await live_broker.cancel(order_id)
            except SchwabError as e:
                raise _schwab_errors(e) from e
            portfolio.refresh_soon()
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

    @app.get("/api/chains", dependencies=[Depends(require_login)])
    async def api_chains(account: Literal["live", "sim"] = "live"):
        if account == "sim":
            return {
                "chains": sim_chains(await broker.snapshot()),
                "proposals": [],
                "mismatches": [],
                "unmatched": [],
                "synced_at": None,
                "error": None,
            }
        return {
            **chain_service.result,
            "synced_at": chain_service.synced_at,
            "error": chain_service.error,
        }

    @app.get("/api/chains/{chain_id}", dependencies=[Depends(require_login)])
    async def api_trade_chain(chain_id: str, account: Literal["live", "sim"] = "live"):
        pool = (
            sim_chains(await broker.snapshot())
            if account == "sim"
            else chain_service.result["chains"]
        )
        found = next((c for c in pool if str(c["id"]) == chain_id), None)
        if found is None:
            raise HTTPException(status_code=404, detail="That trade's history is not available")
        return found

    @app.get("/api/order-check", dependencies=[Depends(require_login)])
    async def api_order_check():
        return await order_check.view()

    @app.post("/api/order-check/refresh", dependencies=[Depends(require_login)])
    async def api_order_check_refresh():
        try:
            await order_check.sync()
        except SchwabError as e:
            raise _schwab_errors(e) from e
        return await order_check.view()

    @app.post("/api/chains/link", dependencies=[Depends(require_login)])
    async def api_chain_link(body: LinkIn):
        try:
            await chain_service.link(body.group, body.chain)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        portfolio.refresh_soon()
        return {"ok": True}

    @app.post("/api/chains/split", dependencies=[Depends(require_login)])
    async def api_chain_split(body: SplitIn):
        await chain_service.split(body.group)
        portfolio.refresh_soon()
        return {"ok": True}

    @app.get("/api/activity", dependencies=[Depends(require_login)])
    async def api_activity(
        account: Literal["live", "sim"] = "live",
        source: Literal["all", "sheet", "schwab"] = "all",
        limit: int = 300,
    ):
        limit = max(1, min(limit, 20000))
        if account == "live":
            chs = chain_service.result["chains"]
            counts = {"all": len(chs)}
            for c in chs:
                counts[c.get("source") or "schwab"] = counts.get(c.get("source") or "schwab", 0) + 1
            shown = [c for c in chs if source == "all" or (c.get("source") or "schwab") == source]
            return {
                "account": "live",
                # Orders at Schwab from the last week, working first, as the live broker
                # last read them (every 10 seconds while one of the app's orders works).
                "orders": sorted(
                    live_broker.state["orders"],
                    key=lambda o: (o["status"] != "WORKING", -(o["created_at"] or 0)),
                )[:200],
                "live": await live_broker.status(),
                "trades": sorted(
                    shown,
                    # Open chains first, then the most recently closed.
                    key=lambda c: (c["status"] != "open", -(c["closed_at"] or c["opened_at"] or 0)),
                )[:limit],
                "trade_counts": counts,
                "trades_shown_of": len(shown),
                "sheet": {
                    "rows": chain_service.history.get("rows", 0),
                    "error": chain_service.history.get("error"),
                },
                "stats": stats(chs),
                "proposals": chain_service.result["proposals"],
                "mismatches": chain_service.result["mismatches"],
                "synced_at": chain_service.synced_at,
                "error": chain_service.error,
            }
        st = await broker.snapshot()
        acct = (sim_portfolio.snapshot or {}).get("account") or {}
        return {
            "account": "sim",
            "cash": st["cash"],
            "start_cash": st["start_cash"],
            "net_liq": acct.get("net_liq"),
            "orders": sorted(st["orders"], key=lambda o: -o["id"])[:300],
            "trades": sorted(sim_chains(st), key=lambda tr: -tr["id"])[:300],
            "stats": stats(st["trades"]),
        }

    # Trading history and dashboard ------------------------------------------------------

    prices_service = PriceHistoryService(
        source,
        store,
        chain_service,
        sim_trades=lambda: sim_chains(broker.state) if broker.state else [],
    )

    @app.get("/api/history", dependencies=[Depends(require_login)])
    async def api_history():
        h = chain_service.history
        return {
            "rows": h["rows"],
            "matched": h["matched"],
            "window_start": h["window_start"],
            "problems": h["problems"],
            "review": h["review"],
            "futures": len(h["futures"]),
            "prices": prices_service.status,
        }

    @app.post("/api/history/review", dependencies=[Depends(require_login)])
    async def api_history_review(body: ReviewIn):
        decisions = await store.get_setting(REVIEW_KEY) or {}
        if body.decision is None:
            decisions.pop(body.id, None)
        else:
            decisions[body.id] = body.decision
        await store.set_setting(REVIEW_KEY, decisions)
        await chain_service.rebuild()
        portfolio.refresh_soon()
        return {"ok": True}

    @app.get("/api/dashboard", dependencies=[Depends(require_login)])
    async def api_dashboard(
        account: Literal["live", "sim"] = "live",
        source: Literal["all", "sheet", "schwab"] = "all",
        futures: bool = False,
    ):
        if account == "sim":
            chs = sim_chains(await broker.snapshot())
            realized, fut, window = [], [], None
        else:
            h = chain_service.history
            chs = chain_service.result["chains"]
            realized, fut, window = h["realized"], h["futures"], h["window_start"]
        symbols = {dash.price_symbol(c["underlying"]) for c in chs} | {"$VIX"}
        prices = await store.prices(symbols)
        out = dash.build(
            chs,
            realized,
            window,
            fut,
            prices,
            source=source if account == "live" else "all",
            include_futures=futures,
        )
        out["prices_pending"] = prices_service.status.get("pending")
        out["account"] = account
        return out

    @app.get("/api/export/sheet", dependencies=[Depends(require_login)])
    async def api_export_sheet(start: date, end: date):
        rows = export_rows(chain_service.result["chains"], start, end, SHEET_ACCOUNT)
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
        await broker.reset(body.start_cash)
        sim_portfolio.refresh_soon()
        return {"ok": True}

    @app.get("/api/events", dependencies=[Depends(require_login)])
    async def api_events(request: Request, account: Literal["live", "sim"] = "live"):
        pf = portfolios[account]
        queue = pf.subscribe()

        async def stream():
            try:
                if pf.snapshot:
                    yield f"data: {json.dumps(pf.snapshot, default=str)}\n\n"
                while not await request.is_disconnected():
                    try:
                        snap = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(snap, default=str)}\n\n"
                    except TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                pf.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
