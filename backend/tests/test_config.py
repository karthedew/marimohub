from collections.abc import Callable

from pydantic import ValidationError
import pytest

from app.core.config import OIDCProvider, SessionBackend, Settings

# Fake values routed through module-level constants, never string literals at
# the sensitive call sites (matches the convention in test_auth.py).
_SECRET_KEY_VALUE = "test-secret-with-at-least-32-bytes"  # noqa: S105
_CLIENT_SECRET_VALUE = "client-secret"  # noqa: S105

_REQUIRED = {"DATABASE_URL": "postgresql+asyncpg://molab:molab@localhost:5432/molab_test"}


def _settings(**overrides: object) -> Settings:
    # The generic **overrides: object can't line up with Settings' literal-typed
    # fields (e.g. NOTEBOOK_STORAGE_BACKEND) for the type checker; every value
    # passed in tests below is a valid Settings field value at runtime.
    return Settings(SECRET_KEY=_SECRET_KEY_VALUE, **_REQUIRED, **overrides)  # ty: ignore[invalid-argument-type]


def _oidc_provider(slug: str) -> OIDCProvider:
    return OIDCProvider(
        kind="oidc",
        slug=slug,
        display_name="Test Provider",
        issuer="https://idp.example.com",
        client_id="client-id",
        client_secret=_CLIENT_SECRET_VALUE,
    )


def test_kube_backend_requires_dns_label_namespace() -> None:
    # SESSION_NAMESPACE always has a working in-cluster default, so there is no
    # "unset" failure mode; the contract is conditional DNS-label correctness
    # once SESSION_BACKEND=kube is selected.
    with pytest.raises(ValidationError, match="SESSION_NAMESPACE"):
        _settings(SESSION_BACKEND=SessionBackend.KUBE, SESSION_NAMESPACE="Not_A_Label")


def test_subprocess_backend_never_validates_kube_only_keys() -> None:
    settings = _settings(
        SESSION_BACKEND=SessionBackend.SUBPROCESS,
        SESSION_NAMESPACE="Not_A_Label",
        SESSION_SERVICE_PORT=999_999,
    )

    assert settings.SESSION_NAMESPACE == "Not_A_Label"


def test_kube_backend_requires_service_port_in_range() -> None:
    with pytest.raises(ValidationError, match="SESSION_SERVICE_PORT"):
        _settings(SESSION_BACKEND=SessionBackend.KUBE, SESSION_SERVICE_PORT=70_000)


def test_kube_backend_requires_runtime_image() -> None:
    # The CRD requires spec.image on every Runtime; an unset image would only
    # surface as an admission rejection on the first Session otherwise.
    with pytest.raises(ValidationError, match="SESSION_RUNTIME_IMAGE"):
        _settings(SESSION_BACKEND=SessionBackend.KUBE)


def test_kube_backend_accepts_a_configured_runtime_image() -> None:
    settings = _settings(
        SESSION_BACKEND=SessionBackend.KUBE,
        SESSION_RUNTIME_IMAGE="registry.example/marimo-runtime@sha256:" + "0" * 64,
    )

    assert settings.SESSION_RUNTIME_IMAGE is not None


def test_oidc_provider_slug_must_be_a_dns_label() -> None:
    with pytest.raises(ValidationError, match="DNS label"):
        _settings(OIDC_PROVIDERS=[_oidc_provider("Not A Slug")])


def test_oidc_provider_slugs_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="unique"):
        _settings(OIDC_PROVIDERS=[_oidc_provider("okta"), _oidc_provider("okta")])


@pytest.mark.parametrize(
    "override",
    [
        lambda: {"SESSION_READY_TIMEOUT_SECONDS": 0},
        lambda: {"WORKSPACE_ARCHIVE_RETENTION_DAYS": 0},
    ],
)
def test_non_positive_numerics_are_rejected(override: Callable[[], dict[str, object]]) -> None:
    with pytest.raises(ValidationError):
        _settings(**override())


def test_google_provider_value_is_bare_google() -> None:
    provider = OIDCProvider(
        kind="google",
        slug="google",
        display_name="Google",
        issuer="https://accounts.google.com",
        client_id="id",
        client_secret=_CLIENT_SECRET_VALUE,
    )

    assert provider.provider_value == "google"


def test_non_google_provider_value_is_kind_colon_slug() -> None:
    provider = _oidc_provider("okta")

    assert provider.provider_value == "oidc:okta"


def test_gitlab_import_enabled_requires_nonempty_allowlist() -> None:
    with pytest.raises(ValidationError, match="GITLAB_IMPORT_ALLOWED_HOSTS"):
        _settings(GITLAB_IMPORT_ENABLED=True, GITLAB_IMPORT_ALLOWED_HOSTS=[])


def test_gitlab_import_disabled_never_validates_allowlist() -> None:
    settings = _settings(GITLAB_IMPORT_ENABLED=False, GITLAB_IMPORT_ALLOWED_HOSTS=[])

    assert settings.GITLAB_IMPORT_ALLOWED_HOSTS == []


def test_gitlab_import_enabled_accepts_a_configured_allowlist() -> None:
    settings = _settings(
        GITLAB_IMPORT_ENABLED=True, GITLAB_IMPORT_ALLOWED_HOSTS=["gitlab.example.com"]
    )

    assert settings.GITLAB_IMPORT_ALLOWED_HOSTS == ["gitlab.example.com"]
