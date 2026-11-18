import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient
from test_phase3 import PASSWORD, TODAY, first_put

from app import apikeys
from app.config import Settings
from app.datasource import DemoDataSource
from app.main import create_app
from app.store import MemoryStore


class Notifier:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, title, message, url="", priority=0):
        self.sent.append((title, message))


@pytest.fixture
def env(tmp_path):
    s = Settings(
        _env_file=None,
        app_password=PASSWORD,
        secret_key="s" * 32,
        demo_mode=True,
        cookie_secure=False,
        data_dir=tmp_path,
    )
    store = MemoryStore()
    notifier = Notifier()
    app = create_app(
        s,
        store=store,
        notifier=notifier,
        start_background=False,
        static_dir=tmp_path / "none",
        source=DemoDataSource(today=TODAY),
    )
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": PASSWORD})
        yield c, app, store, notifier


def make_key(c, name, level):
    r = c.post("/api-keys/create", data={"name": name, "level": level})
    assert r.status_code == 303
    page = c.get("/connect?fragment=1").text
    m = re.search(r"(trd_[A-Za-z0-9_\-]{30,})", page)
    assert m, "the new key is shown once on Settings"
    # Shown once only.
    assert m.group(1) not in c.get("/connect?fragment=1").text
    return m.group(1)


def keyed(app, key):
    """A client with no session, only the key."""
    c = TestClient(app, follow_redirects=False)
    c.headers["Authorization"] = f"Bearer {key}"
    return c


def order_body(c, account="sim"):
    put = first_put(c)
    return {
        "account": account,
        "intent": "open",
        "legs": [{"symbol": put, "quantity": -1, "effect": "open"}],
    }


def test_keys_are_stored_hashed_and_can_be_revoked(env):
    c, app, store, _ = env
    key = make_key(c, "claude", "read")
    keys = asyncio.run(app.state.api_keys.all())
    assert key not in json.dumps(keys)
    assert keys[0]["hash"] == apikeys._hash(key) and keys[0]["last4"] == key[-4:]
    k = keyed(app, key)
    assert k.get("/api/snapshot?account=sim").status_code == 200
    c.post("/api-keys/revoke", data={"key_id": keys[0]["id"]})
    assert k.get("/api/snapshot?account=sim").status_code == 401


def test_bad_keys_are_refused_and_throttled(env):
    _, app, _, _ = env
    k = keyed(app, "trd_nope")
    codes = [k.get("/api/accounts").status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    assert TestClient(app).get("/api/accounts").status_code == 401


def test_read_key_reads_and_previews_but_does_not_trade(env):
    c, app, _, _ = env
    k = keyed(app, make_key(c, "reader", "read"))
    body = order_body(c)
    assert k.get("/api/snapshot?account=sim").status_code == 200
    assert k.post("/api/orders/preview", json=body).status_code == 200
    r = k.post("/api/orders", json={**body, "limit_net": 1.0})
    assert r.status_code == 403 and "can't trade" in r.json()["detail"]


def test_sim_key_trades_only_the_simulated_account_and_notifies(env):
    c, app, _, notifier = env
    k = keyed(app, make_key(c, "simbot", "sim"))
    body = order_body(c)
    pv = k.post("/api/orders/preview", json=body).json()
    r = k.post("/api/orders", json={**body, "limit_net": pv["limit_net"]})
    assert r.status_code == 200 and r.json()["status"] == "FILLED"
    assert notifier.sent and "simbot" in notifier.sent[-1][1]
    # Activity marks the key's order, and only that one.
    by_id = {o["id"]: o for o in c.get("/api/activity?account=sim").json()["orders"]}
    assert by_id[r.json()["id"]]["placed_by_key"] == "simbot"
    manual = c.post("/api/orders", json={**body, "limit_net": pv["limit_net"]}).json()
    by_id = {o["id"]: o for o in c.get("/api/activity?account=sim").json()["orders"]}
    assert "placed_by_key" not in by_id[manual["id"]]
    # A reset starts the order numbers again, so the marks go with it.
    assert (
        c.post("/api/sim/reset", json={"start_cash": 50000, "confirm": "RESET"}).status_code == 200
    )
    orders = c.get("/api/activity?account=sim").json()["orders"]
    assert not any("placed_by_key" in o for o in orders)
    r = k.post("/api/orders", json={**body, "account": "live", "limit_net": pv["limit_net"]})
    assert r.status_code == 403 and "can't trade on 'live'" in r.json()["detail"]
    r = k.post("/api/scanner/run?account=live")
    assert r.status_code == 403


def test_live_key_still_goes_through_the_live_gate(env):
    c, app, _, _ = env
    k = keyed(app, make_key(c, "livebot", "live"))
    body = order_body(c, "live")
    r = k.post("/api/orders", json={**body, "limit_net": 1.0})
    # Past the key check, stopped by the same gate as a manual live order.
    assert r.status_code in (400, 403) and "can't trade" not in r.json()["detail"]


def test_keys_cannot_change_settings(env):
    c, app, _, _ = env
    k = keyed(app, make_key(c, "boss", "live"))
    for path, kw in [
        ("/api/sim/reset", {"json": {"confirm": "RESET"}}),
        ("/api/automation/pause?account=sim", {"json": {"paused": True}}),
        ("/api/watchlist/view", {"json": {"symbol": "SPY", "view": "own"}}),
        ("/api/watchlist/add", {"json": {"text": "GLD"}}),
        ("/api/watchlist/remove", {"json": {"symbol": "SPY"}}),
        ("/api/watchlist/upload", {"files": {"file": ("w.csv", b"GLD", "text/csv")}}),
        ("/api/chains/split?account=sim", {"json": {"group": "x"}}),
    ]:
        r = k.post(path, **kw)
        assert r.status_code == 403 and "settings" in r.json()["detail"], path
    # Pages and settings forms are session only.
    assert k.get("/connect").status_code == 303
    assert k.post("/api-keys/create", data={"name": "x", "level": "live"}).status_code == 303
    assert k.post("/live/settings", data={}).status_code == 303


def test_key_names_and_levels_are_checked(env):
    c, app, _, _ = env
    c.post("/api-keys/create", data={"name": "ok", "level": "admin"})
    c.post("/api-keys/create", data={"name": "<script>", "level": "read"})
    assert asyncio.run(app.state.api_keys.all()) == []
    page = c.get("/connect?fragment=1").text
    assert "trd_" not in page


def test_openapi_needs_a_session_or_key(env):
    c, app, _, _ = env
    assert c.get("/api/openapi.json").json()["info"]["title"] == "Trading"
    assert TestClient(app).get("/api/openapi.json").status_code == 401
    assert TestClient(app).get("/openapi.json").status_code == 404


MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def rpc(client, method, params=None, id_=1):
    r = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
    )
    return r


