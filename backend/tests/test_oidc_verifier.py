"""The real `OIDCVerifier` (authlib + httpx + joserfc) against an in-memory OpenID provider.

`FakeIdP` sits behind `httpx.MockTransport`, so every request the verifier
makes is real httpx traffic: discovery, the authlib token exchange (whose
PKCE verifier the fake checks) and the signing keys. Nothing in the verifier
is patched. `test_oidc_routes` reuses `FakeIdP` for the HTTP routes.
"""

import asyncio
import base64
from collections.abc import Callable
import dataclasses
from dataclasses import dataclass
import hashlib
import json
import secrets
import time
from typing import Final
from urllib.parse import parse_qs, urlsplit

import httpx
from joserfc import jwt as rfc_jwt
from joserfc.jwk import OctKey, RSAKey
import pytest

from app.services.auth_service import OIDCClaims
from app.services.oidc_verifier import (
    AuthorizationRequest,
    OIDCDomainNotAllowedError,
    OIDCHttpOptions,
    OIDCMetadataCache,
    OIDCProviderConfig,
    OIDCProviderError,
    OIDCVerifier,
)

# Fake credentials routed through module-level constants (the test_auth.py convention).
CLIENT_ID: Final = "client-id"
CLIENT_SECRET: Final = "fake-client-secret-long-enough-for-hs256"  # noqa: S105
# Deliberately no path: the issuer is where a trailing-slash bug shows.
ISSUER: Final = "http://idp.test"
REDIRECT_URI: Final = "http://localhost:8000/api/auth/oidc/test/callback"
GOOGLE_ISSUER: Final = "https://accounts.google.com"

KEY_1: Final = RSAKey.generate_key(2048, parameters={"kid": "key-1", "use": "sig", "alg": "RS256"})
KEY_2: Final = RSAKey.generate_key(2048, parameters={"kid": "key-2", "use": "sig", "alg": "RS256"})
UNPUBLISHED_KEY: Final = RSAKey.generate_key(
    2048, parameters={"kid": "key-never-published", "use": "sig", "alg": "RS256"}
)

DROP: Final = object()  # a `claims_extra` value that removes the claim from the id_token


