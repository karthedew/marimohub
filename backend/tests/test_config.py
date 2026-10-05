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


@pytest.mark.parametrize(
    "issuer",
    [
        "https://accounts.google.com",
        "http://localhost:8111",
        "https://keycloak.example.com/realms/main",
        "https://keycloak.example.com/realms/main/",
    ],
)
def test_oidc_issuer_is_kept_exactly_as_configured(issuer: str) -> None:
    # OIDC compares issuers as exact strings; URL normalization (a trailing
    # slash on a bare host) made every Google id_token fail its `iss` check.
    provider = OIDCProvider(
        kind="oidc",
        slug="idp",
        display_name="IdP",
        issuer=issuer,
        client_id="client-id",
        client_secret=_CLIENT_SECRET_VALUE,
    )

    assert provider.issuer == issuer


@pytest.mark.parametrize(
    ("issuer", "message"),
    [
        ("accounts.google.com", "absolute http"),
        ("ftp://idp.example.com", "absolute http"),
        (" https://idp.example.com", "whitespace"),
        ("https://idp.example.com?tenant=a", "query or fragment"),
        ("https://idp.example.com#top", "query or fragment"),
        ("https://user:pw@idp.example.com", "user information"),
        ("https://idp.example.com:99999", "port"),
    ],
)
def test_oidc_issuer_must_be_a_plain_http_url(issuer: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        OIDCProvider(
            kind="oidc",
            slug="idp",
            display_name="IdP",
            issuer=issuer,
            client_id="client-id",
            client_secret=_CLIENT_SECRET_VALUE,
        )


def test_e2e_contract_provider_parses_from_json() -> None:
    settings = _settings(
        OIDC_PROVIDERS=[
            {
                "kind": "oidc",
                "slug": "e2e",
                "display_name": "Test IdP",
                "issuer": "http://localhost:8111",
                "client_id": "e2e-client",
                "client_secret": "e2e-secret",
            }
        ]
    )

    (provider,) = settings.OIDC_PROVIDERS
    assert provider.issuer == "http://localhost:8111"
    assert provider.prompt is None
    assert provider.hosted_domain is None
    assert provider.trusted_email_linking is False


def test_google_provider_prompts_for_an_account_by_default() -> None:
    provider = OIDCProvider(
        kind="google",
        slug="google",
        display_name="Google",
        issuer="https://accounts.google.com",
        client_id="id",
        client_secret=_CLIENT_SECRET_VALUE,
    )

    assert provider.prompt == "select_account"


@pytest.mark.parametrize("prompt", [None, ""])
def test_google_prompt_can_be_switched_off(prompt: str | None) -> None:
    provider = OIDCProvider(
        kind="google",
        slug="google",
        display_name="Google",
        issuer="https://accounts.google.com",
        client_id="id",
        client_secret=_CLIENT_SECRET_VALUE,
        prompt=prompt,
    )

    assert provider.prompt is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"prompt": "none login"}, "prompt"),
        ({"prompt": "always"}, "prompt"),
        ({"scopes": ["email", "profile"]}, "openid"),
        ({"hosted_domain": "localhost"}, "hosted_domain"),
        ({"hosted_domain": "exa mple.com"}, "hosted_domain"),
    ],
)
def test_oidc_provider_rejects_invalid_options(overrides: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "kind": "oidc",
        "slug": "idp",
        "display_name": "IdP",
        "issuer": "https://idp.example.com",
        "client_id": "client-id",
        "client_secret": _CLIENT_SECRET_VALUE,
    }
    values.update(overrides)

    with pytest.raises(ValidationError, match=message):
        OIDCProvider.model_validate(values)


def test_oidc_provider_normalizes_hosted_domain_and_prompt() -> None:
    provider = OIDCProvider.model_validate(
        {
            "kind": "google",
            "slug": "google",
            "display_name": "Google",
            "issuer": "https://accounts.google.com",
            "client_id": "id",
            "client_secret": _CLIENT_SECRET_VALUE,
            "hosted_domain": " Example.COM ",
            "prompt": "consent  select_account",
        }
    )

    assert provider.hosted_domain == "example.com"
    assert provider.prompt == "consent select_account"


