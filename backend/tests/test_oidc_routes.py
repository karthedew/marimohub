"""The OIDC HTTP flow end to end: login redirect, callback, handoff exchange.

The real routes run the real `OIDCVerifier` against `FakeIdP` (from
`test_oidc_verifier`) through dependency overrides: `get_settings` supplies
the provider list and public URLs, `get_oidc_http_options` the fake's
transport. Nothing else is faked.
"""

from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta, tzinfo
import json
from typing import Self, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
from httpx import AsyncClient
import jwt
from pydantic import AnyHttpUrl
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_oidc_http_options
from app.core import security
from app.core.config import OIDCProvider, Settings, get_settings
from app.core.security import (
    PURPOSE_TOKEN_CLOCK_LEEWAY,
    TokenPurpose,
    create_access_token,
    create_purpose_token,
    decode_token,
)
from app.db.database import get_db
from app.main import DEV_SPA_ORIGIN, app, cors_allow_origins
from app.models import Identity, User
from app.services.auth_service import OIDCAuthService, OIDCClaims
from app.services.oidc_verifier import OIDCHttpOptions, OIDCMetadataCache
from test_auth import register_user
from test_notebooks import json_dict
from test_oidc_verifier import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIdP, s256

APP_URL = "http://localhost:5173"
API_URL = "http://localhost:8000"
# The SPA's PKCE pair: base64url of 32 random bytes, and its S256 challenge.
SPA_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
SPA_CHALLENGE = s256(SPA_VERIFIER)
HTTP_COOKIE = "marimohub_oidc"
HTTPS_COOKIE = "__Host-marimohub_oidc"
BEARER = "bearer"


def e2e_provider(**overrides: object) -> OIDCProvider:
    values: dict[str, object] = {
        "kind": "oidc",
        "slug": "e2e",
        "display_name": "Test IdP",
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    values.update(overrides)
    return OIDCProvider.model_validate(values)


def oidc_settings(
    *,
    providers: list[OIDCProvider] | None = None,
    public_api_url: str = API_URL,
    public_app_url: str | None = APP_URL,
    google_client_id: str | None = None,
    google_client_secret: str | None = None,
) -> Settings:
    base = get_settings()
    return Settings(
        DATABASE_URL=base.DATABASE_URL,
        SECRET_KEY=base.SECRET_KEY,
        PUBLIC_API_URL=AnyHttpUrl(public_api_url),
        PUBLIC_APP_URL=AnyHttpUrl(public_app_url) if public_app_url is not None else None,
        OIDC_PROVIDERS=providers if providers is not None else [e2e_provider()],
        GOOGLE_CLIENT_ID=google_client_id,
        GOOGLE_CLIENT_SECRET=google_client_secret,
        GOOGLE_HOSTED_DOMAIN=None,
    )


class OIDCHarness:
    """The API client plus a `FakeIdP` wired in through dependency overrides."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self.idp = FakeIdP()
        self.cache = OIDCMetadataCache()
        self.settings = self.configure()
        app.dependency_overrides[get_oidc_http_options] = lambda: OIDCHttpOptions(
            transport=self.idp.transport(), cache=self.cache
        )

    def configure(self, settings: Settings | None = None) -> Settings:
        self.settings = settings or oidc_settings()
        app.dependency_overrides[get_settings] = lambda: self.settings
        return self.settings

    async def start(
        self, slug: str = "e2e", challenge: str | None = SPA_CHALLENGE
    ) -> httpx.Response:
        params = {"challenge": challenge} if challenge is not None else {}
        response = await self.client.get(f"/api/auth/oidc/{slug}/login", params=params)
        # Cookies are always sent explicitly below, never from the client's jar.
        self.client.cookies.clear()
        return response

    async def callback(
        self, params: dict[str, str], cookie: str | None, slug: str = "e2e"
    ) -> httpx.Response:
        name = HTTPS_COOKIE if self.settings.PUBLIC_API_URL.scheme == "https" else HTTP_COOKIE
        headers = {"Cookie": f"{name}={cookie}"} if cookie is not None else {}
        return await self.client.get(
            f"/api/auth/oidc/{slug}/callback", params=params, headers=headers
        )

    async def sign_in(self) -> httpx.Response:
        """Run login, the provider's consent and the callback; return the callback response."""
        login = await self.start()
        return await self.callback(self.idp.authorize(location(login)), login_cookie(login))

    async def exchange(self, handoff: str, verifier: str = SPA_VERIFIER) -> httpx.Response:
        return await self.client.post(
            "/api/auth/oidc/exchange", json={"handoff": handoff, "verifier": verifier}
        )


@pytest_asyncio.fixture
async def oidc(api_client: AsyncClient) -> AsyncGenerator[OIDCHarness, None]:
    yield OIDCHarness(api_client)
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_oidc_http_options, None)


