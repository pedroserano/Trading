"""Which Schwab account the app uses.

The choice is stored by account number. Schwab's API addresses accounts by a hash, which is
looked up again after each new sign-in.
"""

from .portfolio import NeedsSetup


class AccountSelector:
    def __init__(self, store, source, connection_status, demo_hash: str | None = None):
        self.store = store
        self.source = source
        self.connection_status = connection_status
        self.demo_hash = demo_hash
        self._cache: tuple[float | None, str] | None = None
        self.visible_count: int | None = None

    async def selected(self) -> dict | None:
        if self.demo_hash:
            return {"number": "00001234", "last4": "1234"}
        return await self.store.get_setting("account")

    async def visible(self) -> list[dict]:
        nums = await self.source.account_numbers()
        self.visible_count = len(nums)
        return [{"number": n["accountNumber"], "last4": str(n["accountNumber"])[-4:]} for n in nums]

    async def select(self, number: str) -> None:
        nums = await self.source.account_numbers()
        if number not in {n["accountNumber"] for n in nums}:
            raise ValueError("That account is not visible to the current Schwab sign-in.")
        await self.store.set_setting("account", {"number": number, "last4": number[-4:]})
        self._cache = None

    async def __call__(self) -> str:
        if self.demo_hash:
            self.visible_count = 1
            return self.demo_hash
        sel = await self.store.get_setting("account")
        if not sel:
            raise NeedsSetup("select_account", "Choose the account this app should use.")
        created = self.connection_status().created_at
        if self._cache and self._cache[0] == created:
            return self._cache[1]
        nums = await self.source.account_numbers()
        self.visible_count = len(nums)
        by_number = {n["accountNumber"]: n["hashValue"] for n in nums}
        if sel["number"] not in by_number:
            raise NeedsSetup(
                "select_account",
                f"The selected account ending {sel['last4']} is not visible to the current "
                "Schwab sign-in. Sign in again and tick it, or choose another account.",
            )
        self._cache = (created, by_number[sel["number"]])
        return self._cache[1]
