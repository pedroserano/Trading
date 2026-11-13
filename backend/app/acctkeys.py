"""Keys for data kept per Schwab account.

The Schwab account the app used before it handled several keeps the key "live", so its stored
data keeps its old names; every other account's data is stored under "<name>:<account>".
"""

PRIMARY = "live"


def key_for(base: str, account: str) -> str:
    return base if account == PRIMARY else f"{base}:{account}"


def is_schwab(account: str) -> bool:
    return account != "sim"
