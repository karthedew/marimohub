from dataclasses import dataclass

from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc import jwt as rfc_jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from app.core.errors import Unauthenticated
from app.services.auth_service import OIDCClaims

# Asymmetric-only allow-list: id-tokens must be RS256, never a symmetric
# algorithm (HS256/"none"), which would let a token be forged with a value
# the verifier itself controls (e.g. the discovered client secret).
_ALLOWED_ID_TOKEN_ALGORITHMS = ("RS256",)


@dataclass(frozen=True, slots=True)
class OIDCProviderConfig:
    """Everything one configured OIDC provider needs to run the code flow."""

    provider: str  # the identities.provider value, e.g. "google" or "oidc:okta"
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str]


class OIDCVerifier:
    """Runs the authorization-code flow and verifies the resulting id-token.

    Isolates discovery, JWKS fetch, and token exchange/signature verification
    from `OIDCAuthService`, which only ever consumes the `OIDCClaims` this
    adapter produces — so provisioning stays a pure-DB path that tests exercise
    with hand-built claims, never a token or the network.
    """

    def __init__(self, config: OIDCProviderConfig) -> None:
        """Bind the verifier to one configured provider."""
        self._config = config

    async def _discover(self) -> dict[str, object]:
        async with AsyncOAuth2Client() as client:
            response = await client.get(f"{self._config.issuer}/.well-known/openid-configuration")
            response.raise_for_status()
            return response.json()

    async def authorize_redirect_url(self, *, state: str, nonce: str) -> str:
        """Return the IdP authorize URL for a fresh login attempt."""
        metadata = await self._discover()
        client = AsyncOAuth2Client(
            self._config.client_id,
            self._config.client_secret,
            scope=" ".join(self._config.scopes),
            redirect_uri=self._config.redirect_uri,
        )
        url, _ = client.create_authorization_url(
            str(metadata["authorization_endpoint"]), state=state, nonce=nonce
        )
        return url

    async def verify_callback(self, *, code: str, nonce: str) -> OIDCClaims:
        """Exchange the callback code and verify the id-token into `OIDCClaims`."""
        metadata = await self._discover()
        async with AsyncOAuth2Client(
            self._config.client_id,
            self._config.client_secret,
            redirect_uri=self._config.redirect_uri,
        ) as client:
            token = await client.fetch_token(str(metadata["token_endpoint"]), code=code)
            id_token = token.get("id_token")
            if not isinstance(id_token, str):
                raise Unauthenticated("IdP response did not include an id_token")
            jwks_response = await client.get(str(metadata["jwks_uri"]))
            jwks_response.raise_for_status()
            key_set = KeySet.import_key_set(jwks_response.json())

        try:
            decoded = rfc_jwt.decode(id_token, key_set, algorithms=_ALLOWED_ID_TOKEN_ALGORITHMS)
            rfc_jwt.JWTClaimsRegistry(
                iss={"essential": True, "values": [self._config.issuer]},
                aud={"essential": True, "values": [self._config.client_id]},
                nonce={"essential": True, "values": [nonce]},
            ).validate(decoded.claims)
        except JoseError as exc:
            raise Unauthenticated("Invalid id_token") from exc

        claims = decoded.claims
        return OIDCClaims(
            provider=self._config.provider,
            subject=str(claims["sub"]),
            email=str(claims.get("email", "")),
            email_verified=bool(claims.get("email_verified", False)),
            preferred_username=claims.get("preferred_username"),
        )