@dataclass(frozen=True)
class FromNow:
    """A `claims_extra` timestamp, resolved when the token is issued (never at collection)."""

    seconds: int


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def s256(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass
class _Grant:
    client_id: str
    redirect_uri: str
    nonce: str | None
    code_challenge: str | None
    code_challenge_method: str | None


class FakeIdP:
    """An OpenID provider served from memory through `httpx.MockTransport`.

    Tests steer it through its public attributes: extra discovery fields,
    extra (or `DROP`ped) id_token claims, which key signs and which are
    published, and canned token-endpoint failures.
    """

    def __init__(
        self,
        issuer: str = ISSUER,
        *,
        client_id: str = CLIENT_ID,
        client_secret: str = CLIENT_SECRET,
        email: str = "ada@example.com",
    ) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.client_secret = client_secret
        self.subject = "subject-ada"
        self.email = email
        base = issuer.rstrip("/")
        self.authorization_endpoint = f"{base}/authorize"
        self.token_endpoint = f"{base}/token"
        self.jwks_uri = f"{base}/jwks"
        self.discovery_extra: dict[str, object] = {}
        self.claims_extra: dict[str, object] = {}
        self.signing_key: RSAKey = KEY_1
        self.published_keys: list[RSAKey] = [KEY_1]
        # Replaces RS256 signing with a forgery (e.g. alg "none" or HS256).
        self.forge: Callable[[dict[str, object]], str] | None = None
        self.jwks_cache_control: str | None = "public, max-age=3600"
        self.token_status = 200
        self.token_body: dict[str, object] | None = None  # replaces the success body
        self.raise_on: str | None = None  # a path whose request fails at the network level
        self.requests: list[httpx.Request] = []
        self._grants: dict[str, _Grant] = {}

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def provider_config(self, **overrides: object) -> OIDCProviderConfig:
        config = OIDCProviderConfig(
            provider="oidc:test",
            kind="oidc",
            issuer=self.issuer,
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=REDIRECT_URI,
        )
        return dataclasses.replace(config, **overrides)

    def authorize(self, authorize_url: str) -> dict[str, str]:
        """Play the browser + consent screen: return the callback's query parameters."""
        query = {key: values[0] for key, values in parse_qs(urlsplit(authorize_url).query).items()}
        assert authorize_url.startswith(self.authorization_endpoint)
        code = secrets.token_urlsafe(16)
        self._grants[code] = _Grant(
            client_id=query["client_id"],
            redirect_uri=query["redirect_uri"],
            nonce=query.get("nonce"),
            code_challenge=query.get("code_challenge"),
            code_challenge_method=query.get("code_challenge_method"),
        )
        callback = {"code": code, "state": query["state"]}
        if self.discovery_extra.get("authorization_response_iss_parameter_supported") is True:
            callback["iss"] = self.issuer
        return callback

    def id_token_claims(self, nonce: str | None) -> dict[str, object]:
        now = int(time.time())
        claims: dict[str, object] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": self.subject,
            "email": self.email,
            "email_verified": True,
            "iat": now,
            "exp": now + 3600,
            "nonce": nonce,
        }
        claims.update(self.claims_extra)
        return {
            name: now + value.seconds if isinstance(value, FromNow) else value
            for name, value in claims.items()
            if value is not DROP
        }

    def sign(self, claims: dict[str, object], key: RSAKey | None = None) -> str:
        signing_key = key or self.signing_key
        return rfc_jwt.encode({"alg": "RS256", "kid": signing_key.kid}, claims, signing_key)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_on is not None and request.url.path == self.raise_on:
            raise httpx.ConnectError("connection refused", request=request)
        url = str(request.url.copy_with(query=None))
        if request.method == "GET" and url == self.discovery_url:
            return httpx.Response(200, json=self._discovery_document())
        if request.method == "GET" and url == self.jwks_uri:
            headers = {"Cache-Control": self.jwks_cache_control} if self.jwks_cache_control else {}
            keys = [key.as_dict(private=False) for key in self.published_keys]
            return httpx.Response(200, json={"keys": keys}, headers=headers)
        if request.method == "POST" and url == self.token_endpoint:
            return self._token(request)
        return httpx.Response(404, json={"error": "not_found"})

    def _discovery_document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "jwks_uri": self.jwks_uri,
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
        }
        document.update(self.discovery_extra)
        return document

    def _client_authenticated(self, request: httpx.Request, form: dict[str, str]) -> bool:
        expected = b64url(f"{self.client_id}:{self.client_secret}".encode()).rstrip("=")
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            return header.removeprefix("Basic ").rstrip("=") == expected
        return (form.get("client_id"), form.get("client_secret")) == (
            self.client_id,
            self.client_secret,
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        grant = self._grants.pop(form.get("code", ""), None)  # codes are single-use
        if (
            not self._client_authenticated(request, form)
            or form.get("grant_type") != "authorization_code"
            or grant is None
            or form.get("redirect_uri") != grant.redirect_uri
            or grant.code_challenge_method != "S256"
            or s256(form.get("code_verifier", "")) != grant.code_challenge
        ):
            return httpx.Response(400, json={"error": "invalid_grant"})
        if self.token_body is not None or self.token_status != 200:
            return httpx.Response(self.token_status, json=self.token_body or {})
        claims = self.id_token_claims(grant.nonce)
        id_token = self.forge(claims) if self.forge is not None else self.sign(claims)
        body = {"access_token": "fake-access-token", "token_type": "Bearer", "id_token": id_token}
        return httpx.Response(200, json=body)


def make_verifier(
    idp: FakeIdP,
    *,
    cache: OIDCMetadataCache | None = None,
    **config: object,
) -> OIDCVerifier:
    options = OIDCHttpOptions(transport=idp.transport(), cache=cache or OIDCMetadataCache())
    return OIDCVerifier(idp.provider_config(**config), options)


async def finish(
    verifier: OIDCVerifier,
    request: AuthorizationRequest,
    callback: dict[str, str],
    *,
    iss: str | None = None,
) -> OIDCClaims:
    """Hand the provider's callback parameters to the verifier, as the callback route does."""
    return await verifier.verify_callback(
        code=callback["code"],
        iss=iss if iss is not None else callback.get("iss"),
        nonce=request.nonce,
        code_verifier=request.code_verifier,
    )


async def sign_in(
    verifier: OIDCVerifier, idp: FakeIdP, *, iss: str | None = None
) -> tuple[AuthorizationRequest, OIDCClaims]:
    """Run one complete authorization-code round trip through the verifier."""
    request = await verifier.authorization_request()
    return request, await finish(verifier, request, idp.authorize(request.url), iss=iss)


def query_of(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def requests_to(idp: FakeIdP, url: str) -> list[httpx.Request]:
    return [request for request in idp.requests if str(request.url) == url]


# ── the happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_round_trip_returns_verified_claims() -> None:
    idp = FakeIdP()
    idp.claims_extra["preferred_username"] = "ada"
    idp.claims_extra["name"] = "Ada Lovelace"

    _, claims = await sign_in(make_verifier(idp), idp)

    assert claims == OIDCClaims(
        provider="oidc:test",
        subject="subject-ada",
        email="ada@example.com",
        email_verified=True,
        preferred_username="ada",
        name="Ada Lovelace",
        hosted_domain=None,
    )


@pytest.mark.asyncio
async def test_authorization_url_carries_the_code_flow_and_pkce_parameters() -> None:
    idp = FakeIdP()

    request = await make_verifier(idp).authorization_request()

    query = query_of(request.url)
    assert request.url.startswith(f"{ISSUER}/authorize?")
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["scope"] == "openid email profile"
    assert query["state"] == request.state
    assert query["nonce"] == request.nonce
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == s256(request.code_verifier)
    assert len(request.code_verifier) >= 43
    assert "prompt" not in query
    assert "hd" not in query


@pytest.mark.asyncio
async def test_each_attempt_gets_fresh_state_nonce_and_verifier() -> None:
    verifier = make_verifier(FakeIdP())

    first = await verifier.authorization_request()
    second = await verifier.authorization_request()

    assert first.state != second.state
    assert first.nonce != second.nonce
    assert first.code_verifier != second.code_verifier


@pytest.mark.asyncio
async def test_prompt_and_hosted_domain_are_sent_when_configured() -> None:
    idp = FakeIdP()

    request = await make_verifier(
        idp, prompt="select_account", hosted_domain="example.com"
    ).authorization_request()

    query = query_of(request.url)
    assert query["prompt"] == "select_account"
    assert query["hd"] == "example.com"


@pytest.mark.asyncio
async def test_token_exchange_sends_the_pkce_verifier_and_client_secret_basic() -> None:
    idp = FakeIdP()

    request, _ = await sign_in(make_verifier(idp), idp)

    (token_request,) = requests_to(idp, idp.token_endpoint)
    form = {key: values[0] for key, values in parse_qs(token_request.content.decode()).items()}
    assert form["code_verifier"] == request.code_verifier
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == REDIRECT_URI
    assert token_request.headers["authorization"].startswith("Basic ")
    assert "client_secret" not in form


@pytest.mark.asyncio
async def test_client_secret_post_when_discovery_offers_only_that() -> None:
    idp = FakeIdP()
    idp.discovery_extra["token_endpoint_auth_methods_supported"] = ["client_secret_post"]

    await sign_in(make_verifier(idp), idp)

    (token_request,) = requests_to(idp, idp.token_endpoint)
    form = parse_qs(token_request.content.decode())
    assert "authorization" not in token_request.headers
    assert form["client_secret"] == [CLIENT_SECRET]


@pytest.mark.asyncio
async def test_discovery_and_jwks_requests_never_carry_an_authorization_header() -> None:
    idp = FakeIdP()

    await sign_in(make_verifier(idp), idp)

    gets = [request for request in idp.requests if request.method == "GET"]
    assert {str(request.url) for request in gets} == {idp.discovery_url, idp.jwks_uri}
    assert all("authorization" not in request.headers for request in gets)


@pytest.mark.asyncio
async def test_discovery_url_is_issuer_plus_well_known_without_a_double_slash() -> None:
    idp = FakeIdP()

    await make_verifier(idp).authorization_request()

    (discovery,) = idp.requests
    assert str(discovery.url) == "http://idp.test/.well-known/openid-configuration"


@pytest.mark.asyncio
async def test_issuer_with_a_trailing_slash_is_kept_exactly() -> None:
    # Keycloak-style issuers may end in "/"; discovery strips it, comparisons keep it.
    idp = FakeIdP(issuer="http://idp.test/realms/main/")

    _, claims = await sign_in(make_verifier(idp), idp)

    assert (
        str(idp.requests[0].url) == "http://idp.test/realms/main/.well-known/openid-configuration"
    )
    assert claims.subject == "subject-ada"


# ── issuer checks ───────────────────────────────────────────────────────────


def google_idp() -> FakeIdP:
    idp = FakeIdP(issuer=GOOGLE_ISSUER)
    idp.authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    idp.token_endpoint = "https://oauth2.googleapis.com/token"  # noqa: S105 -- a URL, not a secret
    idp.jwks_uri = "https://www.googleapis.com/oauth2/v3/certs"
    return idp


@pytest.mark.asyncio
@pytest.mark.parametrize("token_issuer", ["https://accounts.google.com", "accounts.google.com"])
async def test_google_accepts_both_documented_issuer_forms(token_issuer: str) -> None:
    idp = google_idp()
    idp.claims_extra["iss"] = token_issuer

    _, claims = await sign_in(make_verifier(idp, kind="google", provider="google"), idp)

    assert claims.provider == "google"


@pytest.mark.asyncio
async def test_bare_host_issuer_is_only_accepted_for_google() -> None:
    idp = FakeIdP()
    idp.claims_extra["iss"] = "idp.test"

    with pytest.raises(OIDCProviderError):
        await sign_in(make_verifier(idp), idp)


@pytest.mark.asyncio
async def test_id_token_from_another_issuer_is_rejected() -> None:
    idp = FakeIdP()
    idp.claims_extra["iss"] = "http://evil.test"

    with pytest.raises(OIDCProviderError):
        await sign_in(make_verifier(idp), idp)


@pytest.mark.asyncio
async def test_discovery_issuer_must_equal_the_configured_issuer() -> None:
    idp = FakeIdP()
    idp.discovery_extra["issuer"] = "http://idp.test/"

    with pytest.raises(OIDCProviderError, match="discovery issuer"):
        await make_verifier(idp).authorization_request()


@pytest.mark.asyncio
async def test_rfc9207_iss_is_required_when_the_provider_advertises_it() -> None:
    idp = FakeIdP()
    idp.discovery_extra["authorization_response_iss_parameter_supported"] = True
    verifier = make_verifier(idp)

    request = await verifier.authorization_request()
    callback = idp.authorize(request.url)
    callback.pop("iss")
    with pytest.raises(OIDCProviderError, match="iss"):
        await finish(verifier, request, callback)

    _, claims = await sign_in(verifier, idp)  # the fake sends iss=issuer
    assert claims.subject == "subject-ada"


@pytest.mark.asyncio
@pytest.mark.parametrize("advertised", [True, False])
async def test_rfc9207_mismatched_iss_is_rejected_before_redeeming_the_code(
    *, advertised: bool
) -> None:
    idp = FakeIdP()
    idp.discovery_extra["authorization_response_iss_parameter_supported"] = advertised

    with pytest.raises(OIDCProviderError, match="iss"):
        await sign_in(make_verifier(idp), idp, iss="http://evil.test")

    assert requests_to(idp, idp.token_endpoint) == []


@pytest.mark.asyncio
async def test_missing_iss_is_fine_when_the_provider_does_not_advertise_it() -> None:
    idp = FakeIdP()

    _, claims = await sign_in(make_verifier(idp), idp)

    assert claims.subject == "subject-ada"


# ── audience, nonce and time claims ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims_extra",
    [
        pytest.param({"aud": "another-client"}, id="wrong-aud"),
        pytest.param({"aud": [CLIENT_ID, "another-client"]}, id="multi-aud"),
        pytest.param(
            {"aud": [CLIENT_ID, "another-client"], "azp": CLIENT_ID}, id="multi-aud-with-azp"
        ),
        pytest.param({"aud": [CLIENT_ID], "azp": "another-client"}, id="list-aud-wrong-azp"),
        pytest.param({"azp": "another-client"}, id="wrong-azp"),
        pytest.param({"aud": DROP}, id="missing-aud"),
        pytest.param({"nonce": "not-the-nonce"}, id="wrong-nonce"),
        pytest.param({"nonce": DROP}, id="missing-nonce"),
        pytest.param({"exp": FromNow(-120)}, id="expired"),
        pytest.param({"exp": DROP}, id="missing-exp"),
        pytest.param({"iat": DROP}, id="missing-iat"),
        pytest.param({"iat": FromNow(600)}, id="issued-in-the-future"),
        pytest.param({"sub": DROP}, id="missing-sub"),
        pytest.param({"sub": ""}, id="empty-sub"),
        pytest.param({"sub": "x" * 256}, id="overlong-sub"),
        pytest.param({"email": DROP}, id="missing-email"),
        pytest.param({"iss": DROP}, id="missing-iss"),
    ],
)
async def test_invalid_id_token_claims_are_rejected(claims_extra: dict[str, object]) -> None:
    idp = FakeIdP()
    idp.claims_extra.update(claims_extra)

    with pytest.raises(OIDCProviderError):
        await sign_in(make_verifier(idp), idp)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims_extra",
    [
        pytest.param({"aud": [CLIENT_ID]}, id="one-element-list-aud"),
        pytest.param({"aud": [CLIENT_ID], "azp": CLIENT_ID}, id="list-aud-matching-azp"),
        pytest.param({"azp": CLIENT_ID}, id="matching-azp"),
        pytest.param({"iat": FromNow(30)}, id="clock-skew-within-leeway"),
        pytest.param({"exp": FromNow(-30)}, id="expiry-within-leeway"),
    ],
)
async def test_valid_variants_of_id_token_claims_are_accepted(
    claims_extra: dict[str, object],
) -> None:
    idp = FakeIdP()
    idp.claims_extra.update(claims_extra)

    _, claims = await sign_in(make_verifier(idp), idp)

    assert claims.subject == "subject-ada"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("true", False), ("True", False), (1, False), (DROP, False)],
)
async def test_email_verified_only_counts_as_json_true(*, value: object, expected: bool) -> None:
    idp = FakeIdP()
    idp.claims_extra["email_verified"] = value

    _, claims = await sign_in(make_verifier(idp), idp)

    assert claims.email_verified is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("Ada Lovelace", "Ada Lovelace", id="name"),
        pytest.param("  Ada Lovelace\n", "Ada Lovelace", id="trimmed"),
        pytest.param("x" * 255, "x" * 255, id="longest-storable"),
        # Optional profile data: an unusable name is dropped, never fatal.
        pytest.param(DROP, None, id="absent"),
        pytest.param("", None, id="empty"),
        pytest.param("   ", None, id="blank"),
        pytest.param("x" * 256, None, id="too-long-to-store"),
        pytest.param(" " + "x" * 255 + " ", "x" * 255, id="fits-once-trimmed"),
        pytest.param(42, None, id="not-a-string"),
        pytest.param(["Ada"], None, id="a-list"),
        # Regression: some providers send the address as the name, which a
        # name search then showed in full.
        pytest.param("ada@example.com", None, id="an-email-address"),
        pytest.param("Ada Lovelace" + chr(0x202E), None, id="right-to-left-override"),
        pytest.param("Ada" + chr(0x200B) + " Lovelace", None, id="zero-width-space"),
        pytest.param("Ada" + chr(10) + "Lovelace", None, id="inner-newline"),
        pytest.param("Ada" + chr(0), None, id="nul"),
        pytest.param("Nima" + chr(0x200C) + "pour", "Nima" + chr(0x200C) + "pour", id="joiner"),
    ],
)
async def test_name_claim_becomes_a_trimmed_storable_name_or_none(
    value: object, expected: str | None
) -> None:
    idp = FakeIdP()
    idp.claims_extra["name"] = value

    _, claims = await sign_in(make_verifier(idp), idp)

    assert claims.name == expected