def location(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    return response.headers["location"]


def login_cookie(response: httpx.Response) -> str:
    (set_cookie,) = response.headers.get_list("set-cookie")
    return set_cookie.split(";", 1)[0].split("=", 1)[1]


def cookie_attributes(set_cookie: str) -> dict[str, str]:
    attributes = [part.strip() for part in set_cookie.split(";")]
    name, _, value = attributes[0].partition("=")
    parsed = {"name": name, "value": value}
    for attribute in attributes[1:]:
        key, _, attribute_value = attribute.partition("=")
        parsed[key.lower()] = attribute_value
    return parsed


def handoff_from(response: httpx.Response) -> str:
    target = location(response)
    assert target.startswith(f"{APP_URL}/auth/callback#handoff="), target
    return parse_qs(urlsplit(target).fragment)["handoff"][0]


def exchanged_user(response: httpx.Response) -> dict[str, object]:
    assert response.status_code == 200, response.text
    return cast("dict[str, object]", json_dict(response)["user"])


def assert_login_error(response: httpx.Response, code: str) -> None:
    assert location(response) == f"{APP_URL}/auth/login?error={code}"


def assert_callback_hygiene(response: httpx.Response, cookie_name: str = HTTP_COOKIE) -> None:
    """Every callback answer: 303, uncacheable, no referrer, login cookie deleted."""
    assert response.status_code == 303
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    (deletion,) = response.headers.get_list("set-cookie")
    attributes = cookie_attributes(deletion)
    assert attributes["name"] == cookie_name
    assert attributes["value"] in {"", '""'}
    assert attributes["max-age"] == "0"


# ── GET /api/auth/providers ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_providers_lists_slug_name_and_kind_but_never_secrets(oidc: OIDCHarness) -> None:
    google_secret = "google-secret-value"  # noqa: S105 -- a fake for the leak check
    oidc.configure(
        oidc_settings(
            google_client_id="google-client.apps.googleusercontent.com",
            google_client_secret=google_secret,
        )
    )

    response = await oidc.client.get("/api/auth/providers")

    assert response.status_code == 200
    assert response.json() == [
        {"slug": "e2e", "display_name": "Test IdP", "kind": "oidc"},
        {"slug": "google", "display_name": "Google", "kind": "google"},
    ]
    for secret_or_internal in (
        CLIENT_SECRET,
        google_secret,
        CLIENT_ID,
        "googleusercontent",
        ISSUER,
    ):
        assert secret_or_internal not in response.text


@pytest.mark.asyncio
async def test_providers_is_an_empty_list_when_none_are_configured(oidc: OIDCHarness) -> None:
    oidc.configure(oidc_settings(providers=[]))

    response = await oidc.client.get("/api/auth/providers")

    assert response.status_code == 200
    assert response.json() == []


# ── GET /api/auth/oidc/{slug}/login ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_redirects_to_the_provider_with_the_backends_own_pkce(
    oidc: OIDCHarness,
) -> None:
    response = await oidc.start()

    target = location(response)
    query = {key: values[0] for key, values in parse_qs(urlsplit(target).query).items()}
    assert target.startswith(f"{ISSUER}/authorize?")
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == f"{API_URL}/api/auth/oidc/e2e/callback"
    assert query["scope"] == "openid email profile"
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43
    assert query["code_challenge"] != SPA_CHALLENGE  # the SPA's challenge never reaches the IdP
    assert query["state"]
    assert query["nonce"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_login_cookie_on_http_is_path_scoped_httponly_lax(oidc: OIDCHarness) -> None:
    response = await oidc.start()

    (set_cookie,) = response.headers.get_list("set-cookie")
    attributes = cookie_attributes(set_cookie)
    assert attributes["name"] == HTTP_COOKIE
    assert attributes["path"] == "/api/auth/oidc/"
    assert attributes["max-age"] == "600"
    assert attributes["samesite"].lower() == "lax"
    assert "httponly" in attributes
    assert "secure" not in attributes


@pytest.mark.asyncio
async def test_login_cookie_on_https_is_host_prefixed_and_secure(oidc: OIDCHarness) -> None:
    oidc.configure(oidc_settings(public_api_url="https://hub.example.com"))

    response = await oidc.start()

    (set_cookie,) = response.headers.get_list("set-cookie")
    attributes = cookie_attributes(set_cookie)
    assert attributes["name"] == HTTPS_COOKIE
    assert attributes["path"] == "/"
    assert "secure" in attributes
    assert "httponly" in attributes
    assert "domain" not in attributes
    assert attributes["samesite"].lower() == "lax"
    query = parse_qs(urlsplit(location(response)).query)
    assert query["redirect_uri"] == ["https://hub.example.com/api/auth/oidc/e2e/callback"]


@pytest.mark.asyncio
async def test_login_cookie_is_one_signed_token_for_this_attempt(oidc: OIDCHarness) -> None:
    response = await oidc.start()

    # A signed token, not a raw state/nonce: nothing to fixate by cookie injection.
    cookie = login_cookie(response)
    assert cookie.count(".") == 2
    claims = jwt.decode(cookie, options={"verify_signature": False})
    assert claims["typ"] == TokenPurpose.OIDC_LOGIN_COOKIE.value
    assert claims["provider"] == "e2e"
    assert claims["challenge"] == SPA_CHALLENGE
    assert claims["exp"] - claims["iat"] == 600


@pytest.mark.asyncio
async def test_login_with_an_unknown_provider_is_a_json_404(oidc: OIDCHarness) -> None:
    response = await oidc.start(slug="nope")

    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown identity provider"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "challenge",
    [
        None,
        "",
        "too-short",
        SPA_CHALLENGE + "A",
        SPA_CHALLENGE[:-1] + "=",
        SPA_CHALLENGE[:-1] + "+",
    ],
)
async def test_login_with_a_bad_challenge_redirects_without_a_cookie(
    oidc: OIDCHarness, challenge: str | None
) -> None:
    response = await oidc.start(challenge=challenge)

    assert_login_error(response, "oidc_invalid_request")
    assert response.headers.get_list("set-cookie") == []
    assert oidc.idp.requests == []


