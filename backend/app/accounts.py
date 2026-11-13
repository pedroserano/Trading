"""The Schwab accounts the app uses: every account ticked at the Schwab sign-in.

Each account has a key the app files its data under. The account the app used before it
handled several keeps "live", so nothing it stored is renamed; every other account is
"live-<last 4 digits>". Accounts are shown by their name in Schwab (the nickname set there).

Schwab's API addresses accounts by a hash, looked up again after each new sign-in.
"""

import logging

from .portfolio import NeedsSetup

log = logging.getLogger(__name__)
REGISTRY_KEY = "schwab_accounts"
PRIMARY = "live"


class AccountRegistry:
    def __init__(self, store, source, connection_status):
        self.store = store
        self.source = source
        self.connection_status = connection_status
        # [{key, number, last4, name, gone}], in the order Schwab lists them.
        self.accounts: list[dict] = []
        self._hashes: dict[str, str] = {}  # account number -> hash
        self._created: float | None = None
        self.visible_count: int | None = None
        self.loaded = False
        # The Account labels in the imported history sheet ("Joint", "Individual"), set by
        # the app from the stored rows.
        self.sheet_labels: list[str] = []

    # Stored list --------------------------------------------------------------------------

    async def load(self) -> None:
        self.accounts = list(await self.store.get_setting(REGISTRY_KEY) or [])
        if not self.accounts:
            # The single account chosen before several were supported becomes "live".
            sel = await self.store.get_setting("account")
            if sel and sel.get("number"):
                self.accounts = [_entry(PRIMARY, sel["number"])]
        self.loaded = True

    async def _save(self) -> None:
        await self.store.set_setting(REGISTRY_KEY, self.accounts)

    def _new_key(self, number: str) -> str:
        taken = {a["key"] for a in self.accounts}
        if PRIMARY not in taken:
            return PRIMARY
        for n in (4, 6, len(number)):
            key = f"live-{number[-n:]}"
            if key not in taken:
                return key
        return f"live-{number}-{len(self.accounts)}"

    async def refresh(self) -> tuple[list[str], list[str]]:
        """Reads which accounts the sign-in can see, and their names. Returns the keys that
        came into use and the keys that went out of use."""
        if not self.loaded:
            await self.load()
        nums = await self.source.account_numbers() or []
        self.visible_count = len(nums)
        self._hashes = {str(n["accountNumber"]): n["hashValue"] for n in nums}
        self._created = self.connection_status().created_at
        try:
            names = await self.source.account_names()
        except Exception as e:  # names are a nicety; the numbers are what matter
            log.warning("Could not read the Schwab account names: %s", e)
            names = {}
        known = {a["number"]: a for a in self.accounts}
        added, gone = [], []
        for number in self._hashes:
            a = known.get(number)
            if a is None:
                a = _entry(self._new_key(number), number)
                self.accounts.append(a)
                added.append(a["key"])
            elif a.get("gone"):
                a["gone"] = False
                added.append(a["key"])
            if names.get(number):
                a["name"] = names[number]
        for a in self.accounts:
            if a["number"] not in self._hashes and not a.get("gone"):
                a["gone"] = True
                gone.append(a["key"])
        await self._save()
        return added, gone

    # Reading ------------------------------------------------------------------------------

    def in_use(self) -> list[dict]:
        """The accounts the app shows and runs: {key, name, last4}."""
        live = [a for a in self.accounts if not a.get("gone")]
        return [{"key": a["key"], "name": self.name(a["key"]), "last4": a["last4"]} for a in live]

    def keys(self) -> list[str]:
        return [a["key"] for a in self.accounts if not a.get("gone")]

    def entry(self, key: str) -> dict | None:
        return next((a for a in self.accounts if a["key"] == key), None)

    def name(self, key: str) -> str:
        """The account's name in Schwab; without one, "Account …1234". Two accounts with the
        same name are told apart by their last 4 digits."""
        a = self.entry(key)
        if a is None:
            return "Schwab"
        base = a.get("name") or f"Account …{a['last4']}"
        same = [x for x in self.accounts if not x.get("gone") and (x.get("name") or "") == base]
        return f"{base} …{a['last4']}" if a.get("name") and len(same) > 1 else base

    def sheet_label(self, key: str) -> str | None:
        """Which of the history sheet's Account labels is this account's: the one chosen on
        Settings, else "Joint" for the first account, else the label equal to its Schwab
        name (ignoring case), else none."""
        a = self.entry(key) or {}
        if "sheet_label" in a:
            return a["sheet_label"] or None
        if key == PRIMARY:
            return "Joint"
        name = (a.get("name") or "").strip().lower()
        return next((x for x in self.sheet_labels if x.lower() == name), None) if name else None

    async def set_sheet_label(self, key: str, label: str | None) -> None:
        a = self.entry(key)
        if a is None:
            raise ValueError("No such account")
        a["sheet_label"] = label or ""
        await self._save()

    def resolver(self, key: str):
        """An async callable giving the account's hash, for the services that read it."""

        async def resolve() -> str:
            if not self.loaded:
                await self.load()
            created = self.connection_status().created_at
            if not self._hashes or self._created != created:
                await self.refresh()
            a = self.entry(key)
            if a is None:
                raise NeedsSetup(
                    "select_account",
                    "No Schwab account yet: sign in and tick the accounts to use.",
                )
            h = self._hashes.get(a["number"])
            if h is None:
                raise NeedsSetup(
                    "select_account",
                    f"{self.name(key)} is not visible to the current Schwab sign-in. Sign in "
                    "again and tick it on Schwab's consent screen.",
                )
            return h

        return resolve


def _entry(key: str, number: str) -> dict:
    return {"key": key, "number": str(number), "last4": str(number)[-4:], "name": "", "gone": False}
