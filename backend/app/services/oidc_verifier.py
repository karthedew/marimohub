"""OpenID Connect authorization-code flow, with PKCE, against one identity provider.

`OIDCVerifier` isolates every call to the identity provider (discovery, the
token exchange, the signing keys) and id_token verification from
`OIDCAuthService`, which only ever consumes the `OIDCClaims` produced here, so
provisioning stays a pure database path that tests drive with hand-built
claims.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import logging
import re
import secrets
import time
from typing import cast
from urllib.parse import urlsplit

from authlib.common.errors import AuthlibBaseError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2 import OAuth2Client
import httpx
from joserfc import jwt as rfc_jwt
from joserfc.errors import InvalidClaimError, JoseError
from joserfc.jwk import DictKey, JWKRegistry, Key, KeySet
from joserfc.jws import extract_compact

from app.core.errors import DomainError
from app.core.security import constant_time_equals
from app.services.auth_service import OIDCClaims
from app.services.identity_text import check_display_name

logger = logging.getLogger(__name__)

# Asymmetric-only allow-list: id_tokens must be RS256, never a symmetric
# algorithm (HS256) or "none", either of which would let a token be forged
# with a value the verifier itself holds (e.g. the configured client secret).
# RS256 is also the one algorithm every OpenID provider must support.
_ALLOWED_ID_TOKEN_ALGORITHMS = ("RS256",)
_HTTP_TIMEOUT = httpx.Timeout(10.0)
_CLOCK_SKEW_LEEWAY_SECONDS = 60
_DISCOVERY_TTL_SECONDS = 3600.0
_JWKS_MIN_TTL_SECONDS = 300.0
_JWKS_MAX_TTL_SECONDS = 86_400.0
_MAX_CLAIM_LENGTH = 255  # width of identities.subject, users.email and users.display_name
_GOOGLE_LEGACY_ISSUER = "accounts.google.com"
_MAX_AGE_RE = re.compile(r"(?:^|[\s,])max-age\s*=\s*\"?(\d+)", re.IGNORECASE)
# Everything a call into httpx/authlib may raise while talking to, or parsing
# the answer of, an identity provider.
_UPSTREAM_ERRORS = (
    httpx.HTTPError,
    httpx.InvalidURL,
    AuthlibBaseError,
    KeyError,
    TypeError,
    ValueError,
)


class OIDCProviderError(DomainError):
    """The identity provider failed, or could not prove the login it returned."""

    status = 502

    def __init__(self, message: str) -> None:
        """Keep ``message`` for logs; clients only see a generic detail."""
        super().__init__(message, detail="The identity provider could not complete the sign-in")


class OIDCDomainNotAllowedError(DomainError):
    """A verified account outside the provider's configured hosted domain."""

    status = 403

    def __init__(self) -> None:
        """Set the client-safe detail."""
        super().__init__("This account's domain is not allowed to sign in")


@dataclass(frozen=True, slots=True)
class OIDCProviderConfig:
    """Everything one configured OIDC provider needs to run the code flow."""

    provider: str  # the identities.provider value, e.g. "google" or "oidc:okta"
    kind: str  # "google" | "oidc" | "saml"
    issuer: str  # exactly as configured; compared as a plain string everywhere
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    hosted_domain: str | None = None  # lowercase; enforced against the `hd` claim
    prompt: str | None = None

    @property
    def accepted_issuers(self) -> tuple[str, ...]:
        """Return the id_token ``iss`` values this provider signs with.

        Google documents both its issuer URL and the legacy bare host.
        """
        if self.kind == "google":
            return (self.issuer, _GOOGLE_LEGACY_ISSUER)
        return (self.issuer,)


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """A fresh authorize redirect plus what its callback must be checked against."""

    url: str
    state: str
    nonce: str
    code_verifier: str  # the backend's own PKCE verifier; never sent to the browser


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """The subset of an OpenID discovery document the code flow uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    # RFC 9207 `authorization_response_iss_parameter_supported`
    iss_parameter_supported: bool
    token_endpoint_auth_methods: tuple[str, ...]

    @classmethod
    def from_document(cls, document: object) -> ProviderMetadata:
        """Parse a discovery document, rejecting one that lacks a usable endpoint."""
        if not isinstance(document, dict):
            raise OIDCProviderError("discovery document is not a JSON object")
        fields = cast("dict[str, object]", document)  # a parsed JSON object
        methods = fields.get("token_endpoint_auth_methods_supported")
        iss_supported = fields.get("authorization_response_iss_parameter_supported")
        return cls(
            issuer=_string_field(fields, "issuer"),
            authorization_endpoint=_url_field(fields, "authorization_endpoint"),
            token_endpoint=_url_field(fields, "token_endpoint"),
            jwks_uri=_url_field(fields, "jwks_uri"),
            iss_parameter_supported=iss_supported is True,
            token_endpoint_auth_methods=tuple(
                method
                for method in (methods if isinstance(methods, list) else [])
                if isinstance(method, str)
            ),
        )

    @property
    def client_auth_method(self) -> str:
        """Pick token-endpoint client authentication: basic unless only post is offered."""
        methods = self.token_endpoint_auth_methods
        if methods and "client_secret_basic" not in methods and "client_secret_post" in methods:
            return "client_secret_post"
        return "client_secret_basic"  # OpenID Discovery's default when the list is omitted


def _string_field(document: dict[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise OIDCProviderError(f"discovery document has no {name}")
    return value


def _url_field(document: dict[str, object], name: str) -> str:
    value = _string_field(document, name)
    if not value.startswith(("https://", "http://")) or not _parses_with_a_host(value):
        raise OIDCProviderError(f"discovery document's {name} is not an http(s) URL")
    return value


def _parses_with_a_host(url: str) -> bool:
    """Return whether ``url`` parses, as authlib parses the authorize URL, with a host.

    A port, if any, must be numeric too: `urlsplit` only checks it when read.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:  # e.g. an unclosed IPv6 bracket, or a host NFKC rewrites
        return False
    return bool(parts.hostname) and port != 0


