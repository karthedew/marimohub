from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from the environment and ``.env``."""

    DATABASE_URL: str
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    IDLE_TIMEOUT_MINUTES: int = 10
    MAX_CONCURRENT_SESSIONS: int = 10
    MARIMO_PORT_RANGE: str = "9000-9099"
    MARIMO_READY_TIMEOUT_SECONDS: float = 30.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings, loading them on first use."""
    # pydantic-settings populates required fields from the environment/.env at
    # runtime, which the type checker cannot see.
    return Settings()  # pyright: ignore[reportCallIssue]
