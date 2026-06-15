from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    IDLE_TIMEOUT_MINUTES: int = 10
    MAX_CONCURRENT_SESSIONS: int = 10
    MARIMO_PORT_RANGE: str = "9000-9099"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