@pytest.mark.asyncio
async def test_login_when_the_provider_is_unreachable_lands_on_oidc_failed(
    oidc: OIDCHarness,
) -> None:
    oidc.idp.raise_on = "/.well-known/openid-configuration"

    response = await oidc.start()

    assert_login_error(response, "oidc_failed")
    assert response.headers.get_list("set-cookie") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization_endpoint",
    [
        pytest.param("http://[::1/authorize", id="unclosed-ipv6-bracket"),
        pytest.param("http://exa℀mple.test/authorize", id="host-changes-under-nfkc"),
        pytest.param("http://idp.test:abc/authorize", id="non-numeric-port"),
    ],
)
async def test_login_with_an_unparseable_authorization_endpoint_lands_on_oidc_failed(
    oidc: OIDCHarness, authorization_endpoint: str
) -> None:
    oidc.idp.discovery_extra["authorization_endpoint"] = authorization_endpoint

    response = await oidc.start()

    assert_login_error(response, "oidc_failed")
    assert response.headers.get_list("set-cookie") == []


@pytest.mark.asyncio
async def test_login_error_redirects_fall_back_to_the_api_origin(oidc: OIDCHarness) -> None:
    # Without PUBLIC_APP_URL the SPA is assumed to share the API's origin.
    oidc.configure(oidc_settings(public_api_url="https://hub.example.com", public_app_url=None))

    response = await oidc.start(challenge=None)

    assert location(response) == "https://hub.example.com/auth/login?error=oidc_invalid_request"