class OIDCMetadataCache:
    """Process-local TTL cache of discovery documents and signing-key sets.

    A discovery document is kept for an hour; a key set for its response's
    ``Cache-Control: max-age``, clamped to between 5 minutes and 24 hours.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Create an empty cache measuring expiry with ``clock`` (seconds)."""
        self._clock = clock
        self._metadata: dict[str, tuple[float, ProviderMetadata]] = {}
        self._key_sets: dict[str, tuple[float, KeySet]] = {}

    def metadata(self, issuer: str) -> ProviderMetadata | None:
        """Return the unexpired discovery metadata for ``issuer``, if any."""
        return _unexpired(self._metadata.get(issuer), self._clock())

    def store_metadata(self, issuer: str, metadata: ProviderMetadata) -> None:
        """Cache ``metadata`` for ``issuer``."""
        self._metadata[issuer] = (self._clock() + _DISCOVERY_TTL_SECONDS, metadata)

    def key_set(self, jwks_uri: str) -> KeySet | None:
        """Return the unexpired key set fetched from ``jwks_uri``, if any."""
        return _unexpired(self._key_sets.get(jwks_uri), self._clock())

    def store_key_set(self, jwks_uri: str, key_set: KeySet, ttl_seconds: float) -> None:
        """Cache ``key_set`` for ``ttl_seconds``."""
        self._key_sets[jwks_uri] = (self._clock() + ttl_seconds, key_set)

    def clear(self) -> None:
        """Forget everything cached."""
        self._metadata.clear()
        self._key_sets.clear()


def _unexpired[T](entry: tuple[float, T] | None, now: float) -> T | None:
    if entry is None or entry[0] <= now:
        return None
    return entry[1]


_PROCESS_CACHE = OIDCMetadataCache()


@dataclass(frozen=True, slots=True)
class OIDCHttpOptions:
    """How the verifier reaches identity providers."""

    proxy: str | None = None  # Settings.OIDC_HTTP_PROXY_URL
    transport: httpx.AsyncBaseTransport | None = None  # tests inject a fake provider here
    cache: OIDCMetadataCache = field(default_factory=lambda: _PROCESS_CACHE)


