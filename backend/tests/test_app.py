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
        return ConnectionStatus(connected=self.connected, token_age_s=86400.0)

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


def make_client(settings, schwab):
    return TestClient(create_app(settings, schwab), follow_redirects=False)


def login(client):
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 303


def test_healthz_is_open(settings):
    with make_client(settings, FakeSchwab()) as c:
        assert c.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("path", ["/", "/schwab/authorize"])
def test_pages_require_login(settings, path):
    with make_client(settings, FakeSchwab()) as c:
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("path", ["/api/status", "/api/accounts"])
def test_api_requires_login(settings, path):
    with make_client(settings, FakeSchwab(connected=True)) as c:
        assert c.get(path).status_code == 401


def test_posts_require_login(settings):
    schwab = FakeSchwab(connected=True)
    with make_client(settings, schwab) as c:
        assert c.post("/schwab/disconnect").status_code == 303
        assert c.post("/schwab/complete", data={"received_url": "x"}).status_code == 303
        assert schwab.connected and schwab.completed_with is None


def test_wrong_password_and_lockout(settings):
    with make_client(settings, FakeSchwab()) as c:
        for _ in range(MAX_LOGIN_FAILURES):
            assert c.post("/login", data={"password": "nope"}).status_code == 401
        # Locked now: even the right password is refused.
        r = c.post("/login", data={"password": PASSWORD})
        assert r.status_code == 401 and "Too many attempts" in r.text


def test_home_shows_masked_account_and_balances(settings):
    with make_client(settings, FakeSchwab(connected=True)) as c:
        login(c)
        r = c.get("/")
        assert r.status_code == 200
        assert "…6789" in r.text and "51,234.50" in r.text
        assert "Database unreachable" in r.text  # no database in tests


def test_connect_flow(settings):
    schwab = FakeSchwab()
    with make_client(settings, schwab) as c:
        login(c)
        assert "Schwab not connected" in c.get("/").text
        r = c.get("/schwab/authorize")
        assert r.headers["location"].startswith("https://api.schwabapi.com/")
        url = "https://127.0.0.1:8182/?code=abc&session=def"
        assert c.post("/schwab/complete", data={"received_url": url}).status_code == 303
        assert schwab.completed_with == url
        assert "Schwab connected" in c.get("/").text


def test_connect_failure_is_shown_once(settings):
    schwab = FakeSchwab()
    schwab.fail_with = "Schwab rejected the sign-in"
    with make_client(settings, schwab) as c:
        login(c)
        c.post("/schwab/complete", data={"received_url": "x"})
        assert "Schwab rejected the sign-in" in c.get("/").text
        assert "Schwab rejected the sign-in" not in c.get("/").text


def test_api_accounts(settings):
    with make_client(settings, FakeSchwab(connected=True)) as c:
        login(c)
        assert c.get("/api/accounts").json()[0]["last4"] == "6789"
        status = c.get("/api/status").json()
        assert status["schwab"] == {"connected": True, "expires_in_days": 6.0, "detail": None}
        assert status["database"]["ok"] is False


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
