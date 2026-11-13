import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import MAX_LOGIN_FAILURES, create_app
from app.schwab_service import (
    AccountSummary,
    ConnectionStatus,
    SchwabError,
    SchwabService,
    summarize_account,
)
from app.store import MemoryStore
from app.token_store import TokenStore, TokenStoreError

PASSWORD = "correct horse battery"
SECRET = "s" * 40


class FakeSchwab:
    def __init__(self, connected=False):
        self.connected = connected
        self.completed_with = None
        self.fail_with = None

    def load(self):
        pass

    async def close(self):
        pass

    def status(self):
        return ConnectionStatus(connected=self.connected, token_age_s=86400.0, created_at=1)

    def authorization_url(self):
        return "https://api.schwabapi.com/v1/oauth/authorize?client_id=x"

    async def complete_authorization(self, url):
        if self.fail_with:
            raise SchwabError(self.fail_with)
        self.completed_with = url
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def accounts(self):
        return [AccountSummary("6789", "MARGIN", 51234.5, 1000.0, 20000.0, 10000.0, 3)]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        app_password=PASSWORD,
        secret_key=SECRET,
        schwab_app_key="key",
        schwab_app_secret="secret",
        data_dir=tmp_path,
        db_host="127.0.0.1",
        db_port=1,
        cookie_secure=False,
    )


class FakeSource:
    async def account_numbers(self):
        return [
            {"accountNumber": "11116789", "hashValue": "H1"},
            {"accountNumber": "22224321", "hashValue": "H2"},
        ]


class FakeNotifier:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.sent = []

    async def send(self, title, message, url="", priority=0):
        self.sent.append((title, message))


def make_client(settings, schwab, store=None, notifier=None, static_dir=None):
    app = create_app(
        settings,
        schwab,
        store=store or MemoryStore(),
        source=FakeSource(),
        notifier=notifier or FakeNotifier(enabled=False),
        start_background=False,
        static_dir=static_dir or settings.data_dir / "no-frontend-build",
    )
    return TestClient(app, follow_redirects=False)


def login(client):
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 303


def test_healthz_is_open(settings):
    with make_client(settings, FakeSchwab()) as c:
        assert c.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("path", ["/", "/connect", "/schwab/authorize", "/schwab/callback"])
def test_pages_require_login(settings, path):
    with make_client(settings, FakeSchwab()) as c:
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("path", ["/api/status", "/api/accounts", "/api/snapshot", "/api/events"])
def test_api_requires_login(settings, path):
    with make_client(settings, FakeSchwab(connected=True)) as c:
        assert c.get(path).status_code == 401


def test_posts_require_login(settings):
    schwab = FakeSchwab(connected=True)
    with make_client(settings, schwab) as c:
        assert c.post("/schwab/disconnect").status_code == 303
        assert c.post("/account/select", data={"number": "11116789"}).status_code == 303
        assert c.post("/notify/test").status_code == 303
        assert c.post("/schwab/complete", data={"received_url": "x"}).status_code == 303
        assert schwab.connected and schwab.completed_with is None


def test_wrong_password_and_lockout(settings):
    with make_client(settings, FakeSchwab()) as c:
        for _ in range(MAX_LOGIN_FAILURES):
            assert c.post("/login", data={"password": "nope"}).status_code == 401
        # Locked now: even the right password is refused.
        r = c.post("/login", data={"password": PASSWORD})
        assert r.status_code == 401 and "Too many attempts" in r.text


def test_root_falls_back_to_connect_page_without_frontend_build(settings):
    with make_client(settings, FakeSchwab(connected=True)) as c:
        login(c)
        r = c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/connect"


