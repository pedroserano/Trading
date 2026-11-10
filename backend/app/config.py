from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Protects this app's own pages. Not the Schwab password.
    app_password: str = Field(min_length=12)
    # Signs session cookies and encrypts the Schwab token at rest.
    secret_key: str = Field(min_length=32)

    schwab_app_key: str
    schwab_app_secret: str
    # Must match the callback registered on the Schwab app character for character.
    schwab_callback_url: str = "https://127.0.0.1:8182"

    data_dir: Path = Path("/data")

    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "trading"
    db_user: str = "trading"
    db_password: str = ""

    # Session cookies are HTTPS-only. Turn off only for local development over plain HTTP.
    cookie_secure: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