# ── signatures and keys ─────────────────────────────────────────────────────


def unsigned_token(claims: dict[str, object]) -> str:
    header = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    return f"{header}.{b64url(json.dumps(claims).encode())}."


def hs256_token(claims: dict[str, object]) -> str:
    # Signed with the client secret: the classic algorithm-confusion forgery.
    return rfc_jwt.encode({"alg": "HS256"}, claims, OctKey.import_key(CLIENT_SECRET))


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", [unsigned_token, hs256_token], ids=["alg-none", "alg-hs256"])
async def test_symmetric_and_unsigned_id_tokens_are_rejected(
    forge: Callable[[dict[str, object]], str],
) -> None:
    idp = FakeIdP()
    idp.forge = forge

    with pytest.raises(OIDCProviderError):
        await sign_in(make_verifier(idp), idp)


@pytest.mark.asyncio
async def test_token_signed_by_an_unpublished_key_is_rejected() -> None:
    idp = FakeIdP()
    idp.signing_key = UNPUBLISHED_KEY

    with pytest.raises(OIDCProviderError):
        await sign_in(make_verifier(idp), idp)


@pytest.mark.asyncio
async def test_unknown_kid_refetches_the_cached_jwks_once() -> None:
    idp = FakeIdP()
    cache = OIDCMetadataCache()
    await sign_in(make_verifier(idp, cache=cache), idp)  # caches [key-1]

    idp.published_keys = [KEY_1, KEY_2]
    idp.signing_key = KEY_2  # the provider rotates its signing key
    _, claims = await sign_in(make_verifier(idp, cache=cache), idp)

    assert claims.subject == "subject-ada"
    assert len(requests_to(idp, idp.jwks_uri)) == 2


