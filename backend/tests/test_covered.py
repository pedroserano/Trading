"""Covered calls adjust the shares' cost basis: a credit lowers it as the call is sold, a
buyback or roll for a debit raises it again."""

from test_phase3 import client  # noqa: F401  (fixture)

from app.chains import covered_premium, shares_arrived

CALL = "AAPL  261218C00250000"
CALL2 = "AAPL  270115C00255000"
PUT = "AAPL  261218P00200000"


def ev(legs, fee=0.0, kind="open"):
    return {
        "kind": kind,
        "commission": fee,
        "legs": [{"symbol": s, "quantity": q, "price": p} for s, q, p in legs],
    }


def chain(cid, strategy, opened, events, status="open"):
    return {
        "id": cid,
        "underlying": "AAPL",
        "strategy": strategy,
        "opened_at": opened,
        "status": status,
        "closed_at": None,
        "events": events,
    }


def test_credits_count_when_sold_and_debits_raise_the_basis_again():
    chains = [
        # Sold for 3.00, then rolled out for a 0.50 debit: 250 net, less fees.
        chain(
            "a",
            "Short call",
            200,
            [
                ev([(CALL, -1, 3.00)], fee=0.65),
                ev([(CALL, 1, 4.00), (CALL2, -1, 3.50)], fee=1.30, kind="roll"),
            ],
        ),
    ]
    out = covered_premium(chains, "AAPL", "long", since=100)
    assert out["premium"] == round(300 - 50 - 1.95, 2)
    assert [c["id"] for c in out["chains"]] == ["a"]


def test_a_buy_write_counts_only_its_option_leg():
    chains = [
        chain(
            "b",
            "Covered call",
            200,
            [
                ev([("AAPL", 100, 240.0), (CALL, -1, 5.0)], fee=1.0),
            ],
        )
    ]
    out = covered_premium(chains, "AAPL", "long", since=100)
    # 500 credit, and the option's share of the fee (1 of 101 units).
    assert out["premium"] == round(500 - 1.0 / 101, 2)


def test_only_calls_written_while_holding_and_never_spreads_or_zebras():
    chains = [
        chain("old", "Short call", 50, [ev([(CALL, -1, 2.0)])]),  # before these shares
        chain("zeb", "Call ZEBRA", 200, [ev([(CALL, 2, 9.0), (CALL2, -1, 6.0)])]),
        chain("vert", "Call vertical", 200, [ev([(CALL, -1, 3.0), (CALL2, 1, 1.0)])]),
        chain("put", "Short put", 200, [ev([(PUT, -1, 2.0)])]),
        chain("ok", "Short call", 150, [ev([(CALL, -1, 1.5)])]),
        {**chain("other", "Short call", 200, [ev([(CALL, -1, 9.0)])]), "underlying": "MSFT"},
    ]
    out = covered_premium(chains, "AAPL", "long", since=100)
    assert [c["id"] for c in out["chains"]] == ["ok"] and out["premium"] == 150.0
    # Short shares: covered puts instead.
    short = covered_premium(chains, "AAPL", "short", since=100)
    assert [c["id"] for c in short["chains"]] == ["put"] and short["premium"] == 200.0


def at(e, t):
    return {**e, "at": t}


def test_shares_assigned_from_a_put_count_calls_sold_after_not_the_put():
    # The wheel: a put sold at 100, assigned at 200 (the shares join the put's chain), then
    # a call sold at 300 in its own chain and another linked into the shares' chain.
    put_chain = chain(
        "p",
        "Short put",
        100,
        [
            at(ev([(PUT, -1, 2.0)]), 100),
            at(ev([(PUT, 1, 0.0), ("AAPL", 100, 200.0)], kind="assign"), 200),
            at(ev([(CALL2, -1, 1.5)], kind="adjust"), 400),
        ],
    )
    call_chain = chain("c", "Short call", 300, [at(ev([(CALL, -1, 3.0)]), 300)])
    chains = [put_chain, call_chain]
    since = shares_arrived(chains, ["p"], "long")
    assert since == 200  # when the shares came in, not when the put was sold
    out = covered_premium(chains, "AAPL", "long", since, ["p"])
    assert out["premium"] == 300.0 + 150.0  # both calls; the put premium is not counted
    labels = {c["id"]: c["strategy"] for c in out["chains"]}
    assert labels == {"p": "Calls in this trade", "c": "Short call"}


def test_a_carried_in_covered_call_and_calls_sold_in_one_order_count():
    carried = chain(
        "cc",
        "Covered call",
        50,
        [
            at(ev([("AAPL", 100, 200.0), (CALL, -1, 4.0)]), 50),
        ],
    )
    custom = chain(
        "x",
        "Custom",
        300,
        [
            at(ev([(CALL, -1, 2.0), (CALL2, -1, 1.0)]), 300),
        ],
    )
    chains = [carried, custom]
    since = shares_arrived(chains, ["cc"], "long")
    out = covered_premium(chains, "AAPL", "long", since, ["cc"])
    assert out["premium"] == 400.0 + 300.0


