from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import chains
from app.chain_service import ChainService
from app.config import Settings
from app.datasource import DemoDataSource, _demo_history, _demo_txn
from app.main import create_app
from app.notify import ET
from app.options import option_symbol
from app.portfolio import PortfolioService
from app.store import MemoryStore

TODAY = date(2026, 11, 11)
EXP = date(2026, 12, 18)
NEXT = date(2027, 1, 15)
T0 = datetime(2026, 10, 1, 15, tzinfo=UTC)


def sym(pc, k, exp=EXP, u="SPY"):
    return option_symbol(u, exp, pc, k)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def groups(*txns):
    return chains.merge([g for t in txns if (g := chains.normalize(t)) is not None])


def by_strategy(result):
    return {c["strategy"]: c for c in result["chains"]}


# Parsing --------------------------------------------------------------------------------


def test_normalize_reads_direction_effect_and_fees():
    t = _demo_txn(7, at(0), [(sym("P", 555), -1, 4.85, "open"), (sym("P", 550), 1, 4.0, "open")])
    g = chains.normalize(t)
    assert g.key == "70" and g.kind == "trade" and g.fees == 1.30
    assert [(f.quantity, f.effect, f.price) for f in g.fills] == [
        (-1, "open", 4.85),
        (1, "open", 4.0),
    ]
    assert chains.normalize({"type": "DIVIDEND_OR_INTEREST"}) is None
    exp = _demo_txn(
        8,
        at(0),
        [(sym("P", 555), 1, 0.0, "close")],
        kind="RECEIVE_AND_DELIVER",
        desc="OPTION EXPIRATION",
    )
    assert chains.normalize(exp).kind == "expire"


def test_timestamps_in_schwab_formats():
    assert chains._ts("2026-10-01T15:00:00+0000") == T0.timestamp()
    assert chains._ts("2026-10-01T15:00:00Z") == T0.timestamp()
    assert chains._ts(T0.timestamp() * 1000) == T0.timestamp()


# Building -------------------------------------------------------------------------------


def test_open_then_roll_in_one_order_is_one_chain_with_cumulative_credit():
    r = chains.build(
        groups(
            _demo_txn(
                1, at(0), [(sym("P", 545), -1, 3.10, "open"), (sym("C", 600), -1, 2.80, "open")]
            ),
            _demo_txn(
                2,
                at(60 * 24 * 20),
                [
                    (sym("P", 545), 1, 1.20, "close"),
                    (sym("C", 600), 1, 3.60, "close"),
                    (sym("P", 555, NEXT), -1, 4.85, "open"),
                    (sym("C", 610, NEXT), -1, 3.95, "open"),
                ],
            ),
        ),
        {sym("P", 555, NEXT): {"quantity": -1}, sym("C", 610, NEXT): {"quantity": -1}},
    )
    (c,) = r["chains"]
    assert c["strategy"] == "Strangle" and c["status"] == "open"
    assert [e["kind"] for e in c["events"]] == ["open", "roll"]
    assert chains.trade_info(r["chains"])[c["id"]] == {
        "strategy": "Strangle",
        "net": pytest.approx(590 - 480 + 880),
        "rolls": 1,
        "opened_at": c["opened_at"],
    }
    assert c["holdings"] == {sym("P", 555, NEXT): -1, sym("C", 610, NEXT): -1}
    assert r["mismatches"] == []


def test_roll_as_two_orders_is_linked_and_can_be_split_or_linked_by_hand():
    txns = (
        _demo_txn(1, at(0), [(sym("P", 545), -1, 3.10, "open")]),
        _demo_txn(2, at(60), [(sym("P", 545), 1, 1.20, "close")]),
        _demo_txn(3, at(65), [(sym("P", 545, NEXT), -1, 3.40, "open")]),
    )
    pos = {sym("P", 545, NEXT): {"quantity": -1}}
    r = chains.build(groups(*txns), pos)
    (c,) = r["chains"]
    assert [e["kind"] for e in c["events"]] == ["open", "close", "roll"]
    assert c["events"][-1]["auto_linked"] and "15 minutes" in c["events"][-1]["note"]
    split = chains.build(groups(*txns), pos, {"split": ["30"]})
    assert [x["status"] for x in split["chains"]] == ["closed", "open"]
    # Hours later the same day, closed at 66 DTE for a profit: a finished trade, a new one.
    late = (txns[0], txns[1], _demo_txn(3, at(180), [(sym("P", 545, NEXT), -1, 3.4, "open")]))
    r = chains.build(groups(*late), pos)
    assert len(r["chains"]) == 2 and r["proposals"] == []
    first = r["chains"][0]["id"]
    linked = chains.build(groups(*late), pos, {"link": {"30": first}})
    (c,) = linked["chains"]
    assert c["events"][-1]["note"] == "Linked by you" and c["status"] == "open"