class OIDCVerifier:
    """Runs the authorization-code flow and verifies the resulting id_token.

    Discovery and signing keys are fetched with a plain HTTP client that never
    carries a token issued by the provider; only the token exchange uses the
    authlib OAuth client. Every client is closed after use, and every
    provider or verification failure surfaces as `OIDCProviderError`.
    """

    def __init__(self, config: OIDCProviderConfig, options: OIDCHttpOptions | None = None) -> None:
        """Bind the verifier to one configured provider."""
        self._config = config
        self._options = options if options is not None else OIDCHttpOptions()

    async def authorization_request(self) -> AuthorizationRequest:
        """Return the IdP authorize redirect for a fresh login attempt.

        The attempt gets its own ``state``, ``nonce`` and an S256 PKCE
        verifier; the challenge method is always sent explicitly, since an
        omitted one means ``plain``.
        """
        metadata = await self._metadata()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        extra: dict[str, str] = {"nonce": nonce}
        if self._config.prompt:
            extra["prompt"] = self._config.prompt
        if self._config.hosted_domain:
            extra["hd"] = self._config.hosted_domain
        # authlib's base client formats the URL without any network I/O, so
        # no HTTP client is opened (or left unclosed) just to build a redirect.
        client = OAuth2Client(
            None,
            client_id=self._config.client_id,
            scope=" ".join(self._config.scopes),
            redirect_uri=self._config.redirect_uri,
            code_challenge_method="S256",
        )
        try:
            url, _ = client.create_authorization_url(
                metadata.authorization_endpoint, state=state, code_verifier=code_verifier, **extra
            )
        except ValueError as exc:  # an authorization_endpoint authlib cannot build on
            raise OIDCProviderError(f"cannot build the authorize redirect: {exc!r}") from exc
        return AuthorizationRequest(
            url=str(url), state=state, nonce=nonce, code_verifier=code_verifier
        )

    async def verify_callback(
        self, *, code: str, iss: str | None, nonce: str, code_verifier: str
    ) -> OIDCClaims:
        """Check the callback's ``iss``, redeem ``code`` and verify the id_token.

        Raises `OIDCProviderError` for any provider or verification failure
        and `OIDCDomainNotAllowedError` when a hosted domain is configured
        and the account is outside it.
        """
        metadata = await self._metadata()
        self._check_response_issuer(metadata, iss)
        id_token = await self._exchange_code(metadata, code, code_verifier)
        claims = await self._verified_claims(metadata.jwks_uri, id_token, nonce)
        return self._to_oidc_claims(claims)

    def _check_response_issuer(self, metadata: ProviderMetadata, iss: str | None) -> None:
        """Apply RFC 9207's mix-up defense to the authorization response."""
        if iss is None:
            if metadata.iss_parameter_supported:
                raise OIDCProviderError("authorization response lacks the advertised iss")
            return
        if iss != self._config.issuer:  # RFC 9207 §2.4: simple string comparison
            raise OIDCProviderError("authorization response iss does not match the issuer")

    async def _metadata(self) -> ProviderMetadata:
        cache = self._options.cache
        cached = cache.metadata(self._config.issuer)
        if cached is not None:
            return cached
        url = f"{self._config.issuer.rstrip('/')}/.well-known/openid-configuration"
        document, _ = await self._get_json(url)
        metadata = ProviderMetadata.from_document(document)
        if metadata.issuer != self._config.issuer:  # OpenID Connect Discovery §4.3
            raise OIDCProviderError(
                f"discovery issuer {metadata.issuer!r} is not the configured "
                f"{self._config.issuer!r}"
            )
        cache.store_metadata(self._config.issuer, metadata)
        return metadata

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            proxy=self._options.proxy,
            transport=self._options.transport,
            follow_redirects=False,
        )

    async def _get_json(self, url: str) -> tuple[object, httpx.Headers]:
        """GET a JSON document with a plain client, so no provider token rides along."""
        try:
            async with self._http_client() as client:
                response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            document: object = response.json()
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise OIDCProviderError(f"GET {url} failed: {exc!r}") from exc
        else:
            return document, response.headers

    async def _exchange_code(
        self, metadata: ProviderMetadata, code: str, code_verifier: str
    ) -> str:
        """Redeem the authorization code (with the PKCE verifier) for an id_token."""
        try:
            async with AsyncOAuth2Client(
                client_id=self._config.client_id,
                client_secret=self._config.client_secret,
                token_endpoint_auth_method=metadata.client_auth_method,
                redirect_uri=self._config.redirect_uri,
                timeout=_HTTP_TIMEOUT,
                proxy=self._options.proxy,
                transport=self._options.transport,
                follow_redirects=False,
            ) as client:
                token: object = await client.fetch_token(
                    metadata.token_endpoint, code=code, code_verifier=code_verifier
                )
        except _UPSTREAM_ERRORS as exc:
            raise OIDCProviderError(f"token exchange failed: {exc!r}") from exc
        id_token = token.get("id_token") if isinstance(token, Mapping) else None
        if not isinstance(id_token, str):
            raise OIDCProviderError("token response carried no id_token")
        return id_token

    async def _verified_claims(self, jwks_uri: str, id_token: str, nonce: str) -> dict[str, object]:
        """Verify the id_token's signature and claims (OpenID Connect Core §3.1.3.7)."""
        try:
            claims = await self._decode_id_token(jwks_uri, id_token)
            self._validate_claims(claims, nonce)
        except JoseError as exc:
            raise OIDCProviderError(f"id_token rejected: {exc}") from exc
        return claims

    async def _decode_id_token(self, jwks_uri: str, id_token: str) -> dict[str, object]:
        key_set, fetched = await self._key_set(jwks_uri, refresh=False)
        if not fetched and not _has_signing_key(key_set, id_token):
            # Signed with a key newer than the cached set: the provider has
            # rotated its keys, so refetch them once before deciding.
            key_set, _ = await self._key_set(jwks_uri, refresh=True)
        token = rfc_jwt.decode(id_token, key_set, algorithms=_ALLOWED_ID_TOKEN_ALGORITHMS)
        claims: object = token.claims
        if not isinstance(claims, dict):
            raise InvalidClaimError("payload", "id_token payload is not a JSON object")
        return claims

    async def _key_set(self, jwks_uri: str, *, refresh: bool) -> tuple[KeySet, bool]:
        """Return the provider's signing keys, and whether they were fetched just now."""
        cache = self._options.cache
        if not refresh:
            cached = cache.key_set(jwks_uri)
            if cached is not None:
                return cached, False
        document, headers = await self._get_json(jwks_uri)
        key_set = _import_key_set(document)
        cache.store_key_set(jwks_uri, key_set, _jwks_ttl_seconds(headers))
        return key_set, True

    def _validate_claims(self, claims: dict[str, object], nonce: str) -> None:
        rfc_jwt.JWTClaimsRegistry(
            leeway=_CLOCK_SKEW_LEEWAY_SECONDS,
            iss={"essential": True, "values": list(self._config.accepted_issuers)},
            sub={"essential": True},
            aud={"essential": True},
            exp={"essential": True},
            iat={"essential": True},
            nonce={"essential": True},
        ).validate(claims)
        client_id = self._config.client_id
        # Exactly this client: a token also addressed to another audience
        # could have been obtained by, and replayed from, that other client.
        if claims["aud"] not in (client_id, [client_id]):
            raise InvalidClaimError("aud")
        if claims.get("azp", client_id) != client_id:
            raise InvalidClaimError("azp")
        token_nonce = claims["nonce"]
        if not isinstance(token_nonce, str) or not constant_time_equals(token_nonce, nonce):
            raise InvalidClaimError("nonce")

    def _to_oidc_claims(self, claims: dict[str, object]) -> OIDCClaims:
        hosted_domain = _optional_string(claims.get("hd"))
        required_domain = self._config.hosted_domain
        if required_domain is not None and (hosted_domain or "").lower() != required_domain:
            raise OIDCDomainNotAllowedError
        subject = claims["sub"]
        if not isinstance(subject, str) or len(subject) > _MAX_CLAIM_LENGTH:
            raise OIDCProviderError("id_token sub is not a usable subject")
        email = _optional_string(claims.get("email"))
        if email is None or not email.strip() or len(email) > _MAX_CLAIM_LENGTH:
            raise OIDCProviderError("id_token carried no usable email")
        return OIDCClaims(
            provider=self._config.provider,
            subject=subject,
            email=email,
            # Only a JSON true counts: some providers send the string "false".
            email_verified=claims.get("email_verified") is True,
            preferred_username=_optional_string(claims.get("preferred_username")),
            name=_display_name(claims.get("name")),
            hosted_domain=hosted_domain,
        )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _display_name(value: object) -> str | None:
    """Return the trimmed ``name`` claim, or None when it is unusable as a display name.

    A name is optional profile data, so one that is blank, not a string, too
    long to store, or refused by `check_display_name` is ignored rather than
    failing the sign-in. So is one containing "@": some providers send the
    email address as the name, and a Display Name is shown to every Owner
    whose person search finds the account (docs/adr/0004).
    """
    name = (_optional_string(value) or "").strip()
    if not name or len(name) > _MAX_CLAIM_LENGTH or "@" in name:
        return None
    try:
        check_display_name(name)
    except ValueError:
        return None
    return name