def test_a_chain_that_bought_calls_to_open_is_not_a_covered_call():
    # Named "Short call" when it opened, but later turned into a spread by buying calls.
    c = chain(
        "s",
        "Short call",
        300,
        [
            at(ev([(CALL, -1, 3.0)]), 300),
            at(ev([(CALL2, 2, 1.0)], kind="adjust"), 320),
        ],
    )
    assert covered_premium([c], "AAPL", "long", 100)["chains"] == []


def shares_row(tid, qty, avg):
    return {
        "trade_id": tid,
        "legs": [{"kind": "EQUITY", "symbol": "AAPL", "quantity": qty, "avg_price": avg}],
        "numbers": {},
    }


def test_snapshot_and_history_show_the_adjusted_basis(client):  # noqa: F811
    pf = client.app.state.accounts["portfolios"]["sim"]
    chains = [
        chain("c1", "Short call", 200, [ev([(CALL, -1, 3.0)])]),
        chain("c2", "Short call", 300, [ev([(CALL2, -2, 2.0)])], status="closed"),
    ]
    pf.trade_info = lambda: {"s1": {"opened_at": 150}, "s2": {"opened_at": 250}}
    pf.chain_list = lambda: chains
    # Two purchases: 100 at 240, 100 at 250. Calls since the first: 300 + 400 = 700.
    rows = [shares_row("s1", 100, 240.0), shares_row("s2", 100, 250.0)]
    pf._adjust_basis("AAPL", rows)
    for r in rows:
        n = r["numbers"]
        assert n["covered_premium"] == 700.0 and n["covered_count"] == 2
        assert n["adjusted_basis"] == 245.0 - 3.5 and n["covered_since"] == 150

    pf.snapshot = {"underlyings": [{"symbol": "AAPL", "strategies": rows}]}
    r = client.get("/api/covered?account=sim&symbol=AAPL").json()
    assert r["shares"] == 200 and r["avg_price"] == 245.0
    assert r["premium"] == 700.0 and r["per_share"] == 3.5 and r["adjusted"] == 241.5
    assert [c["id"] for c in r["chains"]] == ["c1", "c2"]
    assert client.get("/api/covered?account=sim&symbol=QQQ").status_code == 404


def test_short_shares_add_covered_put_premium(client):  # noqa: F811
    pf = client.app.state.accounts["portfolios"]["sim"]
    pf.trade_info = lambda: {"s1": {"opened_at": 150}}
    pf.chain_list = lambda: [chain("p", "Short put", 200, [ev([(PUT, -1, 2.0)])])]
    rows = [shares_row("s1", -100, 240.0)]
    pf._adjust_basis("AAPL", rows)
    assert rows[0]["numbers"]["adjusted_basis"] == 242.0  # sold at 240, plus 2 a share


def test_a_covered_call_rolled_into_a_zebra_counts_up_to_the_roll():
    # Sold a call for 3.00, bought it back for 1.00 in the order that opened a ZEBRA (long 2,
    # short 1). The ZEBRA's own legs are not covered calls; the 200 netted before it is.
    zeb_long, zeb_short = "AAPL  281215C00200000", "AAPL  281215C00260000"
    c = chain(
        "z",
        "Call ZEBRA",
        300,
        [
            at(ev([(CALL, -1, 3.0)]), 300),
            at(ev([(CALL, 1, 1.0), (zeb_long, 2, 60.0), (zeb_short, -1, 20.0)], kind="roll"), 400),
            at(ev([(zeb_short, 1, 10.0)], kind="adjust"), 500),
        ],
    )
    out = covered_premium([c], "AAPL", "long", 100)
    assert out["premium"] == 200.0 and [x["id"] for x in out["chains"]] == ["z"]


def test_calls_sold_after_a_spread_closes_count_again():
    c = chain(
        "v",
        "Call vertical",
        300,
        [
            at(ev([(CALL, -1, 3.0), (CALL2, 1, 1.0)]), 300),
            at(ev([(CALL, 1, 0.5), (CALL2, -1, 0.1)], kind="close"), 400),
            at(ev([(CALL, -1, 2.0)], kind="adjust"), 500),
        ],
    )
    assert covered_premium([c], "AAPL", "long", 100)["premium"] == 200.0


def test_shares_no_trade_accounts_for_count_calls_from_the_history_start(client):  # noqa: F811
    # AFRM: 400 shares no trade explains (held before the record), 100 bought later. The
    # calls started before that purchase and must still count.
    pf = client.app.state.accounts["portfolios"]["sim"]
    chains = [
        chain("calls", "Short call", 100, [at(ev([(CALL, -4, 2.0)]), 100)], status="closed"),
        chain("buy", "Stock", 300, [at(ev([("AAPL", 100, 60.0)]), 300)]),
    ]
    pf.trade_info = lambda: {"buy": {"opened_at": 300}}
    pf.chain_list = lambda: chains
    rows = [shares_row(None, 400, 89.32), shares_row("buy", 100, 89.32)]
    pf._adjust_basis("AAPL", rows)
    n = rows[0]["numbers"]
    assert n["covered_premium"] == 800.0 and n["covered_since"] == 100
    assert n["adjusted_basis"] == round(89.32 - 1.6, 4)
