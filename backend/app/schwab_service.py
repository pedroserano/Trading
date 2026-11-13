"""The single owner of the Schwab token and client.

Schwab allows one valid sign-in per app and one streamer connection per user, so exactly
one object in one process holds the client. Everything else asks this service.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from authlib.integrations.httpx_client import OAuthError
from schwab import auth

from .token_store import TokenStore, TokenStoreError

log = logging.getLogger(__name__)

REFRESH_TOKEN_LIFETIME_S = 7 * 24 * 3600


class SchwabNotConnected(Exception):
    pass


class SchwabError(Exception):
    pass


class SchwabAuthError(SchwabError):
    """Schwab no longer accepts the token. Only a new sign-in fixes it."""


@dataclass
class ConnectionStatus:
    connected: bool
    token_age_s: float | None = None
    detail: str | None = None
    created_at: float | None = None

    @property
    def expires_in_s(self) -> float | None:
        if self.token_age_s is None:
            return None
        return REFRESH_TOKEN_LIFETIME_S - self.token_age_s

    @property
    def expires_in_days(self) -> float | None:
        s = self.expires_in_s
        return None if s is None else round(s / 86400, 1)


@dataclass
class AccountSummary:
    last4: str
    type: str | None
    net_liq: float | None
    cash: float | None
    buying_power: float | None
    option_buying_power: float | None
    positions: int


def summarize_account(raw: dict) -> AccountSummary:
    acct = raw.get("securitiesAccount", {})
    bal = acct.get("currentBalances", {})
    number = str(acct.get("accountNumber", ""))
    return AccountSummary(
        last4=number[-4:],
        type=acct.get("type"),
        net_liq=bal.get("liquidationValue"),
        cash=bal.get("cashBalance"),
        buying_power=bal.get("buyingPower"),
        option_buying_power=bal.get("buyingPowerNonMarginableTrade"),
        positions=len(acct.get("positions", [])),
    )


def callback_list(value: str) -> list[str]:
    """SCHWAB_CALLBACK_URL as a list: one address, or several separated by commas, written
    as in the Schwab developer portal."""
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def pick_callback(callbacks: list[str], host: str | None) -> str:
    """The callback to send Schwab for a sign-in started at this host (the address the app
    was opened at): the one on the same host name, else the first that returns to the app
    (/schwab/callback), else the first."""
    from urllib.parse import urlsplit

    name = (host or "").split(":")[0].lower()
    if name:
        for c in callbacks:
            if (urlsplit(c).hostname or "").lower() == name:
                return c
    return next((c for c in callbacks if c.rstrip("/").endswith("/schwab/callback")), callbacks[0])


class SchwabService:
    def __init__(self, app_key: str, app_secret: str, callback_url: str, store: TokenStore):
        self._app_key = app_key
        self._app_secret = app_secret
        self._callbacks = callback_list(callback_url) or ["https://127.0.0.1:8182"]
        self._callback_url = self._callbacks[0]
        self._store = store
        self._client = None
        self._auth_context = None
        self._load_error: str | None = None
        self._auth_failure: str | None = None

    def load(self) -> None:
        """Build a client from the stored token, if there is one."""
        if not self._store.exists():
            return
        try:
            self._client = auth.client_from_access_functions(
                self._app_key,
                self._app_secret,
                token_read_func=self._store.read,
                token_write_func=self._store.write,
                asyncio=True,
            )
            self._load_error = None
        except TokenStoreError as e:
            self._load_error = str(e)
            log.warning("Could not load Schwab token: %s", e)

    def status(self) -> ConnectionStatus:
        if self._client is None:
            return ConnectionStatus(connected=False, detail=self._load_error)
        age = self._client.token_age()
        created = round(time.time() - age)
        if age >= REFRESH_TOKEN_LIFETIME_S:
            return ConnectionStatus(
                connected=False,
                token_age_s=age,
                created_at=created,
                detail="Schwab sign-in has expired",
            )
        if self._auth_failure:
            return ConnectionStatus(
                connected=False, token_age_s=age, created_at=created, detail=self._auth_failure
            )
        return ConnectionStatus(connected=True, token_age_s=age, created_at=created)

    def authorization_url(self, host: str | None = None) -> str:
        """Start a sign-in. Returns the Schwab URL to open in a browser. host: the address
        the app was opened at, to have Schwab return there (see pick_callback)."""
        chosen = pick_callback(self._callbacks, host) if host else self._callback_url
        self._auth_context = auth.get_auth_context(self._app_key, chosen)
        return self._auth_context.authorization_url

    async def complete_authorization(self, received_url: str) -> None:
        """Finish a sign-in from the URL Schwab redirected the browser to."""
        if self._auth_context is None:
            raise SchwabError("No sign-in in progress. Start again from the Schwab link.")
        received_url = received_url.strip()
        expected = self._auth_context.callback_url
        if not received_url.startswith(expected):
            raise SchwabError(
                f"That address does not start with the callback URL ({expected}). "
                "Copy the whole address from the browser bar after Schwab redirects."
            )
        ctx = self._auth_context
        try:
            # The token exchange is a blocking HTTP call.
            client = await asyncio.to_thread(
                auth.client_from_received_url,
                self._app_key,
                self._app_secret,
                ctx,
                received_url,
                self._store.write,
                asyncio=True,
            )
        except Exception as e:
            # Authorization codes are single-use and short-lived; any failure means start over.
            self._auth_context = None
            raise SchwabError(f"Schwab rejected the sign-in: {type(e).__name__}: {e}") from e
        old, self._client = self._client, client
        self._auth_context = None
        self._load_error = None
        self._auth_failure = None
        if old is not None:
            await old.close_async_session()
        log.info("Schwab sign-in completed")

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.close_async_session()
        self._client = None
        self._store.delete()

    async def _request(self, method: str, *args, **kwargs):
        """Calls a schwab-py client method and returns the checked HTTP response."""
        if not self.status().connected:
            raise SchwabNotConnected()
        started = time.monotonic()
        try:
            resp = await getattr(self._client, method)(*args, **kwargs)
        except OAuthError as e:
            # Raised when the access token refresh is refused, e.g. after a password change.
            self._auth_failure = f"Schwab refused to renew the sign-in ({e}). Sign in again."
            raise SchwabAuthError(self._auth_failure) from e
        except Exception as e:
            raise SchwabError(f"Schwab request failed: {type(e).__name__}: {e}") from e
        log.debug("%s -> %s in %.2fs", method, resp.status_code, time.monotonic() - started)
        if resp.status_code == 401:
            self._auth_failure = "Schwab rejected the token (401). Sign in to Schwab again."
            raise SchwabAuthError(self._auth_failure)
        if resp.status_code == 429:
            raise SchwabError("Schwab rate limit reached (429). Retrying shortly.")
        if resp.status_code >= 300:
            raise SchwabError(f"Schwab returned HTTP {resp.status_code}: {resp.text[:300]}")
        return resp

    async def call(self, method: str, *args, **kwargs):
        """Calls a schwab-py client method and returns the decoded JSON body."""
        resp = await self._request(method, *args, **kwargs)
        return resp.json() if resp.content else None

    async def place_order(self, account_hash: str, body: dict) -> str:
        """Places an order. Returns Schwab's order id, from the Location header."""
        resp = await self._request("place_order", account_hash, body)
        return _order_id(resp)

    async def replace_order(self, account_hash: str, order_id: str, body: dict) -> str:
        """Replaces a working order with a new one. Returns the new order's id."""
        resp = await self._request("replace_order", account_hash, int(order_id), body)
        return _order_id(resp)

    async def cancel_order(self, account_hash: str, order_id: str) -> None:
        await self._request("cancel_order", int(order_id), account_hash)

    def client(self):
        """The underlying schwab-py client, for enum access. None when not signed in."""
        return self._client

    async def accounts(self) -> list[AccountSummary]:
        raw = await self.call("get_accounts", fields=[self._client.Account.Fields.POSITIONS])
        return [summarize_account(a) for a in raw]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close_async_session()


def _order_id(resp) -> str:
    """Schwab answers a placed order with 201 and the new order's address in Location."""
    location = resp.headers.get("Location") or resp.headers.get("location") or ""
    order_id = location.rstrip("/").rsplit("/", 1)[-1]
    if not order_id.isdigit():
        raise SchwabError(
            f"Schwab accepted the order but did not return its id (HTTP {resp.status_code})"
        )
    return order_id
