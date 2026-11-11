from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Protects this app's own pages. Not the Schwab password.
    app_password: str = Field(min_length=12)
    # Signs session cookies and encrypts the Schwab token at rest.
    secret_key: str = Field(min_length=32)

    # Simulated account and prices instead of Schwab. For previews and development.
    demo_mode: bool = False

    schwab_app_key: str = ""
    schwab_app_secret: str = ""
    # Must match a callback registered on the Schwab app character for character.
    schwab_callback_url: str = "https://127.0.0.1:8182"

    # The address the app is opened at, used for links in notifications.
    app_url: str = ""

    data_dir: Path = Path("/data")

    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "trading"
    db_user: str = "trading"
    # Postgres and the app can share one env file, so accept the Postgres variable name too.
    db_password: str = Field("", validation_alias=AliasChoices("DB_PASSWORD", "POSTGRES_PASSWORD"))

    # Session cookies are HTTPS-only. Turn off only for local development over plain HTTP.
    cookie_secure: bool = True

    # Pushover notifications. Leave empty to disable.
    pushover_user_key: str = ""
    pushover_app_token: str = ""
    # Weekly Schwab sign-in reminder, US Eastern time. Monday is 0, Sunday is 6.
    reminder_weekday: int = Field(6, ge=0, le=6)
    reminder_hour: int = Field(18, ge=0, le=23)

    # Underlyings whose IV is recorded daily even when no position is held.
    watchlist: str = "SPY"

    quote_interval_s: float = Field(5.0, ge=2.0)
    positions_interval_s: float = Field(60.0, ge=15.0)

    @model_validator(mode="after")
    def _schwab_keys_unless_demo(self):
        if not self.demo_mode and not (self.schwab_app_key and self.schwab_app_secret):
            raise ValueError("SCHWAB_APP_KEY and SCHWAB_APP_SECRET are required unless DEMO_MODE")
        return self

    @property
    def watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    @property
    def pushover_enabled(self) -> bool:
        return bool(self.pushover_user_key and self.pushover_app_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