def _has_signing_key(key_set: KeySet, id_token: str) -> bool:
    """Return whether ``key_set`` can hold the key named by the token's ``kid``."""
    kid = extract_compact(id_token.encode("utf-8")).headers().get("kid")
    return kid is None or any(key.kid == kid for key in key_set.keys)


def _import_key_set(document: object) -> KeySet:
    """Import a JWKS document, skipping keys of a type or curve that cannot be used."""
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise OIDCProviderError("JWKS document has no keys array")
    keys: list[Key] = []
    for entry in cast("list[object]", entries):
        if not isinstance(entry, dict):
            continue
        try:
            keys.append(JWKRegistry.import_key(cast("DictKey", entry)))  # a parsed JSON object
        except (JoseError, KeyError, TypeError, ValueError) as exc:
            logger.debug("skipping unusable JWKS entry: %r", exc)
    if not keys:
        raise OIDCProviderError("JWKS document has no usable keys")
    return KeySet(keys)


def _jwks_ttl_seconds(headers: httpx.Headers) -> float:
    """Honor the key set's ``Cache-Control: max-age``, clamped to [5 min, 24 h]."""
    match = _MAX_AGE_RE.search(headers.get("cache-control", ""))
    max_age = float(match.group(1)) if match else 0.0
    return min(max(max_age, _JWKS_MIN_TTL_SECONDS), _JWKS_MAX_TTL_SECONDS)