def test_google_shortcut_appends_the_google_provider() -> None:
    settings = _settings(
        OIDC_PROVIDERS=[_oidc_provider("okta")],
        GOOGLE_CLIENT_ID=" google-client.apps.googleusercontent.com ",
        GOOGLE_CLIENT_SECRET=_CLIENT_SECRET_VALUE + "\n",
        GOOGLE_HOSTED_DOMAIN="Example.com",
    )

    okta, google = settings.OIDC_PROVIDERS
    assert okta.slug == "okta"
    assert google.model_dump() == {
        "kind": "google",
        "slug": "google",
        "display_name": "Google",
        "issuer": "https://accounts.google.com",
        "client_id": "google-client.apps.googleusercontent.com",
        "client_secret": _CLIENT_SECRET_VALUE,
        "scopes": ["openid", "email", "profile"],
        "trusted_email_linking": False,
        "hosted_domain": "example.com",
        "prompt": "select_account",
    }
    assert google.provider_value == "google"


@pytest.mark.parametrize("blank", ["", "  ", "\n"])
def test_google_shortcut_treats_empty_strings_as_unset(blank: str) -> None:
    settings = _settings(
        GOOGLE_CLIENT_ID=blank, GOOGLE_CLIENT_SECRET=blank, GOOGLE_HOSTED_DOMAIN=blank
    )

    assert settings.OIDC_PROVIDERS == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"GOOGLE_CLIENT_ID": "id"}, "must be set together"),
        ({"GOOGLE_CLIENT_SECRET": _CLIENT_SECRET_VALUE}, "must be set together"),
        ({"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": ""}, "must be set together"),
        ({"GOOGLE_HOSTED_DOMAIN": "example.com"}, "GOOGLE_HOSTED_DOMAIN requires"),
    ],
)
def test_google_shortcut_needs_both_credentials(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _settings(**overrides)


def test_google_slug_defined_both_ways_is_a_startup_error() -> None:
    with pytest.raises(ValidationError, match="defined both in OIDC_PROVIDERS and by GOOGLE"):
        _settings(
            OIDC_PROVIDERS=[_oidc_provider("google")],
            GOOGLE_CLIENT_ID="id",
            GOOGLE_CLIENT_SECRET=_CLIENT_SECRET_VALUE,
        )


def test_settings_validation_errors_do_not_echo_secrets() -> None:
    with pytest.raises(ValidationError) as caught:
        _settings(GOOGLE_CLIENT_SECRET=_CLIENT_SECRET_VALUE)

    assert _CLIENT_SECRET_VALUE not in str(caught.value)


def test_public_app_url_defaults_to_the_public_api_url() -> None:
    settings = _settings(PUBLIC_API_URL="https://hub.example.com/")

    assert settings.PUBLIC_APP_URL is None
    assert settings.public_api_base_url == "https://hub.example.com"
    assert settings.public_app_base_url == "https://hub.example.com"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_public_app_url_and_proxy_count_as_unset(blank: str) -> None:
    settings = _settings(PUBLIC_APP_URL=blank, OIDC_HTTP_PROXY_URL=blank)

    assert settings.PUBLIC_APP_URL is None
    assert settings.OIDC_HTTP_PROXY_URL is None


def test_explicit_public_app_url_is_used_for_the_spa() -> None:
    settings = _settings(
        PUBLIC_API_URL="http://localhost:8000", PUBLIC_APP_URL="http://localhost:5173"
    )

    assert settings.public_api_base_url == "http://localhost:8000"
    assert settings.public_app_base_url == "http://localhost:5173"


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("PUBLIC_APP_URL", "https://hub.example.com/?next=/"),
        ("PUBLIC_APP_URL", "https://hub.example.com/#x"),
        ("PUBLIC_API_URL", "https://hub.example.com/?a=b"),
        ("PUBLIC_APP_URL", "https://user:pw@hub.example.com"),
    ],
)
def test_public_urls_must_be_plain_base_urls(name: str, url: str) -> None:
    with pytest.raises(ValidationError, match=name):
        _settings(**{name: url})


def test_oidc_http_proxy_url_is_parsed() -> None:
    settings = _settings(OIDC_HTTP_PROXY_URL="http://proxy.internal:3128")

    assert str(settings.OIDC_HTTP_PROXY_URL) == "http://proxy.internal:3128/"


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