@pytest.mark.asyncio
async def test_google_shortcut_login_asks_google_to_show_the_account_chooser(
    oidc: OIDCHarness,
) -> None:
    oidc.idp = FakeIdP(issuer="https://accounts.google.com")
    oidc.configure(
        oidc_settings(
            providers=[],
            google_client_id="google-client",
            google_client_secret="google-secret",  # noqa: S106
        )
    )

    response = await oidc.start(slug="google")

    query = parse_qs(urlsplit(location(response)).query)
    assert query["prompt"] == ["select_account"]
    assert query["redirect_uri"] == [f"{API_URL}/api/auth/oidc/google/callback"]


# ── GET /api/auth/oidc/{slug}/callback ──────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_callback_hands_off_to_the_spa(
    oidc: OIDCHarness, db_session: AsyncSession
) -> None:
    response = await oidc.sign_in()

    assert_callback_hygiene(response)
    handoff = handoff_from(response)
    claims = jwt.decode(handoff, options={"verify_signature": False})
    assert claims["typ"] == TokenPurpose.OIDC_HANDOFF.value
    assert claims["exp"] - claims["iat"] == 60
    assert claims["challenge"] == SPA_CHALLENGE
    identity = await db_session.scalar(select(Identity).where(Identity.provider == "oidc:e2e"))
    assert identity is not None
    assert identity.subject == "subject-ada"
    assert claims["sub"] == str(identity.user_id)


@pytest.mark.asyncio
async def test_callback_ignores_extra_provider_parameters(oidc: OIDCHarness) -> None:
    # Google appends these to every authorization response.
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    params.update(
        {
            "scope": "openid email https://www.googleapis.com/auth/userinfo.email",
            "authuser": "0",
            "prompt": "consent",
            "hd": "example.com",
        }
    )

    response = await oidc.callback(params, login_cookie(login))

    handoff_from(response)


@pytest.mark.asyncio
async def test_callback_on_https_deletes_the_host_prefixed_cookie(oidc: OIDCHarness) -> None:
    oidc.configure(oidc_settings(public_api_url="https://hub.example.com"))

    response = await oidc.sign_in()

    assert_callback_hygiene(response, cookie_name=HTTPS_COOKIE)
    deletion = cookie_attributes(response.headers["set-cookie"])
    assert "secure" in deletion
    assert deletion["path"] == "/"
    handoff_from(response)


@pytest.mark.asyncio
async def test_callback_without_the_login_cookie_is_oidc_expired(oidc: OIDCHarness) -> None:
    login = await oidc.start()

    response = await oidc.callback(oidc.idp.authorize(location(login)), cookie=None)

    assert_callback_hygiene(response)
    assert_login_error(response, "oidc_expired")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "", "forged-state"])
async def test_callback_with_a_mismatched_state_is_oidc_expired(
    oidc: OIDCHarness, state: str | None
) -> None:
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    if state is None:
        del params["state"]
    else:
        params["state"] = state

    response = await oidc.callback(params, login_cookie(login))

    assert_callback_hygiene(response)
    assert_login_error(response, "oidc_expired")
    assert all(request.url.path != "/token" for request in oidc.idp.requests)  # never redeemed


@pytest.mark.asyncio
async def test_callback_with_a_tampered_cookie_is_oidc_expired(oidc: OIDCHarness) -> None:
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    claims = jwt.decode(login_cookie(login), options={"verify_signature": False})
    claims["challenge"] = s256("attacker-chosen-verifier-" + "x" * 20)
    header, _, signature = login_cookie(login).split(".")
    forged_payload = jwt.encode(claims, "irrelevant-key-" + "k" * 32, algorithm="HS256")
    tampered = f"{header}.{forged_payload.split('.')[1]}.{signature}"
    # Signed with the bare SECRET_KEY rather than the derived cookie key.
    wrong_key = jwt.encode(claims, oidc.settings.SECRET_KEY, algorithm="HS256")

    for cookie in (tampered, wrong_key):
        response = await oidc.callback(params, cookie)
        assert_login_error(response, "oidc_expired")


@pytest.mark.asyncio
async def test_callback_with_an_expired_cookie_is_oidc_expired(oidc: OIDCHarness) -> None:
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    expired = create_purpose_token(
        oidc.settings.SECRET_KEY,
        TokenPurpose.OIDC_LOGIN_COOKIE,
        {
            "provider": "e2e",
            "state": params["state"],
            "nonce": "n",
            "code_verifier": "v" * 43,
            "challenge": SPA_CHALLENGE,
        },
        timedelta(minutes=-1),
    )

    response = await oidc.callback(params, expired)

    assert_login_error(response, "oidc_expired")


