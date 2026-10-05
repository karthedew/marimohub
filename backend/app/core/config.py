from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_MAX_PORT = 65535
# A dotted DNS name such as a Google Workspace domain ("example.com").
_LABEL = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})+$")
# OpenID Connect Core 1.0 §3.1.2.1 `prompt` values; "none" stands alone.
_OIDC_PROMPT_VALUES = frozenset({"none", "login", "consent", "select_account"})

GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_PROVIDER_SLUG = "google"


class SessionBackend(StrEnum):
    """Which `SessionManager` implementation `get_session_manager()` selects."""

    SUBPROCESS = "subprocess"
    KUBE = "kube"


def _blank_to_none(value: object) -> object:
    """Treat an empty or whitespace-only string as unset, else strip it."""
    if isinstance(value, str):
        return value.strip() or None
    return value


def _check_http_base_url(name: str, url: AnyHttpUrl) -> None:
    """Reject a base URL that cannot have paths appended to it, or that names a user."""
    if url.query is not None or url.fragment is not None:
        raise ValueError(f"{name} must not contain a query or fragment")
    if url.username is not None or url.password is not None:
        raise ValueError(f"{name} must not contain user information")


class OIDCProvider(BaseModel):
    """One configured external identity provider, an element of `Settings.OIDC_PROVIDERS`.

    The redirect URI is not stored here — it is derived from `PUBLIC_API_URL`
    so it has a single source.
    """

    # Validation errors would otherwise echo the input, client_secret included.
    model_config = ConfigDict(hide_input_in_errors=True)

    kind: Literal["google", "oidc", "saml"]
    slug: str
    display_name: str
    # Kept byte-for-byte as configured rather than as a URL type: OpenID
    # Connect compares issuers as exact strings (the discovery document's
    # `issuer`, the id_token's `iss`, the RFC 9207 `iss` response parameter),
    # and URL normalization (e.g. a trailing slash) would break every one.
    issuer: str
    client_id: str
    client_secret: str
    scopes: list[str] = ["openid", "email", "profile"]
    trusted_email_linking: bool = False
    # Google's `hd`: only accounts of this hosted domain may sign in. It is
    # also sent as the `hd` authorize parameter, which only tunes Google's
    # account chooser; the id_token's `hd` claim is what is enforced.
    hosted_domain: str | None = None
    # The OIDC `prompt` authorize parameter. Defaults to "select_account" for
    # kind "google" (signing out of MarimoHub does not sign out of Google, so
    # without it the next login silently reuses the same Google account);
    # unset otherwise. An explicit null or "" disables it.
    prompt: str | None = None

    @field_validator("issuer")
    @classmethod
    def _validate_issuer(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("issuer must not have surrounding whitespace")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("issuer must be an absolute http(s) URL")
        if parts.query or parts.fragment or "?" in value or "#" in value:
            raise ValueError("issuer must not contain a query or fragment")
        if parts.username is not None or parts.password is not None:
            raise ValueError("issuer must not contain user information")
        AnyHttpUrl(value)  # full URL syntax (host, port); the original string is what is kept
        return value

    @field_validator("scopes")
    @classmethod
    def _require_openid_scope(cls, value: list[str]) -> list[str]:
        if "openid" not in value:
            raise ValueError("scopes must include 'openid' (no id_token is issued without it)")
        return value

    @field_validator("hosted_domain", "prompt", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("hosted_domain")
    @classmethod
    def _validate_hosted_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        domain = value.lower()
        if not _DOMAIN_RE.match(domain):
            raise ValueError(f"hosted_domain {value!r} must be a DNS domain such as example.com")
        return domain

    @field_validator("prompt")
    @classmethod
    def _validate_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        values = value.split()
        if not set(values) <= _OIDC_PROMPT_VALUES or ("none" in values and len(values) > 1):
            raise ValueError(
                "prompt must be space-separated values from none, login, consent and "
                "select_account, with none on its own"
            )
        return " ".join(values)

    @model_validator(mode="after")
    def _default_google_prompt(self) -> OIDCProvider:
        if self.kind == "google" and "prompt" not in self.model_fields_set:
            self.prompt = "select_account"
        return self

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
    # External base URL of this API; OIDC redirect URIs are derived from it.
    PUBLIC_API_URL: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    # External base URL of the SPA, where an OIDC login ends
    # ({PUBLIC_APP_URL}/auth/callback, or /auth/login with an error). Unset
    # means the SPA shares the API's origin (an ingress routing /api to the
    # backend), so PUBLIC_API_URL is used.
    PUBLIC_APP_URL: AnyHttpUrl | None = None
    OIDC_PROVIDERS: list[OIDCProvider] = Field(default_factory=list)
    # Shortcut for Google sign-in: setting both appends the provider
    # {kind/slug "google", issuer https://accounts.google.com} to
    # OIDC_PROVIDERS, so no JSON is needed. Empty strings count as unset.
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_HOSTED_DOMAIN: str | None = None
    # Optional egress proxy for the backend's own calls to identity providers
    # (discovery, token exchange, signing keys) -- never the process-wide
    # HTTP(S)_PROXY, which would also capture in-cluster Runtime traffic.
    OIDC_HTTP_PROXY_URL: AnyHttpUrl | None = None

    # ── workspace lifecycle ──────────────────────────────────────────────────
    WORKSPACE_ARCHIVE_RETENTION_DAYS: int = 30

    # ── session / runtime (backend selector + settings for both backends) ────
    SESSION_BACKEND: SessionBackend = SessionBackend.SUBPROCESS
    SESSION_READY_TIMEOUT_SECONDS: float = 30.0
    # How often a proxied HTTP stream or open WebSocket re-signals activity
    # for the runtime it is attached to, independent of request/frame edges.
    # Mirrors the chart's forthcoming `activitySignalIntervalSeconds`; must
    # stay comfortably under the operator's `MinIdleTimeoutSeconds` (30s) so a
    # throttled signal can never be mistaken for idleness.
    ACTIVITY_SIGNAL_INTERVAL_SECONDS: float = 10.0
    # kube-only below — inert under SESSION_BACKEND=subprocess
    SESSION_NAMESPACE: str = "marimohub-sessions"
    SESSION_SERVICE_DNS_SUFFIX: str = "svc"
    SESSION_SERVICE_PORT: int = 8080
    SESSION_RUNTIME_IMAGE: str | None = None
    # Bounds how long a foreground CR delete waits for NotFound before giving
    # up; stop/redeploy/archive all wait outside a database lock, so this must
    # be generous but still finite. It must also outlast the Runtime Pod's
    # 30s termination grace period (the operator's
    # terminationGracePeriodSeconds), or a Runtime that uses its whole grace
    # period is reported as a failed stop even though deletion completes.
    SESSION_DELETE_TIMEOUT_SECONDS: float = 45.0

    # ── notebook source storage ───────────────────────────────────────────────
    NOTEBOOK_STORAGE_BACKEND: Literal["postgres"] = "postgres"

    # ── database TLS ──────────────────────────────────────────────────────────
    # Verified TLS to PostgreSQL: unset means the driver's own default (no
    # certificate verification) applies, which is only acceptable outside the
    # OpenShift profile. When set, every engine this process creates
    # (application and Alembic migrations alike) verifies the server
    # certificate against this CA rather than trusting the ambient system
    # store, since an external database's CA is never one this image ships.
    DATABASE_CA_FILE: str | None = None

    # ── GitLab notebook import ────────────────────────────────────────────────
    # Fails closed: importing from an arbitrary URL is disabled unless an
    # administrator both enables it and names a non-empty host allowlist.
    # GITLAB_IMPORT_PROXY_URL is optional -- some deployments bound egress to
    # the allowlisted hosts through NetworkPolicy CIDRs alone rather than an
    # actual HTTP(S) proxy.
    GITLAB_IMPORT_ENABLED: bool = False
    GITLAB_IMPORT_PROXY_URL: AnyHttpUrl | None = None
    GITLAB_IMPORT_ALLOWED_HOSTS: list[str] = Field(default_factory=list)

    # hide_input_in_errors: a startup validation error must not print secrets.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    @field_validator(
        "PUBLIC_APP_URL",
        "OIDC_HTTP_PROXY_URL",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_HOSTED_DOMAIN",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return _blank_to_none(value)

    @property
    def public_api_base_url(self) -> str:
        """Return `PUBLIC_API_URL` without a trailing slash, ready for path joins."""
        return str(self.PUBLIC_API_URL).rstrip("/")

    @property
    def public_app_base_url(self) -> str:
        """Return the SPA base URL (`PUBLIC_APP_URL`, else `PUBLIC_API_URL`), no trailing slash."""
        return str(self.PUBLIC_APP_URL or self.PUBLIC_API_URL).rstrip("/")

    def _validate_positive_numerics(self) -> None:
        if self.SESSION_READY_TIMEOUT_SECONDS <= 0:
            raise ValueError("SESSION_READY_TIMEOUT_SECONDS must be > 0")
        if self.WORKSPACE_ARCHIVE_RETENTION_DAYS <= 0:
            raise ValueError("WORKSPACE_ARCHIVE_RETENTION_DAYS must be > 0")
        if self.ACTIVITY_SIGNAL_INTERVAL_SECONDS <= 0:
            raise ValueError("ACTIVITY_SIGNAL_INTERVAL_SECONDS must be > 0")
        if self.SESSION_DELETE_TIMEOUT_SECONDS <= 0:
            raise ValueError("SESSION_DELETE_TIMEOUT_SECONDS must be > 0")

    def _validate_public_urls(self) -> None:
        _check_http_base_url("PUBLIC_API_URL", self.PUBLIC_API_URL)
        if self.PUBLIC_APP_URL is not None:
            _check_http_base_url("PUBLIC_APP_URL", self.PUBLIC_APP_URL)

    def _apply_google_shortcut(self) -> None:
        client_id, client_secret = self.GOOGLE_CLIENT_ID, self.GOOGLE_CLIENT_SECRET
        if client_id is None and client_secret is None:
            if self.GOOGLE_HOSTED_DOMAIN is not None:
                raise ValueError(
                    "GOOGLE_HOSTED_DOMAIN requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
                )
            return
        if client_id is None or client_secret is None:
            raise ValueError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set together")
        if any(provider.slug == GOOGLE_PROVIDER_SLUG for provider in self.OIDC_PROVIDERS):
            raise ValueError(
                "OIDC provider slug 'google' is defined both in OIDC_PROVIDERS and by "
                "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET; configure it in only one place"
            )
        google = OIDCProvider(
            kind="google",
            slug=GOOGLE_PROVIDER_SLUG,
            display_name="Google",
            issuer=GOOGLE_ISSUER,
            client_id=client_id,
            client_secret=client_secret,
            hosted_domain=self.GOOGLE_HOSTED_DOMAIN,
        )
        self.OIDC_PROVIDERS = [*self.OIDC_PROVIDERS, google]

    def _validate_oidc_providers(self) -> None:
        slugs = [provider.slug for provider in self.OIDC_PROVIDERS]
        if len(set(slugs)) != len(slugs):
            raise ValueError("OIDC_PROVIDERS slugs must be unique")
        for provider in self.OIDC_PROVIDERS:
            if not _DNS_LABEL_RE.match(provider.slug):
                raise ValueError(f"OIDC provider slug {provider.slug!r} must be a DNS label")

    def _validate_gitlab_import(self) -> None:
        if self.GITLAB_IMPORT_ENABLED and not self.GITLAB_IMPORT_ALLOWED_HOSTS:
            raise ValueError(
                "GITLAB_IMPORT_ALLOWED_HOSTS must be non-empty when GITLAB_IMPORT_ENABLED"
            )

    def _validate_kube_backend(self) -> None:
        if self.SESSION_BACKEND is not SessionBackend.KUBE:
            return
        if not _DNS_LABEL_RE.match(self.SESSION_NAMESPACE):
            raise ValueError("SESSION_NAMESPACE must be a DNS-1123 label when SESSION_BACKEND=kube")
        if not 1 <= self.SESSION_SERVICE_PORT <= _MAX_PORT:
            raise ValueError("SESSION_SERVICE_PORT out of range")
        if not self.SESSION_RUNTIME_IMAGE:
            # The CRD requires spec.image on every Runtime, edit/run and
            # deploy alike; a kube backend with nothing configured to put
            # there would build an admission-rejected CR on its very first
            # Session, a much worse place to discover this than startup.
            raise ValueError("SESSION_RUNTIME_IMAGE is required when SESSION_BACKEND=kube")

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        self._validate_positive_numerics()
        self._validate_public_urls()
        self._apply_google_shortcut()
        self._validate_oidc_providers()
        self._validate_gitlab_import()
        self._validate_kube_backend()
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings, loading them on first use."""
    # pydantic-settings populates required fields from the environment/.env at
    # runtime, which the type checker cannot see.
    return Settings()  # pyright: ignore[reportCallIssue]
