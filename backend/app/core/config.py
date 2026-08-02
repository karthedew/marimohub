from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
import re
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_MAX_PORT = 65535


class SessionBackend(StrEnum):
    """Which `SessionManager` implementation `get_session_manager()` selects."""

    SUBPROCESS = "subprocess"
    KUBE = "kube"


class OIDCProvider(BaseModel):
    """One configured external identity provider, an element of `Settings.OIDC_PROVIDERS`.

    The redirect URI is not stored here — it is derived from `PUBLIC_API_URL`
    so it has a single source.
    """

    kind: Literal["google", "oidc", "saml"]
    slug: str
    display_name: str
    issuer: AnyHttpUrl
    client_id: str
    client_secret: str
    scopes: list[str] = ["openid", "email", "profile"]
    trusted_email_linking: bool = False

    @property
    def provider_value(self) -> str:
        """Return the `identities.provider` string this config resolves to."""
        return "google" if self.kind == "google" else f"{self.kind}:{self.slug}"


class Settings(BaseSettings):
    """Application settings loaded from the environment and `.env`.

    Field defaults below are the single source of truth for every non-required key.
    """

    # ── database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str

    # ── auth / identity ──────────────────────────────────────────────────────
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    PUBLIC_API_URL: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    OIDC_PROVIDERS: list[OIDCProvider] = Field(default_factory=list)

    # ── workspace lifecycle ──────────────────────────────────────────────────
    WORKSPACE_ARCHIVE_RETENTION_DAYS: int = 30

    # ── session / runtime (backend selector + settings for both backends) ────
    SESSION_BACKEND: SessionBackend = SessionBackend.SUBPROCESS
    SESSION_READY_TIMEOUT_SECONDS: float = 30.0
    SESSION_TOKEN_TTL_SECONDS: int = 86_400
    # kube-only below — inert under SESSION_BACKEND=subprocess
    SESSION_NAMESPACE: str = "marimohub-sessions"
    SESSION_SERVICE_DNS_SUFFIX: str = "svc"
    SESSION_SERVICE_PORT: int = 8080
    SESSION_RUNTIME_IMAGE: str | None = None

    # ── notebook source storage ───────────────────────────────────────────────
    NOTEBOOK_STORAGE_BACKEND: Literal["postgres"] = "postgres"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def _validate_positive_numerics(self) -> None:
        if self.SESSION_READY_TIMEOUT_SECONDS <= 0:
            raise ValueError("SESSION_READY_TIMEOUT_SECONDS must be > 0")
        if self.SESSION_TOKEN_TTL_SECONDS <= 0:
            raise ValueError("SESSION_TOKEN_TTL_SECONDS must be > 0")
        if self.WORKSPACE_ARCHIVE_RETENTION_DAYS <= 0:
            raise ValueError("WORKSPACE_ARCHIVE_RETENTION_DAYS must be > 0")

    def _validate_oidc_providers(self) -> None:
        slugs = [provider.slug for provider in self.OIDC_PROVIDERS]
        if len(set(slugs)) != len(slugs):
            raise ValueError("OIDC_PROVIDERS slugs must be unique")
        for provider in self.OIDC_PROVIDERS:
            if not _DNS_LABEL_RE.match(provider.slug):
                raise ValueError(f"OIDC provider slug {provider.slug!r} must be a DNS label")

    def _validate_kube_backend(self) -> None:
        if self.SESSION_BACKEND is not SessionBackend.KUBE:
            return
        if not _DNS_LABEL_RE.match(self.SESSION_NAMESPACE):
            raise ValueError("SESSION_NAMESPACE must be a DNS-1123 label when SESSION_BACKEND=kube")
        if not 1 <= self.SESSION_SERVICE_PORT <= _MAX_PORT:
            raise ValueError("SESSION_SERVICE_PORT out of range")

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        self._validate_positive_numerics()
        self._validate_oidc_providers()
        self._validate_kube_backend()
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings, loading them on first use."""
    # pydantic-settings populates required fields from the environment/.env at
    # runtime, which the type checker cannot see.
    return Settings()  # pyright: ignore[reportCallIssue]