@pytest.mark.asyncio
async def test_callback_on_another_providers_path_is_oidc_expired(oidc: OIDCHarness) -> None:
    other = e2e_provider(slug="other", display_name="Other", issuer="http://other.test")
    oidc.configure(oidc_settings(providers=[e2e_provider(), other]))
    login = await oidc.start(slug="e2e")

    response = await oidc.callback(
        oidc.idp.authorize(location(login)), login_cookie(login), slug="other"
    )

    assert_callback_hygiene(response)
    assert_login_error(response, "oidc_expired")


@pytest.mark.asyncio
async def test_callback_for_a_provider_removed_since_login_is_oidc_failed(
    oidc: OIDCHarness,
) -> None:
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    oidc.configure(oidc_settings(providers=[]))

    response = await oidc.callback(params, login_cookie(login))

    assert_callback_hygiene(response)
    assert_login_error(response, "oidc_failed")


@pytest.mark.asyncio
async def test_callback_replayed_after_the_cookie_is_deleted_is_oidc_expired(
    oidc: OIDCHarness,
) -> None:
    # Let the client's jar play the browser: it stores the cookie, then obeys the deletion.
    login = await oidc.client.get("/api/auth/oidc/e2e/login", params={"challenge": SPA_CHALLENGE})
    params = oidc.idp.authorize(location(login))
    first = await oidc.client.get("/api/auth/oidc/e2e/callback", params=params)
    handoff_from(first)
    assert HTTP_COOKIE not in oidc.client.cookies

    replay = await oidc.client.get("/api/auth/oidc/e2e/callback", params=params)

    assert_callback_hygiene(replay)
    assert_login_error(replay, "oidc_expired")


@pytest.mark.asyncio
async def test_callback_replayed_with_a_captured_cookie_fails_at_the_provider(
    oidc: OIDCHarness,
) -> None:
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    handoff_from(await oidc.callback(params, login_cookie(login)))

    replay = await oidc.callback(params, login_cookie(login))

    assert_login_error(replay, "oidc_failed")  # authorization codes are single-use


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("access_denied", "oidc_denied"),
        ("server_error", "oidc_failed"),
        ("temporarily_unavailable", "oidc_failed"),
        ("interaction_required", "oidc_failed"),
    ],
)
async def test_provider_errors_map_to_login_error_codes(
    oidc: OIDCHarness, error: str, code: str
) -> None:
    login = await oidc.start()
    state = parse_qs(urlsplit(location(login)).query)["state"][0]

    response = await oidc.callback(
        {"error": error, "error_description": "nope\nforged log line", "state": state},
        login_cookie(login),
    )

    assert_callback_hygiene(response)
    assert_login_error(response, code)


@pytest.mark.asyncio
async def test_provider_error_without_the_matching_state_is_oidc_expired(
    oidc: OIDCHarness,
) -> None:
    login = await oidc.start()

    response = await oidc.callback({"error": "access_denied"}, login_cookie(login))

    assert_login_error(response, "oidc_expired")


@pytest.mark.asyncio
async def test_callback_without_a_code_is_oidc_failed(oidc: OIDCHarness) -> None:
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    del params["code"]

    response = await oidc.callback(params, login_cookie(login))

    assert_login_error(response, "oidc_failed")


@pytest.mark.asyncio
async def test_failed_token_exchange_is_oidc_failed(oidc: OIDCHarness) -> None:
    oidc.idp.token_status = 400
    oidc.idp.token_body = {"error": "invalid_grant"}

    response = await oidc.sign_in()

    assert_callback_hygiene(response)
    assert_login_error(response, "oidc_failed")


@pytest.mark.asyncio
async def test_invalid_id_token_is_oidc_failed(oidc: OIDCHarness) -> None:
    oidc.idp.claims_extra["aud"] = "another-client"

    response = await oidc.sign_in()

    assert_login_error(response, "oidc_failed")