def test_mcp_needs_a_key(env):
    _, app, _, _ = env
    assert rpc(TestClient(app), "tools/list").status_code == 401
    assert rpc(keyed(app, "trd_wrong"), "tools/list").status_code == 401


def test_mcp_tools_use_the_keys_level(env):
    c, app, _, _ = env
    key = make_key(c, "mcp", "read")
    body = order_body(c)
    k = keyed(app, key)
    init = rpc(
        k,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert init.status_code == 200, init.text
    tools = rpc(k, "tools/list", id_=2)
    assert tools.status_code == 200, tools.text
    names = {t["name"] for t in tools.json()["result"]["tools"]}
    assert {"get_snapshot", "get_chain", "preview_order", "place_order"} <= names

    snap = rpc(k, "tools/call", {"name": "get_snapshot", "arguments": {"account": "sim"}}, 3)
    res = snap.json()["result"]
    assert not res.get("isError"), res
    pv = rpc(k, "tools/call", {"name": "preview_order", "arguments": {"order": body}}, 4)
    res = pv.json()["result"]
    assert not res.get("isError") and '"errors": []' in res["content"][0]["text"]
    placed = rpc(
        k,
        "tools/call",
        {"name": "place_order", "arguments": {"order": {**body, "limit_net": 1.0}}},
        5,
    )
    res = placed.json()["result"]
    assert res.get("isError") and "403" in res["content"][0]["text"]


def test_the_ai_guide_is_shown_in_the_app(env):
    c, app, _, _ = env
    page = c.get("/guide/ai")
    assert page.status_code == 200
    html = page.text
    assert "Why connect an AI" in html and "Tailscale" in html
    # Links between guides stay inside the app, and headings carry GitHub's anchors.
    assert 'href="/guide/deploy#tailscale-optional-a-trusted-certificate' in html
    deploy = c.get("/guide/deploy").text
    assert 'id="tailscale-optional-a-trusted-certificate-and-access-away-from-home"' in deploy
    assert c.get("/guide/nope").status_code == 404
    assert c.get("/guide/..%2Fmain").status_code == 404
    # Settings links to it; signed out it goes to the login page, and a key can't open it.
    assert 'href="/guide/ai"' in c.get("/connect?fragment=1").text
    assert TestClient(app, follow_redirects=False).get("/guide/ai").status_code == 303


def test_an_order_placed_from_a_proposals_ticket_settles_the_proposal(env):
    c, app, _, _ = env
    auto = app.state.accounts["automations"]["sim"]
    body = order_body(c)
    c.get("/api/automation?account=sim")  # loads its state
    auto.state["proposals"].append(
        {
            "id": "p1",
            "key": "scan:sim:SPY:2026-11-18",
            "trade_id": "new:SPY",
            "rule": "scan",
            "kind": "open",
            "underlying": "SPY",
            "strategy": "Strangle",
            "reason": "New trade",
            "bodies": [body],
            "status": "pending",
            "created_at": 0,
            "due_at": None,
        }
    )
    pv = c.post("/api/orders/preview", json=body).json()
    r = c.post("/api/orders", json={**body, "limit_net": pv["limit_net"], "proposal_id": "p1"})
    assert r.status_code == 200
    assert c.get("/api/automation?account=sim").json()["proposals"] == []
    assert "scan:sim:SPY:2026-11-18" in auto.state["acted"]