@pytest.mark.asyncio
async def test_kid_unknown_even_after_a_refetch_fails_without_retrying_again() -> None:
    idp = FakeIdP()
    cache = OIDCMetadataCache()
    await sign_in(make_verifier(idp, cache=cache), idp)

    idp.signing_key = UNPUBLISHED_KEY
    with pytest.raises(OIDCProviderError):
        await sign_in(make_verifier(idp, cache=cache), idp)

    assert len(requests_to(idp, idp.jwks_uri)) == 2


@pytest.mark.asyncio
async def test_known_kid_never_refetches_the_jwks() -> None:
    idp = FakeIdP()
    cache = OIDCMetadataCache()

    for _ in range(3):
        await sign_in(make_verifier(idp, cache=cache), idp)

    assert len(requests_to(idp, idp.discovery_url)) == 1
    assert len(requests_to(idp, idp.jwks_uri)) == 1


@pytest.mark.asyncio
async def test_unusable_jwks_entries_are_skipped() -> None:
    idp = FakeIdP()
    original_handle = idp.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == idp.jwks_uri:
            keys = [{"kty": "unknown"}, "junk", KEY_1.as_dict(private=False)]
            return httpx.Response(200, json={"keys": keys})
        return original_handle(request)

    verifier = OIDCVerifier(
        idp.provider_config(),
        OIDCHttpOptions(transport=httpx.MockTransport(handle), cache=OIDCMetadataCache()),
    )

    _, claims = await sign_in(verifier, idp)

    assert claims.subject == "subject-ada"