@pytest.mark.asyncio
async def test_rfc9207_iss_mismatch_is_oidc_failed(oidc: OIDCHarness) -> None:
    oidc.idp.discovery_extra["authorization_response_iss_parameter_supported"] = True
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    params["iss"] = "http://evil.test"

    response = await oidc.callback(params, login_cookie(login))

    assert_login_error(response, "oidc_failed")
    assert all(request.url.path != "/token" for request in oidc.idp.requests)


@pytest.mark.asyncio
async def test_account_outside_the_hosted_domain_is_oidc_domain_not_allowed(
    oidc: OIDCHarness,
) -> None:
    oidc.configure(oidc_settings(providers=[e2e_provider(hosted_domain="example.com")]))
    oidc.idp.claims_extra["hd"] = "elsewhere.example"

    response = await oidc.sign_in()

    assert_callback_hygiene(response)
    assert_login_error(response, "oidc_domain_not_allowed")


@pytest.mark.asyncio
async def test_email_of_an_existing_account_is_oidc_account_exists(
    oidc: OIDCHarness, db_session: AsyncSession
) -> None:
    await register_user(oidc.client, username="ada", email="ada@example.com")

    response = await oidc.sign_in()

    assert_callback_hygiene(response)
    assert_login_error(response, "oidc_account_exists")
    users = (await db_session.execute(select(User))).scalars().all()
    assert len(users) == 1


@pytest.mark.asyncio
async def test_trusted_provider_links_to_an_existing_password_less_account(
    oidc: OIDCHarness, db_session: AsyncSession
) -> None:
    existing = await OIDCAuthService(db_session, "oidc:okta").complete_login(
        OIDCClaims(
            provider="oidc:okta",
            subject="okta-ada",
            email="Ada@Example.com",
            email_verified=True,
        )
    )
    oidc.configure(oidc_settings(providers=[e2e_provider(trusted_email_linking=True)]))

    response = await oidc.exchange(handoff_from(await oidc.sign_in()))

    assert exchanged_user(response)["id"] == str(existing.id)


@pytest.mark.asyncio
async def test_trusted_provider_never_links_into_an_account_claimed_with_an_unverified_email(
    oidc: OIDCHarness, db_session: AsyncSession
) -> None:
    # Pre-account hijacking: an attacker signs in first with the victim's
    # address unverified, then the victim signs in with it verified.
    oidc.configure(oidc_settings(providers=[e2e_provider(trusted_email_linking=True)]))
    oidc.idp.subject = "attacker-sub"
    oidc.idp.claims_extra["email_verified"] = False
    exchanged_user(await oidc.exchange(handoff_from(await oidc.sign_in())))
    oidc.idp.subject = "victim-sub"
    oidc.idp.claims_extra["email_verified"] = True

    response = await oidc.sign_in()

    assert_callback_hygiene(response)
    assert_login_error(response, "oidc_account_exists")
    subjects = (await db_session.execute(select(Identity.subject))).scalars().all()
    assert subjects == ["attacker-sub"]


@pytest.mark.asyncio
async def test_unexpected_failure_still_lands_on_the_spa(oidc: OIDCHarness) -> None:
    class BrokenSession:
        async def scalar(self, *_: object, **__: object) -> object:
            raise RuntimeError("database is down")

    async def broken_db() -> AsyncGenerator[BrokenSession, None]:
        yield BrokenSession()

    app.dependency_overrides[get_db] = broken_db

    response = await oidc.sign_in()

    assert_callback_hygiene(response)
    assert_login_error(response, "oidc_failed")


# ── POST /api/auth/oidc/exchange ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exchange_returns_a_working_access_token_and_the_user(oidc: OIDCHarness) -> None:
    oidc.idp.claims_extra["preferred_username"] = "Ada Lovelace"
    oidc.idp.claims_extra["name"] = "  Ada King, Countess of Lovelace "
    handoff = handoff_from(await oidc.sign_in())

    response = await oidc.exchange(handoff)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = json_dict(response)
    assert payload["token_type"] == BEARER
    user = exchanged_user(response)
    assert user["username"] == "ada-lovelace"
    assert user["email"] == "ada@example.com"
    assert user["display_name"] == "Ada King, Countess of Lovelace"
    assert set(user) == {"id", "username", "email", "display_name", "created_at"}
    logout = await oidc.client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {payload['access_token']}"}
    )
    assert logout.status_code == 204


