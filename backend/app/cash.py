"""Cash for buying shares: cash and the sweep vehicle only, never margin.

A short put the trader would be glad to own is left to be assigned only when the account can
pay for the shares outright. Borrowing at margin rates to do it is never worth it.
"""

FEE_BUFFER = 25.0


def free_cash(acct: dict | None) -> float:
    """Cash plus the sweep balance, each only when positive. Negative cash, or a margin loan,
    means the account is already borrowing: nothing is free then. Buying power, available
    funds, SMA and the value of other holdings never count."""
    if not acct:
        return 0.0
    cash = acct.get("cash") or 0.0
    loan = acct.get("margin_balance") or 0.0
    if cash < 0 or loan > 0:
        return 0.0
    return round(max(cash, 0.0) + max(acct.get("sweep") or 0.0, 0.0), 2)


def put_cost(st: dict) -> float:
    """What assignment of the trade's short puts would cost: strike x 100 x contracts."""
    return sum(
        x["strike"] * 100 * -x["quantity"]
        for x in st["legs"]
        if x["kind"] == "OPTION" and x["put_call"] == "P" and x["quantity"] < 0
    )


def allot(free: float, needs: list[tuple[str, float]]) -> tuple[set[str], dict[str, float]]:
    """Shares out the free cash, less a fee buffer, among puts that would be assigned, the
    largest first, so no two count the same dollars. Returns the ids that are covered, and
    for every id the cash left for it when its turn came."""
    left = free - FEE_BUFFER
    covered, avail = set(), {}
    for tid, need in sorted(needs, key=lambda x: -x[1]):
        avail[tid] = max(left, 0.0)
        if need <= left + 1e-9:
            covered.add(tid)
            left -= need
    return covered, avail