# ── caching ─────────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_discovery_is_cached_for_an_hour() -> None:
    idp = FakeIdP()
    clock = FakeClock()
    verifier = make_verifier(idp, cache=OIDCMetadataCache(clock))

    await verifier.authorization_request()
    clock.now += 3599
    await verifier.authorization_request()
    assert len(requests_to(idp, idp.discovery_url)) == 1

    clock.now += 2
    await verifier.authorization_request()
    assert len(requests_to(idp, idp.discovery_url)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cache_control", "fresh_for"),
    [
        pytest.param("public, max-age=19830, must-revalidate", 19_830, id="honored"),
        pytest.param("max-age=10", 300, id="clamped-up-to-5-minutes"),
        pytest.param("no-store", 300, id="no-max-age"),
        pytest.param(None, 300, id="no-cache-control"),
        pytest.param("max-age=999999", 86_400, id="clamped-down-to-24-hours"),
        pytest.param("s-maxage=99999", 300, id="s-maxage-ignored"),
    ],
)
async def test_jwks_cache_honors_max_age_within_bounds(
    cache_control: str | None, fresh_for: float
) -> None:
    idp = FakeIdP()
    idp.jwks_cache_control = cache_control
    clock = FakeClock()
    cache = OIDCMetadataCache(clock)

    await sign_in(make_verifier(idp, cache=cache), idp)
    clock.now += fresh_for - 1
    await sign_in(make_verifier(idp, cache=cache), idp)
    assert len(requests_to(idp, idp.jwks_uri)) == 1

    clock.now += 2
    await sign_in(make_verifier(idp, cache=cache), idp)
    assert len(requests_to(idp, idp.jwks_uri)) == 2