@pytest.mark.asyncio
async def test_signing_in_again_resolves_the_same_user(oidc: OIDCHarness) -> None:
    first = exchanged_user(await oidc.exchange(handoff_from(await oidc.sign_in())))
    second = exchanged_user(await oidc.exchange(handoff_from(await oidc.sign_in())))

    assert first == second


@pytest.mark.asyncio
async def test_a_later_sign_in_fills_a_missing_display_name_but_never_replaces_one(
    oidc: OIDCHarness,
) -> None:
    async def signed_in_display_name() -> object:
        return exchanged_user(await oidc.exchange(handoff_from(await oidc.sign_in())))[
            "display_name"
        ]

    # The provider first sends no `name`, then one, then a different one.
    assert await signed_in_display_name() is None
    oidc.idp.claims_extra["name"] = "Ada Lovelace"
    assert await signed_in_display_name() == "Ada Lovelace"
    oidc.idp.claims_extra["name"] = "Augusta Ada King"
    assert await signed_in_display_name() == "Ada Lovelace"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verifier",
    [
        "A" * 43,
        SPA_VERIFIER[:-1],
        SPA_CHALLENGE,  # the challenge itself is not the verifier
        "",
        "é" * 43,
    ],
)
async def test_exchange_with_the_wrong_verifier_is_401(oidc: OIDCHarness, verifier: str) -> None:
    handoff = handoff_from(await oidc.sign_in())

    response = await oidc.exchange(handoff, verifier)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired sign-in handoff"}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["handoff", "verifier"])
async def test_exchange_with_a_lone_surrogate_is_401(oidc: OIDCHarness, field: str) -> None:
    body = {"handoff": handoff_from(await oidc.sign_in()), "verifier": SPA_VERIFIER}
    body[field] = "\ud800"  # valid JSON, as the escape \ud800, but not encodable as UTF-8

    response = await oidc.client.post(
        "/api/auth/oidc/exchange",
        content=json.dumps(body).encode("ascii"),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired sign-in handoff"}


class _ClockAhead(datetime):
    """`datetime` as read on a replica whose clock runs `ahead` of this one."""

    ahead = timedelta(0)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp((datetime.now(tz) + cls.ahead).timestamp(), tz)


@pytest.fixture
def replica_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Set how far ahead the clock that signs purpose tokens runs; 0 is this replica's."""

    def set_ahead(seconds: float) -> None:
        _ClockAhead.ahead = timedelta(seconds=seconds)
        monkeypatch.setattr(security, "datetime", _ClockAhead)

    return set_ahead


@pytest.mark.asyncio
@pytest.mark.parametrize("ahead_seconds", [2.0, 4.0])
async def test_handoff_minted_by_a_replica_with_a_slightly_fast_clock_is_accepted(
    oidc: OIDCHarness, replica_clock: Callable[[float], None], ahead_seconds: float
) -> None:
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    replica_clock(ahead_seconds)  # the callback lands on a replica whose clock runs ahead
    callback = await oidc.callback(params, login_cookie(login))
    replica_clock(0)

    response = await oidc.exchange(handoff_from(callback))

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("ahead_seconds", [2.0, 4.0])
async def test_login_cookie_minted_by_a_replica_with_a_slightly_fast_clock_is_accepted(
    oidc: OIDCHarness, replica_clock: Callable[[float], None], ahead_seconds: float
) -> None:
    replica_clock(ahead_seconds)  # the login starts on a replica whose clock runs ahead
    login = await oidc.start()
    replica_clock(0)

    response = await oidc.callback(oidc.idp.authorize(location(login)), login_cookie(login))

    handoff_from(response)


@pytest.mark.asyncio
async def test_handoff_issued_well_in_the_future_is_401(
    oidc: OIDCHarness, replica_clock: Callable[[float], None]
) -> None:
    login = await oidc.start()
    params = oidc.idp.authorize(location(login))
    replica_clock(30)
    callback = await oidc.callback(params, login_cookie(login))
    replica_clock(0)

    response = await oidc.exchange(handoff_from(callback))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_exchange_with_an_expired_handoff_is_401(oidc: OIDCHarness) -> None:
    handoff = handoff_from(await oidc.sign_in())
    user_id = jwt.decode(handoff, options={"verify_signature": False})["sub"]
    expired = create_purpose_token(
        oidc.settings.SECRET_KEY,
        TokenPurpose.OIDC_HANDOFF,
        {"sub": user_id, "challenge": SPA_CHALLENGE},
        # Expired for longer than the clock skew allowed between replicas.
        -(PURPOSE_TOKEN_CLOCK_LEEWAY + timedelta(seconds=1)),
    )

    response = await oidc.exchange(expired)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_exchange_refuses_tokens_minted_for_other_purposes(oidc: OIDCHarness) -> None:
    login = await oidc.start()
    handoff = handoff_from(
        await oidc.callback(oidc.idp.authorize(location(login)), login_cookie(login))
    )
    user_id = UUID(jwt.decode(handoff, options={"verify_signature": False})["sub"])
    cookie_claims = {"sub": str(user_id), "challenge": SPA_CHALLENGE}
    as_cookie = create_purpose_token(
        oidc.settings.SECRET_KEY, TokenPurpose.OIDC_LOGIN_COOKIE, cookie_claims, timedelta(1)
    )

    for impostor in (login_cookie(login), as_cookie, create_access_token(user_id), "", "x.y.z"):
        response = await oidc.exchange(impostor)
        assert response.status_code == 401, impostor


@pytest.mark.asyncio
async def test_exchange_for_a_deleted_user_is_401(
    oidc: OIDCHarness, db_session: AsyncSession
) -> None:
    handoff = handoff_from(await oidc.sign_in())
    await db_session.execute(delete(User))
    await db_session.commit()

    response = await oidc.exchange(handoff)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_handoff_is_never_accepted_as_a_bearer_token(oidc: OIDCHarness) -> None:
    handoff = handoff_from(await oidc.sign_in())

    response = await oidc.client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {handoff}"}
    )

    assert response.status_code == 401
    assert decode_token(handoff) is None