def test_rolls_are_decided_without_asking():
    near = date(2026, 10, 16)  # 15 DTE on the day of the close

    def case(close_price, open_legs, minutes=180, exp=near):
        txns = (
            _demo_txn(1, at(0), [(sym("P", 545, exp), -1, 3.10, "open")]),
            _demo_txn(2, at(60), [(sym("P", 545, exp), 1, close_price, "close")]),
            _demo_txn(3, at(minutes), open_legs),
        )
        held = {symbol: {"quantity": n} for symbol, n, _, _ in open_legs}
        return chains.build(groups(*txns), held)

    new_put = [(sym("P", 540, NEXT), -2, 3.4, "open")]
    # Closed inside 21 DTE, a short put reopened that afternoon (other strike, size): a roll.
    (c,) = case(1.20, new_put)["chains"]
    assert c["events"][-1]["kind"] == "roll" and "15 DTE" in c["events"][-1]["note"]
    # Closed at a loss, far from expiration, reopened the same day: a roll.
    (c,) = case(5.00, new_put, exp=EXP)["chains"]
    assert "at a loss" in c["events"][-1]["note"]
    # A different position (a short call) is a new trade, whatever the timing.
    r = case(1.20, [(sym("C", 600, NEXT), -1, 2.0, "open")], minutes=65)
    assert len(r["chains"]) == 2
    # The next day: a new trade.
    assert len(case(1.20, new_put, minutes=60 * 24 + 60)["chains"]) == 2
    # Within 15 minutes, a winner far from expiration still counts: same position, a roll.
    (c,) = case(1.20, new_put, minutes=70, exp=EXP)["chains"]
    assert "15 minutes" in c["events"][-1]["note"]
    assert case(1.20, new_put)["proposals"] == []


def test_expiration_and_partial_close():
    r = chains.build(
        groups(
            _demo_txn(1, at(0), [(sym("P", 545), -2, 3.10, "open")]),
            _demo_txn(2, at(10), [(sym("P", 545), 1, 1.00, "close")]),
            _demo_txn(
                3,
                at(20),
                [(sym("P", 545), 1, 0.0, "close")],
                kind="RECEIVE_AND_DELIVER",
                desc="REMOVAL OF OPTION DUE TO EXPIRATION",
            ),
        ),
        {},
    )
    (c,) = r["chains"]
    assert [e["kind"] for e in c["events"]] == ["open", "close", "expire"]
    assert c["status"] == "closed"
    assert c["realized_pl"] == pytest.approx(620 - 100 - 1.30 - 0.65)


def test_assignment_shares_join_the_option_chain():
    put = sym("P", 545)
    shares = {
        "activityId": 9,
        "orderId": None,
        "time": at(30).isoformat(),
        "type": "RECEIVE_AND_DELIVER",
        "description": "ASSIGNMENT",
        "netAmount": -54500,
        "transferItems": [
            {
                "instrument": {"assetType": "EQUITY", "symbol": "SPY"},
                "amount": 100,
                "cost": -54500,
                "price": 545,
                "positionEffect": "OPENING",
            }
        ],
    }
    r = chains.build(
        groups(
            _demo_txn(1, at(0), [(put, -1, 3.10, "open")]),
            _demo_txn(
                2,
                at(30),
                [(put, 1, 0.0, "close")],
                kind="RECEIVE_AND_DELIVER",
                desc="OPTION ASSIGNMENT",
            ),
            shares,
        ),
        {"SPY": {"quantity": 100, "underlying": "SPY"}},
    )
    (c,) = r["chains"]
    assert [e["kind"] for e in c["events"]] == ["open", "assign", "assign"]
    assert c["holdings"] == {"SPY": 100} and c["status"] == "open"
    assert r["mismatches"] == []


