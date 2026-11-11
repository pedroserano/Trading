import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .accounts import AccountSelector
from .config import Settings, get_settings
from .datasource import DemoDataSource, SchwabDataSource
from .jobs import DailyJobs
from .notify import PushoverNotifier, ReminderService
from .portfolio import PortfolioService
from .schwab_service import (
    ConnectionStatus,
    SchwabError,
    SchwabNotConnected,
    SchwabService,
)
from .store import PgStore
from .token_store import TokenStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC_DIR = Path(__file__).parent / "static"

SESSION_MAX_AGE_S = 12 * 3600
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_S = 60


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

    portfolio = PortfolioService(
        source, store, settings, connection_json, selector, mode="demo" if demo else "live"
    )
    jobs = DailyJobs(source, store, portfolio, settings)
    reminders = ReminderService(notifier, store, connection_status, settings, demo)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.open()
        schwab.load()
        log.info("Started. Demo: %s. Schwab connected: %s", demo, schwab.status().connected)
        tasks = []
        if start_background:
            tasks = [
                asyncio.create_task(portfolio.run(), name="portfolio"),
                asyncio.create_task(jobs.run(), name="jobs"),
                asyncio.create_task(reminders.run(), name="reminders"),
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

    @app.get("/connect", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def connect_page(request: Request):
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
            "connect.html",
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
        return RedirectResponse("/connect", status_code=303)

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
        return RedirectResponse("/connect", status_code=303)

    @app.post("/account/select", dependencies=[Depends(require_login)])
    async def account_select(request: Request, number: str = Form()):
        try:
            await selector.select(number)
            portfolio.refresh_soon()
            request.session["notice"] = f"Using the account ending {number[-4:]}."
        except (ValueError, SchwabError) as e:
            request.session["flash"] = str(e)
        return RedirectResponse("/connect", status_code=303)

    @app.post("/notify/test", dependencies=[Depends(require_login)])
    async def notify_test(request: Request):
        try:
            await notifier.send(
                "Test", "Notifications from the trading app work.", settings.app_url
            )
            request.session["notice"] = "Test notification sent."
        except Exception as e:
            request.session["flash"] = f"Test notification failed: {e}"
        return RedirectResponse("/connect", status_code=303)

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
    async def api_snapshot():
        return JSONResponse(portfolio.snapshot or {"pending": True})

    @app.get("/api/events", dependencies=[Depends(require_login)])
    async def api_events(request: Request):
        queue = portfolio.subscribe()

        async def stream():
            try:
                if portfolio.snapshot:
                    yield f"data: {json.dumps(portfolio.snapshot, default=str)}\n\n"
                while not await request.is_disconnected():
                    try:
                        snap = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(snap, default=str)}\n\n"
                    except TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                portfolio.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