# ── hosted domain (Google `hd`) ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("hd", ["example.com", "Example.COM"])
async def test_hosted_domain_accepts_its_own_accounts(hd: str) -> None:
    idp = FakeIdP()
    idp.claims_extra["hd"] = hd

    _, claims = await sign_in(make_verifier(idp, hosted_domain="example.com"), idp)

    assert claims.hosted_domain == hd


@pytest.mark.asyncio
@pytest.mark.parametrize("hd", [DROP, "", "evil.example", "example.com.evil.test"])
async def test_hosted_domain_rejects_other_accounts(hd: object) -> None:
    idp = FakeIdP()
    idp.claims_extra["hd"] = hd

    with pytest.raises(OIDCDomainNotAllowedError):
        await sign_in(make_verifier(idp, hosted_domain="example.com"), idp)


@pytest.mark.asyncio
async def test_hd_claim_is_exposed_without_a_hosted_domain_restriction() -> None:
    idp = FakeIdP()
    idp.claims_extra["hd"] = "example.com"

    _, claims = await sign_in(make_verifier(idp), idp)

    assert claims.hosted_domain == "example.com"


# ── provider failures map to one domain error ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(400, {"error": "invalid_grant"}, id="oauth-error"),
        pytest.param(500, {"error": "server_error"}, id="server-error"),
        pytest.param(200, {"access_token": "x", "token_type": "Bearer"}, id="no-id-token"),
        pytest.param(200, {"access_token": "x", "id_token": 42}, id="non-string-id-token"),
        pytest.param(200, {"id_token": "not.a.jwt"}, id="malformed-id-token"),
        pytest.param(200, {"access_token": "x", "expires_in": "soon"}, id="bad-expires-in"),
    ],
)
async def test_token_endpoint_failures_raise_provider_error(
    status: int, body: dict[str, object]
) -> None:
    idp = FakeIdP()
    idp.token_status = status
    idp.token_body = body

    with pytest.raises(OIDCProviderError):
        await sign_in(make_verifier(idp), idp)