# ── the token boundary: only access tokens are bearer tokens ────────────────


def test_decode_token_refuses_a_typed_token_even_when_signed_with_the_secret_key(
    oidc_secret_key: str,
) -> None:
    now = datetime.now(UTC)
    user_id = uuid4()
    typed = jwt.encode(
        {"sub": str(user_id), "exp": now + timedelta(minutes=5), "typ": "anything"},
        oidc_secret_key,
        algorithm="HS256",
    )
    without_exp = jwt.encode({"sub": str(user_id)}, oidc_secret_key, algorithm="HS256")

    assert decode_token(typed) is None
    assert decode_token(without_exp) is None
    assert decode_token(create_access_token(user_id)) == user_id


@pytest.mark.parametrize("purpose", list(TokenPurpose))
def test_purpose_tokens_are_never_bearer_tokens(
    oidc_secret_key: str, purpose: TokenPurpose
) -> None:
    user_id = uuid4()
    token = create_purpose_token(
        oidc_secret_key, purpose, {"sub": str(user_id)}, timedelta(minutes=5)
    )

    assert decode_token(token) is None


@pytest.fixture
def oidc_secret_key(test_database_url: str) -> str:
    del test_database_url  # only needed for the environment it sets up
    return get_settings().SECRET_KEY


# ── CORS ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("public_app_url", "expected"),
    [
        (None, [DEV_SPA_ORIGIN]),
        ("http://localhost:5173", [DEV_SPA_ORIGIN]),
        ("https://hub.example.com/app/", [DEV_SPA_ORIGIN, "https://hub.example.com"]),
        ("https://hub.example.com:8443", [DEV_SPA_ORIGIN, "https://hub.example.com:8443"]),
        ("https://hub.example.com:443/", [DEV_SPA_ORIGIN, "https://hub.example.com"]),
    ],
)
def test_cors_allows_the_dev_spa_and_an_explicit_public_app_url(
    test_database_url: str, public_app_url: str | None, expected: list[str]
) -> None:
    del test_database_url  # only needed for the environment it sets up
    settings = oidc_settings(public_app_url=public_app_url)

    assert cors_allow_origins(settings) == expected


@pytest.mark.asyncio
async def test_cors_preflight_from_the_dev_spa_is_allowed(api_client: AsyncClient) -> None:
    response = await api_client.options(
        "/api/auth/oidc/exchange",
        headers={
            "Origin": DEV_SPA_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEV_SPA_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
