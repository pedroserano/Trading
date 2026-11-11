"""Notifications and the weekly Schwab sign-in reminder."""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class PushoverNotifier:
    URL = "https://api.pushover.net/1/messages.json"

    def __init__(self, user_key: str, app_token: str):
        self.user_key = user_key
        self.app_token = app_token

    @property
    def enabled(self) -> bool:
        return bool(self.user_key and self.app_token)

    async def send(self, title: str, message: str, url: str = "", priority: int = 0) -> None:
        if not self.enabled:
            raise RuntimeError("Pushover is not configured")
        data = {
            "token": self.app_token,
            "user": self.user_key,
            "title": title,
            "message": message,
            "priority": priority,
        }
        if url:
            data["url"] = url
            data["url_title"] = "Open trading app"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(self.URL, data=data)
        if resp.status_code != 200:
            raise RuntimeError(f"Pushover returned HTTP {resp.status_code}: {resp.text[:200]}")


def due_reminders(
    now: datetime,
    status,
    weekday: int,
    hour: int,
) -> list[tuple[str, str, int]]:
    """Reminders due now as (dedupe key, message, priority). Pure, for testing.

    Messages carry no account numbers or balances: they pass through Pushover.
    """
    out = []
    created = status.created_at
    if created is None:
        return out
    et = now.astimezone(ET)
    left_h = None if status.expires_in_s is None else status.expires_in_s / 3600
    if not status.connected:
        out.append(
            (
                f"lost:{created}",
                "Schwab sign-in has expired or was refused. Sign in again to restore live data.",
                1,
            )
        )
        return out
    if left_h is not None and left_h <= 24:
        out.append(
            (
                f"exp24:{created}",
                f"Schwab sign-in expires in {left_h:.0f} hours. Sign in again now.",
                1,
            )
        )
    elif left_h is not None and left_h <= 48:
        out.append((f"exp48:{created}", f"Schwab sign-in expires in {left_h:.0f} hours.", 0))
    if et.weekday() == weekday and et.hour >= hour and (status.token_age_s or 0) > 86400:
        week = et.strftime("%G-W%V")
        out.append(
            (
                f"weekly:{week}",
                f"Weekly reminder: renew the Schwab sign-in ({left_h / 24:.1f} days left).",
                0,
            )
        )
    return out


class ReminderService:
    def __init__(self, notifier, store, connection_status, settings, demo: bool):
        self.notifier = notifier
        self.store = store
        self.connection_status = connection_status
        self.settings = settings
        self.demo = demo
        self.last_error: str | None = None

    async def run(self) -> None:
        if self.demo:
            return
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("Reminder check failed: %s", self.last_error)
            await asyncio.sleep(300)

    async def check(self, now: datetime | None = None) -> list[str]:
        if not self.notifier.enabled:
            return []
        now = now or datetime.now(ET)
        sent = []
        due = due_reminders(
            now,
            self.connection_status(),
            self.settings.reminder_weekday,
            self.settings.reminder_hour,
        )
        for key, message, priority in due:
            if await self.store.mark_notified(key):
                await self.notifier.send("Schwab sign-in", message, self.settings.app_url, priority)
                sent.append(key)
                log.info("Sent reminder %s", key)
        self.last_error = None
        return sent