@pytest.mark.asyncio
async def test_reused_authorization_code_raises_provider_error() -> None:
    idp = FakeIdP()
    verifier = make_verifier(idp)
    request = await verifier.authorization_request()
    callback = idp.authorize(request.url)
    await finish(verifier, request, callback)

    with pytest.raises(OIDCProviderError):
        await finish(verifier, request, callback)


@pytest.mark.asyncio
async def test_wrong_pkce_verifier_is_refused_by_the_provider() -> None:
    idp = FakeIdP()
    verifier = make_verifier(idp)
    request = await verifier.authorization_request()
    callback = idp.authorize(request.url)
    forged = dataclasses.replace(request, code_verifier="a" * 64)

    with pytest.raises(OIDCProviderError):
        await finish(verifier, forged, callback)


@pytest.mark.asyncio
async def test_unreachable_discovery_raises_provider_error() -> None:
    idp = FakeIdP()
    idp.raise_on = "/.well-known/openid-configuration"

    with pytest.raises(OIDCProviderError):
        await make_verifier(idp).authorization_request()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/token", "/jwks"])
async def test_unreachable_token_or_jwks_endpoint_raises_provider_error(path: str) -> None:
    idp = FakeIdP()
    verifier = make_verifier(idp)
    request = await verifier.authorization_request()
    callback = idp.authorize(request.url)
    idp.raise_on = path

    with pytest.raises(OIDCProviderError):
        await finish(verifier, request, callback)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        pytest.param(["not", "an", "object"], id="not-an-object"),
        pytest.param({"issuer": ISSUER}, id="no-endpoints"),
        pytest.param(
            {
                "issuer": ISSUER,
                "authorization_endpoint": "javascript:alert(1)",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
            },
            id="non-http-endpoint",
        ),
    ],
)
async def test_unusable_discovery_documents_raise_provider_error(document: object) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=document)

    verifier = OIDCVerifier(
        FakeIdP().provider_config(),
        OIDCHttpOptions(transport=httpx.MockTransport(handle), cache=OIDCMetadataCache()),
    )

    with pytest.raises(OIDCProviderError):
        await verifier.authorization_request()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization_endpoint",
    [
        pytest.param("http://[::1/authorize", id="unclosed-ipv6-bracket"),
        pytest.param("http://exa℀mple.test/authorize", id="host-changes-under-nfkc"),
        pytest.param("http://idp.test:abc/authorize", id="non-numeric-port"),
        pytest.param("http:///authorize", id="no-host"),
    ],
)
async def test_unusable_authorization_endpoint_raises_provider_error(
    authorization_endpoint: str,
) -> None:
    idp = FakeIdP()
    idp.discovery_extra["authorization_endpoint"] = authorization_endpoint

    with pytest.raises(OIDCProviderError):
        await make_verifier(idp).authorization_request()


