"""The single owner of the Schwab token and client.

Schwab allows one valid sign-in per app and one streamer connection per user, so exactly
one object in one process holds the client. Everything else asks this service.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from schwab import auth

from .token_store import TokenStore, TokenStoreError

log = logging.getLogger(__name__)

REFRESH_TOKEN_LIFETIME_S = 7 * 24 * 3600


class SchwabNotConnected(Exception):
    pass


class SchwabError(Exception):
    pass


@dataclass
class ConnectionStatus:
    connected: bool
    token_age_s: float | None = None
    detail: str | None = None

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


class SchwabService:
    def __init__(self, app_key: str, app_secret: str, callback_url: str, store: TokenStore):
        self._app_key = app_key
        self._app_secret = app_secret
        self._callback_url = callback_url
        self._store = store
        self._client = None
        self._auth_context = None
        self._load_error: str | None = None

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
        if age >= REFRESH_TOKEN_LIFETIME_S:
            return ConnectionStatus(
                connected=False, token_age_s=age, detail="Schwab sign-in has expired"
            )
        return ConnectionStatus(connected=True, token_age_s=age)

    def authorization_url(self) -> str:
        """Start a sign-in. Returns the Schwab URL to open in a browser."""
        self._auth_context = auth.get_auth_context(self._app_key, self._callback_url)
        return self._auth_context.authorization_url

    async def complete_authorization(self, received_url: str) -> None:
        """Finish a sign-in from the URL Schwab redirected the browser to."""
        if self._auth_context is None:
            raise SchwabError("No sign-in in progress. Start again from the Schwab link.")
        received_url = received_url.strip()
        if not received_url.startswith(self._callback_url):
            raise SchwabError(
                f"That address does not start with the callback URL ({self._callback_url}). "
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
        if old is not None:
            await old.close_async_session()
        log.info("Schwab sign-in completed")

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.close_async_session()
        self._client = None
        self._store.delete()

    async def accounts(self) -> list[AccountSummary]:
        if not self.status().connected:
            raise SchwabNotConnected()
        started = time.monotonic()
        try:
            resp = await self._client.get_accounts(fields=[self._client.Account.Fields.POSITIONS])
        except Exception as e:
            raise SchwabError(f"Schwab request failed: {type(e).__name__}: {e}") from e
        log.info("GET accounts -> %s in %.2fs", resp.status_code, time.monotonic() - started)
        if resp.status_code == 401:
            raise SchwabError("Schwab rejected the token (401). Sign in to Schwab again.")
        if resp.status_code != 200:
            raise SchwabError(f"Schwab returned HTTP {resp.status_code}: {resp.text[:300]}")
        return [summarize_account(a) for a in resp.json()]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close_async_session()