def test_root_serves_frontend_build(settings, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<div id=root></div>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    with make_client(settings, FakeSchwab(connected=True), static_dir=static) as c:
        assert c.get("/assets/app.js").status_code == 200  # public, no secrets in the bundle
        assert c.get("/").status_code == 303  # the page itself needs the app login
        login(c)
        assert "id=root" in c.get("/").text


def test_connect_page_lists_masked_accounts_and_selects_one(settings):
    store = MemoryStore()
    with make_client(settings, FakeSchwab(connected=True), store=store) as c:
        login(c)
        r = c.get("/connect")
        assert r.status_code == 200
        assert "…6789" in r.text and "…4321" in r.text
        assert "11116789" in r.text  # form value; the page is behind the app login
        assert "shows 2 accounts" in r.text
        assert "Notifications off" in r.text
        c.post("/account/select", data={"number": "22224321"})
        assert store.settings["account"] == {"number": "22224321", "last4": "4321"}
        assert "Using the account ending 4321" in c.get("/connect").text


def test_selecting_an_invisible_account_is_refused(settings):
    store = MemoryStore()
    with make_client(settings, FakeSchwab(connected=True), store=store) as c:
        login(c)
        c.post("/account/select", data={"number": "99990000"})
        assert "account" not in store.settings
        assert "not visible" in c.get("/connect").text


def test_connect_flow(settings):
    schwab = FakeSchwab()
    with make_client(settings, schwab) as c:
        login(c)
        assert "Schwab not connected" in c.get("/connect").text
        r = c.get("/schwab/authorize")
        assert r.headers["location"].startswith("https://api.schwabapi.com/")
        url = "https://127.0.0.1:8182/?code=abc&session=def"
        assert c.post("/schwab/complete", data={"received_url": url}).status_code == 303
        assert schwab.completed_with == url
        page = c.get("/connect").text
        assert "Schwab connected" in page and "Schwab sign-in renewed" in page


def test_one_tap_callback(settings):
    settings.schwab_callback_url = "https://192.168.4.102/schwab/callback"
    schwab = FakeSchwab()
    with make_client(settings, schwab) as c:
        login(c)
        assert "Sign in with Schwab" in c.get("/connect").text
        r = c.get("/schwab/callback?code=abc&state=xyz")
        assert r.status_code == 303 and r.headers["location"] == "/connect#connection"
        assert schwab.completed_with.endswith("/schwab/callback?code=abc&state=xyz")


def test_connect_failure_is_shown_once(settings):
    schwab = FakeSchwab()
    schwab.fail_with = "Schwab rejected the sign-in"
    with make_client(settings, schwab) as c:
        login(c)
        c.post("/schwab/complete", data={"received_url": "x"})
        assert "Schwab rejected the sign-in" in c.get("/connect").text
        assert "Schwab rejected the sign-in" not in c.get("/connect").text


def test_test_notification(settings):
    notifier = FakeNotifier()
    with make_client(settings, FakeSchwab(connected=True), notifier=notifier) as c:
        login(c)
        assert "Notifications on" in c.get("/connect").text
        c.post("/notify/test")
        assert notifier.sent and "Test notification sent" in c.get("/connect").text


def test_api_accounts_status_and_snapshot(settings):
    with make_client(settings, FakeSchwab(connected=True)) as c:
        login(c)
        assert c.get("/api/accounts").json()[0]["last4"] == "6789"
        status = c.get("/api/status").json()
        assert status["schwab"] == {"connected": True, "expires_in_days": 6.0, "detail": None}
        assert status["database"]["ok"] is True
        assert c.get("/api/snapshot").json() == {"pending": True}


def test_demo_mode_needs_no_schwab_keys():
    s = Settings(_env_file=None, app_password=PASSWORD, secret_key=SECRET, demo_mode=True)
    assert s.demo_mode
    with pytest.raises(ValueError, match="SCHWAB_APP_KEY"):
        Settings(_env_file=None, app_password=PASSWORD, secret_key=SECRET)


def test_token_store_roundtrip_and_encryption(tmp_path):
    path = tmp_path / "t.enc"
    store = TokenStore(path, SECRET)
    token = {"creation_timestamp": 1, "token": {"refresh_token": "very-secret-value"}}
    store.write(token)
    assert store.read() == token
    assert b"very-secret-value" not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(TokenStoreError, match="SECRET_KEY"):
        TokenStore(path, "x" * 40).read()
    store.delete()
    assert not store.exists()


def test_summarize_account_masks_number():
    raw = {
        "securitiesAccount": {
            "accountNumber": "123456789",
            "type": "MARGIN",
            "currentBalances": {"liquidationValue": 10.5, "buyingPower": 20.0},
            "positions": [{}, {}],
        }
    }
    s = summarize_account(raw)
    assert (s.last4, s.type, s.net_liq, s.buying_power, s.positions) == (
        "6789",
        "MARGIN",
        10.5,
        20.0,
        2,
    )
    assert s.cash is None


def test_service_without_token_is_disconnected(tmp_path):
    svc = SchwabService("k", "s", "https://127.0.0.1:8182", TokenStore(tmp_path / "t", SECRET))
    svc.load()
    assert svc.status().connected is False


@pytest.mark.anyio
async def test_service_rejects_wrong_callback_address(tmp_path):
    svc = SchwabService("k", "s", "https://127.0.0.1:8182", TokenStore(tmp_path / "t", SECRET))
    with pytest.raises(SchwabError, match="No sign-in in progress"):
        await svc.complete_authorization("https://127.0.0.1:8182/?code=a")
    url = svc.authorization_url()
    assert "client_id=k" in url and "redirect_uri=https%3A%2F%2F127.0.0.1%3A8182" in url
    with pytest.raises(SchwabError, match="does not start with the callback URL"):
        await svc.complete_authorization("https://evil.example/?code=a")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_db_password_accepts_postgres_variable_name(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-postgres-name")
    s = Settings(
        _env_file=None,
        app_password=PASSWORD,
        secret_key=SECRET,
        schwab_app_key="k",
        schwab_app_secret="s",
    )
    assert s.db_password == "from-postgres-name"


def test_settings_live_inside_the_app_when_it_is_built(settings, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<div id=root></div>")
    with make_client(settings, FakeSchwab(connected=True), static_dir=static) as c:
        login(c)
        r = c.get("/connect")
        assert r.status_code == 303 and r.headers["location"] == "/#/settings"
        frag = c.get("/connect?fragment=1")
        assert frag.status_code == 200 and "<html" not in frag.text and "Watchlist" in frag.text
        r = c.post("/watchlist/remove", data={"symbol": "SPY"})
        assert r.headers["location"] == "/#/settings/watchlist"
        r = c.post("/rules", data={"manage_dte": "999"})
        assert r.headers["location"] == "/#/settings/rules"
        assert "must be between" in c.get("/connect?fragment=1").text


def test_automanage_settings_and_api(settings):
    store = MemoryStore()
    with make_client(settings, FakeSchwab(connected=True), store=store) as c:
        login(c)
        r = c.post("/automation/settings", data={"level_loss": "auto", "daily_cap": "4"})
        assert r.status_code == 303
        saved = store.settings["automation_settings"]
        assert saved["levels"]["loss"] == "auto" and saved["daily_cap"] == 4
        c.post("/automation/settings", data={"daily_cap": "0"})
        assert "must be between" in c.get("/connect").text
        assert store.settings["automation_settings"]["daily_cap"] == 4
        view = c.post("/api/automation/trade", json={"trade_id": 7, "enabled": True}).json()
        assert view["trades"]["7"]["enabled"] is True
        assert c.post("/api/automation/pause", json={"paused": True}).json()["paused"] is True


def test_buying_power_limits_are_settings_with_warnings(settings):
    store = MemoryStore()
    with make_client(settings, FakeSchwab(connected=True), store=store) as c:
        login(c)
        page = c.get("/connect").text
        assert "Buying power in one underlying" in page and "Hard limit" in page
        c.post("/rules", data={"max_underlying_bp": "20", "max_index_bp": "60"})
        saved = store.settings["rules"]
        assert saved["max_underlying_bp"] == 0.20 and saved["max_index_bp"] == 0.60
        assert saved["block_over_ceiling"] is False  # unticked on that form
        c.post("/rules", data={"block_over_ceiling": "on"})
        assert store.settings["rules"]["block_over_ceiling"] is True


def test_portfolio_margin_halves_the_sizing_defaults(settings):
    from app.analytics import bp_ceiling

    store = MemoryStore()
    with make_client(settings, FakeSchwab(connected=True), store=store) as c:
        login(c)
        assert "Portfolio margin" in c.get("/connect").text
        c.post("/rules", data={"margin_type": "pm", "max_underlying_bp": "15"})
        r = store.settings["rules"]
        assert r["margin_type"] == "pm"
        assert r["max_underlying_bp"] == 0.075 and r["max_index_bp"] == 0.25
        assert store.settings["scanner_settings"]["max_bp_share"] == 0.025
        assert store.settings["live_trading"]["max_bp_share"] == pytest.approx(0.025)
        assert "Portfolio margin" in c.get("/connect").text
        # Adjusting a limit afterwards keeps the margin type and the new value.
        c.post("/rules", data={"margin_type": "pm", "max_underlying_bp": "10"})
        assert store.settings["rules"]["max_underlying_bp"] == 0.10
        # Back to Reg T: its defaults return.
        c.post("/rules", data={"margin_type": "reg_t"})
        assert store.settings["rules"]["max_underlying_bp"] == 0.15
        assert store.settings["scanner_settings"]["max_bp_share"] == 0.05
        bad = c.post("/rules", data={"margin_type": "cash"})
        assert bad.status_code == 303
    assert bp_ceiling(18) == 0.30 and bp_ceiling(18, 0.5) == 0.15


def test_settings_page_shows_portfolio_margin_values_everywhere(settings):
    with make_client(settings, FakeSchwab(connected=True), store=MemoryStore()) as c:
        login(c)
        c.post("/rules", data={"margin_type": "pm"})
        page = c.get("/connect").text
        assert 'id="r-max_underlying_bp"' in page and 'value="7.5"' in page
        assert 'id="s-max_bp_share"' in page and 'value="2.5"' in page
        assert "saves at once" in page


def test_delta_bias_from_the_full_settings_form(settings):
    """The bias select saves the whole rules form at once, with the old band in it."""
    import re

    store = MemoryStore()
    with make_client(settings, FakeSchwab(connected=True), store=store) as c:
        login(c)
        page = c.get("/connect").text
        assert 'id="r-bias"' in page and "Portfolio targets" in page
        form = dict(
            re.findall(
                r'<input id="r-[^"]+" type="number" name="([^"]+)"[^>]*?value="([^"]*)"', page, re.S
            )
        )
        assert form["delta_min"] == "-5" and form["delta_max"] == "5"
        for bias, band in (
            ("bullish", (0.0, 0.15)),
            ("bearish", (-0.15, 0.0)),
            ("neutral", (-0.05, 0.05)),
        ):
            c.post("/rules", data={**form, "margin_type": "reg_t", "delta_bias": bias})
            r = store.settings["rules"]
            assert r["delta_bias"] == bias and (r["delta_min"], r["delta_max"]) == band
        page = c.get("/connect").text
        assert 'id="r-delta_min"' in page and "0.1% to 0.3% of net liq" in page


def test_the_env_alone_decides_schwab_orders_and_each_account_has_its_own_levels(settings):
    store = MemoryStore()
    on = settings.model_copy(update={"live_trading": True, "live_auto": True})
    with make_client(on, FakeSchwab(connected=True), store=store) as c:
        login(c)
        # LIVE_TRADING and LIVE_AUTO in .env: nothing else to switch on.
        assert c.get("/api/automation?account=live").json()["max_level"] == "auto"
        page = c.get("/connect").text
        assert 'id="l-mode"' not in page and "Automatic orders: <b>allowed</b>" in page
        assert 'id="a-live-profit"' in page and "What runs by itself now" in page
        live_select = re.search(r'<select id="a-live-profit".*?</select>', page, re.S).group(0)
        assert 'value="auto"' in live_select
        # Each account's own levels: Schwab at Auto leaves the simulated level alone.
        c.post(
            "/automation/settings",
            data={"_form": "automation", "level_profit": "notify", "live_level_profit": "auto"},
        )
        assert c.get("/api/automation?account=live").json()["settings"]["levels"]["profit"] == (
            "auto"
        )
        assert c.get("/api/automation?account=sim").json()["settings"]["levels"]["profit"] == (
            "notify"
        )
    store = MemoryStore()
    allowed = settings.model_copy(update={"live_trading": True})
    with make_client(allowed, FakeSchwab(connected=True), store=store) as c:
        login(c)
        assert c.get("/api/automation?account=live").json()["max_level"] == "propose"
        page = c.get("/connect").text
        assert "Automatic orders: <b>not allowed</b>" in page and "LIVE_AUTO=true" in page
        live_select = re.search(r'<select id="a-live-profit".*?</select>', page, re.S).group(0)
        assert 'value="auto"' not in live_select  # not offered, so never lowered quietly
        c.post("/automation/settings", data={"_form": "automation", "live_level_dte": "auto"})
        assert "LIVE_AUTO" in str(c.get("/connect").text)
        assert c.get("/api/automation?account=live").json()["settings"]["levels"]["dte"] == (
            "propose"
        )


def test_settings_tabs_show_every_setting_with_its_default(settings):
    """Each setting is on the page once, in a tab, with a default for the change markers."""
    import re

    from app import scanner as scan_mod
    from app.automation import RULES, SETTING_FIELDS
    from app.preferences import BAND_FIELDS, RULE_FIELDS

    store = MemoryStore()
    with make_client(settings, FakeSchwab(connected=True), store=store) as c:
        login(c)
        page = c.get("/connect").text
        tabs = re.findall(r'class="tab-panel" data-tab="([a-z]+)"', page)
        assert tabs == ["rules", "portfolio", "automation", "scanner", "watchlist", "connection"]
        keys = (
            [("r", f[0]) for f in RULE_FIELDS + BAND_FIELDS]
            + [("a", f[0]) for f in SETTING_FIELDS]
            + [("s", f[0]) for f in scan_mod.FIELDS]
        )
        for prefix, key in keys:
            tag = re.search(rf'<(?:input|select) id="{prefix}-{key}"[^>]*>', page)
            assert tag and "data-default=" in tag.group(0), key
        for rule in RULES:
            assert f'id="a-{rule}"' in page
        # The Portfolio tab's form leaves the Rules tab's checkboxes alone.
        c.post("/rules", data={"block_over_ceiling": "on", "notify_flags": "on"})
        r = c.post("/rules", data={"_form": "targets", "delta_bias": "neutral", "theta_max": "0.4"})
        assert r.headers["location"].endswith("#targets")
        saved = store.settings["rules"]
        assert saved["notify_flags"] and saved["block_over_ceiling"] and saved["theta_max"] == 0.004


def test_home_screen_icon_at_the_standard_addresses_without_sign_in(settings, tmp_path):
    static = tmp_path / "static"
    (static / "assets" / "icons").mkdir(parents=True)
    (static / "index.html").write_text("<div id=root></div>")
    for name in ("apple-touch-icon.png", "icon-32.png"):
        (static / "assets" / "icons" / name).write_bytes(b"\x89PNG fake")
    with make_client(settings, FakeSchwab(), static_dir=static) as c:
        for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/favicon.ico"):
            r = c.get(path)
            assert r.status_code == 200 and r.headers["content-type"] == "image/png", path
        assert c.get("/assets/icons/apple-touch-icon.png").status_code == 200
        login = c.get("/login").text
        assert "viewport-fit=cover" in login and 'apple-touch-icon" href=' in login


def test_settings_written_into_the_explanations_save_as_they_are(settings):
    """The scanner's and automanage's step lists are their forms: posting them unchanged
    keeps every setting, and a value changed in the text is saved."""
    import re

    from app import scanner as scan_mod
    from app.automation import DEFAULT_SETTINGS, SETTINGS_KEY

    store = MemoryStore()
    with make_client(settings, FakeSchwab(connected=True), store=store) as c:
        login(c)
        page = c.get("/connect").text

        def form_data(action):
            body = page[page.index(f'action="{action}"') :]
            body = body[: body.index("</form>")]
            data = {}
            for tag in re.findall(r"<input[^>]*>|<select[^>]*>.*?</select>", body, re.S):
                name = re.search(r'name="([^"]+)"', tag).group(1)
                if tag.startswith("<select"):
                    data[name] = re.search(r'<option value="([^"]*)" selected', tag).group(1)
                elif 'type="checkbox"' in tag:
                    if " checked" in tag:
                        data[name] = "on"
                else:
                    data[name] = re.search(r'value="([^"]*)"', tag).group(1)
            return data

        scan = form_data("/scanner/settings")
        assert scan["_form"] == "scanner" and scan["min_ivr"] == "30"
        c.post("/scanner/settings", data={**scan, "min_ivr": "35"})
        saved = scan_mod.merged(store.settings.get(scan_mod.SETTINGS_KEY))
        assert saved["min_ivr"] == 35
        for k, v in scan_mod.DEFAULTS.items():
            if k != "min_ivr":
                assert saved[k] == (pytest.approx(v, abs=1e-4) if isinstance(v, float) else v), k
        auto = form_data("/automation/settings")
        c.post("/automation/settings", data=auto)
        assert store.settings[SETTINGS_KEY] == DEFAULT_SETTINGS