@pytest.mark.asyncio
async def test_authorization_endpoint_authlib_cannot_build_on_raises_provider_error() -> None:
    idp = FakeIdP()
    # A JSON-escaped lone surrogate in the query parses, and passes the URL
    # check, but authlib cannot UTF-8-encode it into the authorize redirect.
    idp.discovery_extra["authorization_endpoint"] = f"{ISSUER}/authorize?x=\ud800"

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(idp._discovery_document()).encode("ascii"))

    verifier = OIDCVerifier(
        idp.provider_config(),
        OIDCHttpOptions(transport=httpx.MockTransport(handle), cache=OIDCMetadataCache()),
    )

    with pytest.raises(OIDCProviderError):
        await verifier.authorization_request()


@pytest.mark.asyncio
async def test_non_json_discovery_raises_provider_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    verifier = OIDCVerifier(
        FakeIdP().provider_config(),
        OIDCHttpOptions(transport=httpx.MockTransport(handle), cache=OIDCMetadataCache()),
    )

    with pytest.raises(OIDCProviderError):
        await verifier.authorization_request()


# ── the optional egress proxy ───────────────────────────────────────────────


class FakeForwardProxy:
    """A plain-HTTP forward proxy on localhost that answers from a `FakeIdP`.

    It records each absolute-form request line, proving the verifier's
    traffic really went through ``OIDCHttpOptions.proxy``.
    """

    def __init__(self, idp: FakeIdP) -> None:
        self.idp = idp
        self.request_lines: list[str] = []
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> str:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def __aexit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request_line = (await reader.readline()).decode().strip()
                if not request_line:
                    return
                method, target, _ = request_line.split(" ", 2)
                self.request_lines.append(f"{method} {target}")
                headers: dict[str, str] = {}
                while (line := (await reader.readline()).decode().strip()) != "":
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                upstream = httpx.Request(method, target, headers=headers, content=body)
                response = self.idp.handle(upstream)
                head = f"HTTP/1.1 {response.status_code} OK\r\n"
                head += f"Content-Type: {response.headers.get('content-type', 'text/plain')}\r\n"
                head += f"Content-Length: {len(response.content)}\r\n\r\n"
                writer.write(head.encode() + response.content)
                await writer.drain()
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_every_provider_call_goes_through_the_configured_proxy() -> None:
    idp = FakeIdP()
    proxy = FakeForwardProxy(idp)
    async with proxy as proxy_url:
        verifier = OIDCVerifier(
            idp.provider_config(),
            OIDCHttpOptions(proxy=proxy_url, cache=OIDCMetadataCache()),
        )
        _, claims = await sign_in(verifier, idp)

    assert claims.subject == "subject-ada"
    assert proxy.request_lines == [
        f"GET {idp.discovery_url}",
        f"POST {idp.token_endpoint}",
        f"GET {idp.jwks_uri}",
    ]
