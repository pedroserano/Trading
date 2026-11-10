import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.conninfo import make_conninfo
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .schwab_service import SchwabError, SchwabNotConnected, SchwabService
from .token_store import TokenStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

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


async def check_db(settings: Settings) -> str | None:
    """Returns None when the database answers, otherwise a short error."""
    conninfo = make_conninfo(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        connect_timeout=3,
    )
    try:
        async with await psycopg.AsyncConnection.connect(conninfo) as conn:
            await conn.execute("SELECT 1")
        return None
    except Exception as e:
        return f"{type(e).__name__}: {str(e).strip()[:200]}"


def create_app(settings: Settings | None = None, schwab: SchwabService | None = None) -> FastAPI:
    settings = settings or get_settings()
    if schwab is None:
        store = TokenStore(settings.data_dir / "schwab_token.enc", settings.secret_key)
        schwab = SchwabService(
            settings.schwab_app_key,
            settings.schwab_app_secret,
            settings.schwab_callback_url,
            store,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        schwab.load()
        log.info("Started. Schwab connected: %s", schwab.status().connected)
        yield
        await schwab.close()

    app = FastAPI(title="Trading", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=SESSION_MAX_AGE_S,
        same_site="strict",
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

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def home(request: Request):
        status = schwab.status()
        accounts, error = [], request.session.pop("flash", None)
        if status.connected:
            try:
                accounts = await schwab.accounts()
            except SchwabError as e:
                error = str(e)
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "status": status,
                "accounts": accounts,
                "error": error,
                "db_error": await check_db(settings),
                "callback_url": settings.schwab_callback_url,
            },
        )

    @app.get("/schwab/authorize", dependencies=[Depends(require_login)])
    async def schwab_authorize():
        return RedirectResponse(schwab.authorization_url(), status_code=303)

    @app.post("/schwab/complete", dependencies=[Depends(require_login)])
    async def schwab_complete(request: Request, received_url: str = Form()):
        try:
            await schwab.complete_authorization(received_url)
        except SchwabError as e:
            request.session["flash"] = str(e)
        return RedirectResponse("/", status_code=303)

    @app.post("/schwab/disconnect", dependencies=[Depends(require_login)])
    async def schwab_disconnect():
        await schwab.disconnect()
        return RedirectResponse("/", status_code=303)

    @app.get("/api/status", dependencies=[Depends(require_login)])
    async def api_status():
        status = schwab.status()
        return {
            "schwab": {
                "connected": status.connected,
                "expires_in_days": status.expires_in_days,
                "detail": status.detail,
            },
            "database": {"ok": (db_error := await check_db(settings)) is None, "error": db_error},
        }

    @app.get("/api/accounts", dependencies=[Depends(require_login)])
    async def api_accounts():
        try:
            return await schwab.accounts()
        except SchwabNotConnected:
            raise HTTPException(status_code=409, detail="Schwab is not connected") from None
        except SchwabError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    return app