def test_positions_from_before_the_history_are_carried_in_and_closable():
    pos = {
        sym("P", 470): {"quantity": 1, "avg_price": 2.1, "underlying": "QQQ"},
        sym("P", 475): {"quantity": -1, "avg_price": 2.75, "underlying": "QQQ"},
    }
    r = chains.build([], pos)
    (c,) = r["chains"]
    assert c["carried_in"] and c["strategy"] == "Put vertical"
    assert c["events"][0]["net"] == pytest.approx(65.0)
    # A close of something bought before the history starts: the position it closed is
    # reconstructed as a carried-in chain, which this order then closes.
    closing = _demo_txn(
        1, at(0), [(sym("P", 470), -1, 0.5, "close"), (sym("P", 475), 1, 0.9, "close")]
    )
    r = chains.build(groups(closing), {})
    (c,) = r["chains"]
    assert c["carried_in"] and c["status"] == "closed" and r["unmatched"] == []
    assert [e["kind"] for e in c["events"]] == ["carried", "close"]
    assert "opening price is unknown" in c["events"][0]["note"]
    from app.sim import stats

    assert stats(r["chains"]) == []  # excluded from results by strategy


def test_mismatch_reported_when_schwab_disagrees():
    r = chains.build(
        groups(_demo_txn(1, at(0), [(sym("P", 545), -1, 3.10, "open")])),
        {sym("P", 545): {"quantity": -2}},
    )
    # One contract is carried in to make up the difference, so nothing is lost.
    assert sum(abs(q) for c in r["chains"] for q in c["holdings"].values()) == 2
    assert r["mismatches"] == []


# Demo history, service and positions ----------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


def demo_setup():
    store = MemoryStore()
    demo = DemoDataSource(today=TODAY)
    s = Settings(_env_file=None, app_password="p" * 12, secret_key="s" * 32, demo_mode=True)

    async def resolver():
        return DemoDataSource.HASH

    pf = PortfolioService(
        demo,
        store,
        s,
        lambda: {},
        resolver,
        "demo",
        clock=lambda: datetime(2026, 11, 11, 11, tzinfo=ET),
    )
    svc = ChainService(demo, store, pf, resolver)
    pf.trade_info, pf.chain_holdings = svc.trade_info, svc.holdings
    return store, demo, pf, svc


@pytest.mark.anyio
async def test_demo_history_builds_expected_chains_and_feeds_positions():
    store, demo, pf, svc = demo_setup()
    await pf.tick()  # loads positions
    assert len(_demo_history(demo)) == 6
    assert await svc.sync() == 6
    assert await svc.sync() == 0  # idempotent
    res = svc.result
    assert res["mismatches"] == [] and res["unmatched"] == []
    names = sorted(
        (c["underlying"], c["strategy"], c["status"], c["carried_in"]) for c in res["chains"]
    )
    assert names == [
        ("AAPL", "Covered call", "open", True),
        ("IWM", "Short put", "open", False),
        ("QQQ", "Iron condor", "open", True),
        ("SPY", "Iron condor", "closed", False),
        ("SPY", "Put vertical", "open", False),
        ("SPY", "Strangle", "open", False),
    ]
    strangle = next(c for c in res["chains"] if c["strategy"] == "Strangle")
    assert [e["kind"] for e in strangle["events"]] == ["open", "roll"]
    await pf.tick()
    spy = next(u for u in pf.snapshot["underlyings"] if u["symbol"] == "SPY")
    st = next(s for s in spy["strategies"] if s["name"] == "Strangle")
    assert st["trade_id"] == strangle["id"] and st["rolls"] == 1
    assert st["numbers"]["open_value"] == pytest.approx(590 - 480 + 880)


def test_chain_api_link_split_and_activity(tmp_path):
    s = Settings(
        _env_file=None,
        app_password="p" * 12,
        secret_key="s" * 32,
        demo_mode=True,
        cookie_secure=False,
        data_dir=tmp_path,
    )
    app = create_app(
        s,
        store=MemoryStore(),
        start_background=False,
        static_dir=tmp_path / "x",
        source=DemoDataSource(today=TODAY),
    )
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "p" * 12})
        assert c.get("/api/chains").json()["chains"] == []
        assert c.get("/api/chains?account=sim").json()["chains"] == []
        act = c.get("/api/activity?account=live").json()
        assert act["account"] == "live" and act["orders"] == []
        assert c.post("/api/chains/link", json={"group": "1", "chain": "nope"}).status_code == 400
        assert c.post("/api/chains/split", json={"group": "1"}).json() == {"ok": True}
