from dataclasses import replace
import re
from uuid import uuid4

from httpx import AsyncClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Identity, LocalCredential, User, Workspace
from app.services.auth_service import (
    BasicAuthService,
    DuplicateUserError,
    OIDCAccountExistsError,
    OIDCAuthService,
    OIDCClaims,
)
from test_notebooks import json_dict

# Fake credentials used across the auth tests. Routed through module-level
# constants so the values are never string literals at sensitive call sites.
_LOGIN_VALUE = "correct-horse"
_BEARER_TYPE = "bearer"


async def register_user(
    client: AsyncClient,
    username: str = "ada",
    email: str = "ada@example.com",
    password: str = _LOGIN_VALUE,
    display_name: str | None = None,
) -> dict[str, object]:
    payload = {"username": username, "email": email, "password": password}
    if display_name is not None:
        payload["display_name"] = display_name
    response = await client.post("/api/auth/register", json=payload)
    assert response.status_code == 201
    return json_dict(response)


async def login_user(
    client: AsyncClient, username: str = "ada", password: str = _LOGIN_VALUE
) -> str:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    payload = json_dict(response)
    assert payload["token_type"] == _BEARER_TYPE
    return str(payload["access_token"])


@pytest.mark.asyncio
async def test_register_login_and_logout_with_bearer_token(api_client: AsyncClient) -> None:
    user = await register_user(api_client)
    token = await login_user(api_client)
    logout_response = await api_client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )

    assert user["username"] == "ada"
    assert user["email"] == "ada@example.com"
    assert logout_response.status_code == 204
    assert logout_response.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        pytest.param("Ada Lovelace", "Ada Lovelace", id="name"),
        pytest.param("  Ada Lovelace \t", "Ada Lovelace", id="trimmed"),
        pytest.param("x" * 255, "x" * 255, id="longest"),
        pytest.param(" " + "x" * 255 + " ", "x" * 255, id="fits-once-trimmed"),
        pytest.param("", None, id="empty-means-none"),
        pytest.param("   ", None, id="blank-means-none"),
        pytest.param(None, None, id="null"),
        # Persian and Indic names need the zero-width non-joiner.
        pytest.param("Nima" + chr(0x200C) + "pour", "Nima" + chr(0x200C) + "pour", id="joiner"),
        pytest.param("José Núñez", "José Núñez", id="accents"),
    ],
)
async def test_register_accepts_an_optional_display_name(
    api_client: AsyncClient, db_session: AsyncSession, sent: str | None, stored: str | None
) -> None:
    response = await api_client.post(
        "/api/auth/register",
        json={
            "username": "ada",
            "email": "ada@example.com",
            "password": _LOGIN_VALUE,
            "display_name": sent,
        },
    )

    assert response.status_code == 201
    assert json_dict(response)["display_name"] == stored
    assert await db_session.scalar(select(User.display_name)) == stored


@pytest.mark.asyncio
async def test_register_without_a_display_name_has_none(api_client: AsyncClient) -> None:
    user = await register_user(api_client)

    assert user["display_name"] is None
    assert set(user) == {"id", "username", "email", "display_name", "created_at"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "display_name",
    [
        pytest.param("x" * 256, id="too-long"),
        pytest.param(42, id="not-a-string"),
        # Regression: each was stored unchanged (201), and NUL was a 500.
        pytest.param("Karl Schmidt" + chr(0x202E), id="right-to-left-override"),
        pytest.param("Karl" + chr(0x2066) + " Schmidt", id="bidi-isolate"),
        pytest.param("Ka" + chr(0x200B) + "rl Schmidt", id="zero-width-space"),
        pytest.param("Karl" + chr(10) + "Schmidt", id="newline"),
        pytest.param("Karl" + chr(7) + "Schmidt", id="bell"),
        pytest.param("Ada" + chr(0), id="nul"),
    ],
)
async def test_register_rejects_an_unusable_display_name(
    api_client: AsyncClient, db_session: AsyncSession, display_name: object
) -> None:
    response = await api_client.post(
        "/api/auth/register",
        json={
            "username": "ada",
            "email": "ada@example.com",
            "password": _LOGIN_VALUE,
            "display_name": display_name,
        },
    )

    assert response.status_code == 422
    assert (await db_session.execute(select(User))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "username",
    [
        # Regression: "kschmid<U+200B>t" registered (201) and rendered exactly
        # like the existing "kschmidt" in the person search.
        pytest.param("kschmid" + chr(0x200B) + "t", id="zero-width-space"),
        pytest.param("kschmidt" + chr(0x202E), id="right-to-left-override"),
        pytest.param("kschmid" + chr(0x1160) + "t", id="hangul-filler"),
        pytest.param("kschmid" + chr(0x034F) + "t", id="combining-grapheme-joiner"),
        pytest.param("kschmidt" + chr(0xFE0F), id="variation-selector"),
        pytest.param("kschmidt ", id="trailing-space"),
        pytest.param(" kschmidt", id="leading-space"),
        pytest.param("ksch" + chr(0xA0) + "midt", id="no-break-space"),
        pytest.param(chr(0xFF4B) + "schmidt", id="full-width-letter"),
        pytest.param("o" + chr(0xFB00) + "ice", id="ligature"),
        pytest.param("Jose" + chr(0x0301), id="separately-composed-accent"),
        pytest.param("ksch" + chr(10) + "midt", id="newline"),
        pytest.param("ksch" + chr(0) + "midt", id="nul"),
    ],
)
async def test_register_refuses_a_username_that_could_pass_for_another(
    api_client: AsyncClient, db_session: AsyncSession, username: str
) -> None:
    response = await api_client.post(
        "/api/auth/register",
        json={"username": username, "email": "k@example.com", "password": _LOGIN_VALUE},
    )

    assert response.status_code == 422
    assert (await db_session.execute(select(User))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "username", ["kschmidt", "Karl Schmidt", "karl.schmidt@example.com", "José", "李小龍"]
)
async def test_register_still_accepts_ordinary_usernames(
    api_client: AsyncClient, username: str
) -> None:
    user = await register_user(api_client, username=username)

    assert user["username"] == username


@pytest.mark.asyncio
async def test_register_and_login_refuse_nul_with_a_422_not_a_server_error(
    api_client: AsyncClient,
) -> None:
    # Regression: NUL reached PostgreSQL, which cannot store it: a 500, and a
    # logged traceback quoting the username and email.
    nul = chr(0)
    register = await api_client.post(
        "/api/auth/register",
        json={"username": "ada", "email": f"ada{nul}@example.com", "password": _LOGIN_VALUE},
    )
    login = await api_client.post(
        "/api/auth/login", json={"username": f"a{nul}da", "password": _LOGIN_VALUE}
    )

    assert register.status_code == 422
    assert login.status_code == 422


@pytest.mark.asyncio
async def test_login_still_accepts_a_username_registered_before_usernames_were_checked(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    legacy = "tab" + chr(9) + "user"  # a control character, refused for new accounts
    await BasicAuthService(db_session).register(legacy, "legacy@example.com", _LOGIN_VALUE)

    await login_user(api_client, username=legacy)


@pytest.mark.asyncio
async def test_register_duplicate_username_or_email_returns_409(api_client: AsyncClient) -> None:
    await register_user(api_client)

    duplicate_username = await api_client.post(
        "/api/auth/register",
        json={"username": "ada", "email": "other@example.com", "password": "correct-horse"},
    )
    duplicate_email = await api_client.post(
        "/api/auth/register",
        json={"username": "grace", "email": "ada@example.com", "password": "correct-horse"},
    )

    assert duplicate_username.status_code == 409
    assert duplicate_email.status_code == 409


@pytest.mark.asyncio
async def test_bad_credentials_return_401(api_client: AsyncClient) -> None:
    await register_user(api_client)

    wrong_password = await api_client.post(
        "/api/auth/login", json={"username": "ada", "password": "wrong-password"}
    )
    missing_user = await api_client.post(
        "/api/auth/login", json={"username": "missing", "password": "correct-horse"}
    )

    assert wrong_password.status_code == 401
    assert missing_user.status_code == 401


@pytest.mark.asyncio
async def test_logout_requires_valid_token(api_client: AsyncClient) -> None:
    without_token = await api_client.post("/api/auth/logout")
    invalid_token = await api_client.post(
        "/api/auth/logout", headers={"Authorization": "Bearer not-a-token"}
    )

    assert without_token.status_code == 401
    assert invalid_token.status_code == 401


@pytest.mark.asyncio
async def test_register_creates_user_local_identity_and_credentials_no_workspace(
    db_session: AsyncSession,
) -> None:
    service = BasicAuthService(db_session)
    user = await service.register("ada", "Ada@Example.com", _LOGIN_VALUE)

    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))
    credential = await db_session.scalar(
        select(LocalCredential).where(LocalCredential.user_id == user.id)
    )
    workspaces = (await db_session.execute(select(Workspace))).scalars().all()

    assert user.email == "ada@example.com"
    assert identity is not None
    assert identity.provider == "local"
    assert identity.subject == str(user.id)
    assert credential is not None
    assert workspaces == []


@pytest.mark.asyncio
async def test_register_duplicate_conflict_leaves_no_partial_rows(
    db_session: AsyncSession,
) -> None:
    service = BasicAuthService(db_session)
    await service.register("ada", "ada@example.com", _LOGIN_VALUE)

    with pytest.raises(DuplicateUserError):
        await service.register("ada", "someone-else@example.com", _LOGIN_VALUE)

    users = (await db_session.execute(select(User))).scalars().all()
    identities = (await db_session.execute(select(Identity))).scalars().all()
    credentials = (await db_session.execute(select(LocalCredential))).scalars().all()
    assert len(users) == 1
    assert len(identities) == 1
    assert len(credentials) == 1


@pytest.mark.asyncio
async def test_one_local_identity_per_user_is_db_enforced(db_session: AsyncSession) -> None:
    service = BasicAuthService(db_session)
    user = await service.register("ada", "ada@example.com", _LOGIN_VALUE)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO identities (id, user_id, provider, subject) "
                    "VALUES (:id, :user_id, 'local', :subject)"
                ),
                {"id": uuid4(), "user_id": user.id, "subject": "second-local"},
            )


@pytest.mark.asyncio
async def test_authenticate_success_bumps_last_login_at(db_session: AsyncSession) -> None:
    service = BasicAuthService(db_session)
    user = await service.register("ada", "ada@example.com", _LOGIN_VALUE)
    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))
    assert identity is not None
    before = identity.last_login_at

    authenticated = await service.authenticate("ada", _LOGIN_VALUE)

    await db_session.refresh(identity)
    assert authenticated is not None
    assert authenticated.id == user.id
    assert identity.last_login_at is not None
    assert before is None or identity.last_login_at >= before


@pytest.mark.asyncio
async def test_authenticate_wrong_password_returns_none(db_session: AsyncSession) -> None:
    service = BasicAuthService(db_session)
    await service.register("ada", "ada@example.com", _LOGIN_VALUE)

    assert await service.authenticate("ada", "wrong-password") is None


@pytest.mark.asyncio
async def test_authenticate_unknown_and_sso_only_users_are_indistinguishable(
    db_session: AsyncSession,
) -> None:
    service = BasicAuthService(db_session)
    oidc_service = OIDCAuthService(db_session, "google")
    await oidc_service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="grace@example.com",
            email_verified=True,
            preferred_username="grace",
        )
    )

    assert await service.authenticate("missing", "whatever") is None
    assert await service.authenticate("grace", "whatever") is None


@pytest.mark.asyncio
async def test_oidc_resolve_hit_returns_same_user_and_touches_identity(
    db_session: AsyncSession,
) -> None:
    service = OIDCAuthService(db_session, "google")
    claims = OIDCClaims(
        provider="google",
        subject="google-subject",
        email="grace@example.com",
        email_verified=True,
        preferred_username="grace",
    )
    first = await service.complete_login(claims)
    identity = await db_session.scalar(
        select(Identity).where(Identity.provider == "google", Identity.subject == "google-subject")
    )
    assert identity is not None
    first_login = identity.last_login_at

    second = await service.complete_login(claims)

    await db_session.refresh(identity)
    users = (await db_session.execute(select(User))).scalars().all()
    assert second.id == first.id
    assert len(users) == 1
    assert identity.last_login_at is not None
    assert first_login is None or identity.last_login_at >= first_login


async def _password_less_user(db_session: AsyncSession, email: str) -> User:
    """Provision an account that only signs in through another provider (no password)."""
    return await OIDCAuthService(db_session, "oidc:okta").complete_login(
        OIDCClaims(provider="oidc:okta", subject="okta-subject", email=email, email_verified=True)
    )


async def _identity_providers(db_session: AsyncSession, user: User) -> set[str]:
    rows = await db_session.execute(select(Identity.provider).where(Identity.user_id == user.id))
    return set(rows.scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email", "hosted_domain"),
    [
        pytest.param("ada@gmail.com", None, id="gmail"),
        pytest.param("  Ada@GMail.com  ", None, id="gmail-unnormalized"),
        pytest.param("ada@example.com", "example.com", id="workspace-hd"),
    ],
)
async def test_oidc_trusted_google_links_an_authoritative_email_to_a_password_less_account(
    db_session: AsyncSession, email: str, hosted_domain: str | None
) -> None:
    existing = await _password_less_user(db_session, email.strip().lower())

    service = OIDCAuthService(db_session, "google", trusted_email_linking=True)
    linked = await service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email=email,
            email_verified=True,
            hosted_domain=hosted_domain,
        )
    )

    users = (await db_session.execute(select(User))).scalars().all()
    assert linked.id == existing.id
    assert len(users) == 1
    assert await _identity_providers(db_session, existing) == {"oidc:okta", "google"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email", "email_verified", "hosted_domain"),
    [
        # Google only vouches for @gmail.com and Workspace (hd) addresses; for
        # any other address it cannot say the account still owns it.
        pytest.param("ada@example.com", True, None, id="google-third-party-email"),
        pytest.param("ada@gmail.com", False, None, id="unverified-gmail"),
        pytest.param("ada@example.com", False, "example.com", id="unverified-workspace"),
    ],
)
async def test_oidc_trusted_google_refuses_a_non_authoritative_email(
    db_session: AsyncSession,
    email: str,
    hosted_domain: str | None,
    *,
    email_verified: bool,
) -> None:
    existing = await _password_less_user(db_session, email)

    service = OIDCAuthService(db_session, "google", trusted_email_linking=True)
    with pytest.raises(OIDCAccountExistsError):
        await service.complete_login(
            OIDCClaims(
                provider="google",
                subject="google-subject",
                email=email,
                email_verified=email_verified,
                hosted_domain=hosted_domain,
            )
        )

    assert await _identity_providers(db_session, existing) == {"oidc:okta"}


@pytest.mark.asyncio
async def test_oidc_linking_never_joins_an_account_with_a_local_password(
    db_session: AsyncSession,
) -> None:
    # Local sign-up never verifies an email: whoever registered it may not own it.
    existing = await BasicAuthService(db_session).register("ada", "ada@gmail.com", _LOGIN_VALUE)

    service = OIDCAuthService(db_session, "google", trusted_email_linking=True)
    with pytest.raises(OIDCAccountExistsError):
        await service.complete_login(
            OIDCClaims(
                provider="google",
                subject="google-subject",
                email="ada@gmail.com",
                email_verified=True,
            )
        )

    users = (await db_session.execute(select(User))).scalars().all()
    assert len(users) == 1
    assert await _identity_providers(db_session, existing) == {"local"}


@pytest.mark.asyncio
async def test_oidc_untrusted_provider_refuses_an_existing_email(db_session: AsyncSession) -> None:
    await _password_less_user(db_session, "ada@gmail.com")

    service = OIDCAuthService(db_session, "google")  # trusted_email_linking defaults to False
    with pytest.raises(OIDCAccountExistsError):
        await service.complete_login(
            OIDCClaims(
                provider="google",
                subject="google-subject",
                email="ada@gmail.com",
                email_verified=True,
            )
        )

    users = (await db_session.execute(select(User))).scalars().all()
    assert len(users) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("email_verified", [True, False])
async def test_oidc_trusted_generic_provider_links_on_email_verified_alone(
    db_session: AsyncSession, *, email_verified: bool
) -> None:
    existing = await _password_less_user(db_session, "ada@corp.example")

    service = OIDCAuthService(db_session, "oidc:corp", trusted_email_linking=True)
    claims = OIDCClaims(
        provider="oidc:corp",
        subject="corp-subject",
        email="ada@corp.example",
        email_verified=email_verified,
    )
    if not email_verified:
        with pytest.raises(OIDCAccountExistsError):
            await service.complete_login(claims)
        return

    linked = await service.complete_login(claims)

    assert linked.id == existing.id
    assert await _identity_providers(db_session, existing) == {"oidc:okta", "oidc:corp"}


# ── pre-account hijacking through an OIDC sign-up ────────────────────────────
# Whoever first claims an address through an assertion no provider vouched
# for must not end up sharing the account its real owner is later linked into.


async def _identity_subjects(db_session: AsyncSession, user: User) -> list[str]:
    rows = await db_session.execute(select(Identity.subject).where(Identity.user_id == user.id))
    return sorted(rows.scalars().all())


@pytest.mark.asyncio
async def test_oidc_trusted_linking_refuses_an_account_first_claimed_with_an_unverified_email(
    db_session: AsyncSession,
) -> None:
    # The attacker signs in first, through a provider that does not verify emails.
    attacker = OIDCClaims(
        provider="oidc:corp",
        subject="attacker-sub",
        email="victim@gmail.com",
        email_verified=False,
    )
    squatted = await OIDCAuthService(db_session, "oidc:corp").complete_login(attacker)

    # The victim's own Gmail sign-in, which Google vouches for, is not linked in.
    with pytest.raises(OIDCAccountExistsError):
        await OIDCAuthService(db_session, "google", trusted_email_linking=True).complete_login(
            OIDCClaims(
                provider="google",
                subject="victim-google-sub",
                email="victim@gmail.com",
                email_verified=True,
            )
        )

    again = await OIDCAuthService(db_session, "oidc:corp").complete_login(attacker)
    assert again.id == squatted.id
    assert await _identity_subjects(db_session, squatted) == ["attacker-sub"]


@pytest.mark.asyncio
async def test_oidc_trusted_linking_refuses_an_unverified_account_of_the_same_provider(
    db_session: AsyncSession,
) -> None:
    service = OIDCAuthService(db_session, "oidc:corp", trusted_email_linking=True)
    squatted = await service.complete_login(
        OIDCClaims(
            provider="oidc:corp",
            subject="attacker-sub",
            email="victim@corp.example",
            email_verified=False,
        )
    )

    with pytest.raises(OIDCAccountExistsError):
        await service.complete_login(
            OIDCClaims(
                provider="oidc:corp",
                subject="victim-sub",
                email="victim@corp.example",
                email_verified=True,
            )
        )

    assert await _identity_subjects(db_session, squatted) == ["attacker-sub"]


@pytest.mark.asyncio
async def test_oidc_trusted_google_refuses_a_workspace_identity_into_a_consumer_account(
    db_session: AsyncSession,
) -> None:
    service = OIDCAuthService(db_session, "google", trusted_email_linking=True)
    # A consumer Google account holding a third-party address: verified once,
    # but Google does not vouch that it still owns the address (no `hd`).
    consumer = await service.complete_login(
        OIDCClaims(
            provider="google",
            subject="consumer-sub",
            email="ada@corp.example",
            email_verified=True,
        )
    )

    with pytest.raises(OIDCAccountExistsError):
        await service.complete_login(
            OIDCClaims(
                provider="google",
                subject="workspace-sub",
                email="ada@corp.example",
                email_verified=True,
                hosted_domain="corp.example",
            )
        )

    assert await _identity_subjects(db_session, consumer) == ["consumer-sub"]


@pytest.mark.asyncio
async def test_oidc_trusted_linking_never_adds_a_second_identity_of_the_same_provider(
    db_session: AsyncSession,
) -> None:
    service = OIDCAuthService(db_session, "oidc:corp", trusted_email_linking=True)
    first = await service.complete_login(
        OIDCClaims(
            provider="oidc:corp", subject="first-sub", email="ada@corp.example", email_verified=True
        )
    )

    with pytest.raises(OIDCAccountExistsError):
        await service.complete_login(
            OIDCClaims(
                provider="oidc:corp",
                subject="second-sub",
                email="ada@corp.example",
                email_verified=True,
            )
        )

    assert await _identity_subjects(db_session, first) == ["first-sub"]


@pytest.mark.asyncio
async def test_oidc_trusted_linking_follows_each_identitys_latest_sign_in(
    db_session: AsyncSession,
) -> None:
    okta = OIDCAuthService(db_session, "oidc:okta")
    vouched = OIDCClaims(
        provider="oidc:okta", subject="okta-subject", email="ada@corp.example", email_verified=True
    )
    existing = await okta.complete_login(vouched)
    await okta.complete_login(replace(vouched, email_verified=False))
    google = OIDCAuthService(db_session, "google", trusted_email_linking=True)
    workspace = OIDCClaims(
        provider="google",
        subject="google-subject",
        email="ada@corp.example",
        email_verified=True,
        hosted_domain="corp.example",
    )

    # The account's only identity no longer vouches for the address.
    with pytest.raises(OIDCAccountExistsError):
        await google.complete_login(workspace)

    await okta.complete_login(vouched)
    linked = await google.complete_login(workspace)

    assert linked.id == existing.id
    assert await _identity_providers(db_session, existing) == {"oidc:okta", "google"}


@pytest.mark.asyncio
async def test_identities_record_whether_their_email_was_vouched_for(
    db_session: AsyncSession,
) -> None:
    local = await BasicAuthService(db_session).register("ada", "ada@gmail.com", _LOGIN_VALUE)
    assert await BasicAuthService(db_session).authenticate("ada", _LOGIN_VALUE) is not None
    google = OIDCAuthService(db_session, "google")
    vouched = await google.complete_login(
        OIDCClaims(provider="google", subject="g-1", email="grace@gmail.com", email_verified=True)
    )
    # Not vouched for, yet still provisioned just in time.
    unvouched = await google.complete_login(
        OIDCClaims(
            provider="google", subject="g-2", email="hopper@example.com", email_verified=True
        )
    )

    rows = await db_session.execute(select(Identity.user_id, Identity.email_authoritative))
    assert dict(rows.tuples().all()) == {local.id: False, vouched.id: True, unvouched.id: False}


@pytest.mark.parametrize(
    ("provider", "email", "email_verified", "hosted_domain", "expected"),
    [
        ("google", "ada@gmail.com", True, None, True),
        ("google", "ADA@GMAIL.COM ", True, None, True),
        ("google", "ada@example.com", True, "example.com", True),
        ("google", "ada@example.com", True, None, False),
        ("google", "ada@gmail.com.evil.test", True, None, False),
        ("google", "ada@gmail.com", False, None, False),
        ("oidc:corp", "ada@example.com", True, None, True),
        ("oidc:corp", "ada@example.com", False, None, False),
    ],
)
def test_email_is_authoritative(
    provider: str,
    email: str,
    hosted_domain: str | None,
    *,
    email_verified: bool,
    expected: bool,
) -> None:
    claims = OIDCClaims(
        provider=provider,
        subject="subject",
        email=email,
        email_verified=email_verified,
        hosted_domain=hosted_domain,
    )

    assert claims.email_is_authoritative is expected


@pytest.mark.asyncio
async def test_oidc_first_login_race_resolves_to_the_winning_account(
    db_session: AsyncSession,
) -> None:
    claims = OIDCClaims(
        provider="google", subject="google-subject", email="ada@gmail.com", email_verified=True
    )

    class LosesTheRace(OIDCAuthService):
        async def _available_username(self, claims: OIDCClaims) -> str:
            # A concurrent first login of the same identity commits in between.
            await OIDCAuthService(self.db, claims.provider).complete_login(claims)
            return await super()._available_username(claims)

    user = await LosesTheRace(db_session, "google").complete_login(claims)

    users = (await db_session.execute(select(User))).scalars().all()
    assert [existing.id for existing in users] == [user.id]


@pytest.mark.asyncio
async def test_oidc_provisioning_race_with_another_account_stays_a_conflict(
    db_session: AsyncSession,
) -> None:
    class LosesTheRace(OIDCAuthService):
        async def _available_username(self, claims: OIDCClaims) -> str:
            # Someone else registers the same email in between.
            await BasicAuthService(self.db).register("someone", claims.email, _LOGIN_VALUE)
            return await super()._available_username(claims)

    with pytest.raises(DuplicateUserError):
        await LosesTheRace(db_session, "google").complete_login(
            OIDCClaims(
                provider="google",
                subject="google-subject",
                email="ada@gmail.com",
                email_verified=True,
            )
        )


@pytest.mark.asyncio
async def test_oidc_miss_untrusted_provider_never_links_jit_provisions_instead(
    db_session: AsyncSession,
) -> None:
    basic = BasicAuthService(db_session)
    existing = await basic.register("ada", "ada@example.com", _LOGIN_VALUE)

    service = OIDCAuthService(db_session, "google", trusted_email_linking=False)
    provisioned = await service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="ada.google@example.com",
            email_verified=True,
        )
    )

    assert provisioned.id != existing.id
    users = (await db_session.execute(select(User))).scalars().all()
    assert len(users) == 2


@pytest.mark.asyncio
async def test_oidc_miss_unverified_email_never_links_jit_provisions_instead(
    db_session: AsyncSession,
) -> None:
    basic = BasicAuthService(db_session)
    existing = await basic.register("ada", "ada@example.com", _LOGIN_VALUE)

    service = OIDCAuthService(db_session, "google", trusted_email_linking=True)
    provisioned = await service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="ada.google@example.com",
            email_verified=False,
        )
    )

    assert provisioned.id != existing.id
    users = (await db_session.execute(select(User))).scalars().all()
    assert len(users) == 2


@pytest.mark.asyncio
async def test_oidc_jit_username_collision_gets_suffixed(db_session: AsyncSession) -> None:
    basic = BasicAuthService(db_session)
    await basic.register("grace", "grace-local@example.com", _LOGIN_VALUE)

    service = OIDCAuthService(db_session, "google")
    provisioned = await service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="grace@example.com",
            email_verified=True,
            preferred_username="grace",
        )
    )

    assert provisioned.username == "grace-2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preferred_username", "name", "username"),
    [
        # Regression: Google sends no preferred_username, so this account was
        # "karthedew", which with its "k•••@gmail.com" hint spells the address.
        pytest.param(None, "Karl Schmidt", "karl-schmidt", id="google-uses-the-name"),
        pytest.param("kschmidt", "Karl Schmidt", "kschmidt", id="preferred-username-first"),
        pytest.param(
            "karthedew@gmail.com", "Karl Schmidt", "karl-schmidt", id="an-address-is-no-seed"
        ),
        pytest.param("___", "Karl Schmidt", "karl-schmidt", id="no-letters-is-no-seed"),
        pytest.param(None, "José Núñez", "jose-nunez", id="accents-folded"),
    ],
)
async def test_oidc_jit_username_never_comes_from_the_email(
    db_session: AsyncSession, preferred_username: str | None, name: str, username: str
) -> None:
    user = await OIDCAuthService(db_session, "google").complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="karthedew@gmail.com",
            email_verified=True,
            preferred_username=preferred_username,
            name=name,
        )
    )

    assert user.username == username


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [None, "李小龍"], ids=["no-name", "no-latin-letters"])
async def test_oidc_jit_username_without_a_seed_is_a_random_handle(
    db_session: AsyncSession, name: str | None
) -> None:
    user = await OIDCAuthService(db_session, "google").complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="karthedew@gmail.com",
            email_verified=True,
            name=name,
        )
    )

    assert re.fullmatch(r"user-[0-9a-f]{8}", user.username), user.username


# ── display name from the OIDC `name` claim ──────────────────────────────────


async def _stored_display_name(db_session: AsyncSession, user: User) -> str | None:
    """Read the column itself, not the session's (possibly stale) identity map."""
    return await db_session.scalar(select(User.display_name).where(User.id == user.id))


@pytest.mark.asyncio
async def test_oidc_jit_provisioning_takes_the_display_name_from_the_name_claim(
    db_session: AsyncSession,
) -> None:
    user = await OIDCAuthService(db_session, "google").complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="grace@gmail.com",
            email_verified=True,
            name="Grace Hopper",
        )
    )

    assert user.display_name == "Grace Hopper"
    assert await _stored_display_name(db_session, user) == "Grace Hopper"


@pytest.mark.asyncio
async def test_oidc_sign_in_fills_a_missing_display_name_but_never_replaces_one(
    db_session: AsyncSession,
) -> None:
    service = OIDCAuthService(db_session, "google")
    claims = OIDCClaims(
        provider="google", subject="google-subject", email="grace@gmail.com", email_verified=True
    )
    user = await service.complete_login(claims)  # the provider sent no name at first
    assert await _stored_display_name(db_session, user) is None

    await service.complete_login(replace(claims, name="Grace Hopper"))
    assert await _stored_display_name(db_session, user) == "Grace Hopper"

    await service.complete_login(replace(claims, name="Rear Admiral Hopper"))
    await service.complete_login(claims)  # and no name at all again
    assert await _stored_display_name(db_session, user) == "Grace Hopper"


@pytest.mark.asyncio
async def test_oidc_sign_in_never_replaces_a_display_name_set_since_the_account_was_read(
    db_session: AsyncSession,
) -> None:
    service = OIDCAuthService(db_session, "google")
    claims = OIDCClaims(
        provider="google", subject="google-subject", email="grace@gmail.com", email_verified=True
    )
    user = await service.complete_login(claims)
    # Another sign-in names the account behind this session's back: the
    # session still holds the account as unnamed.
    await db_session.execute(
        text("UPDATE users SET display_name = 'Set elsewhere' WHERE id = :id"), {"id": user.id}
    )
    await db_session.commit()

    await service.complete_login(replace(claims, name="Grace Hopper"))

    assert await _stored_display_name(db_session, user) == "Set elsewhere"


@pytest.mark.asyncio
async def test_oidc_linking_fills_a_missing_display_name_but_never_replaces_one(
    db_session: AsyncSession,
) -> None:
    unnamed = await _password_less_user(db_session, "ada@corp.example")
    named = await OIDCAuthService(db_session, "oidc:okta").complete_login(
        OIDCClaims(
            provider="oidc:okta",
            subject="okta-grace",
            email="grace@corp.example",
            email_verified=True,
            name="Grace Hopper",
        )
    )
    corp = OIDCAuthService(db_session, "oidc:corp", trusted_email_linking=True)

    for subject, email, name in [
        ("corp-ada", "ada@corp.example", "Ada Lovelace"),
        ("corp-grace", "grace@corp.example", "Amazing Grace"),
    ]:
        await corp.complete_login(
            OIDCClaims(
                provider="oidc:corp",
                subject=subject,
                email=email,
                email_verified=True,
                name=name,
            )
        )

    assert await _identity_providers(db_session, unnamed) == {"oidc:okta", "oidc:corp"}
    assert await _identity_providers(db_session, named) == {"oidc:okta", "oidc:corp"}
    assert await _stored_display_name(db_session, unnamed) == "Ada Lovelace"
    assert await _stored_display_name(db_session, named) == "Grace Hopper"


@pytest.mark.asyncio
async def test_local_sign_in_leaves_the_display_name_alone(db_session: AsyncSession) -> None:
    service = BasicAuthService(db_session)
    user = await service.register("ada", "ada@example.com", _LOGIN_VALUE, display_name="Ada")

    assert await service.authenticate("ada", _LOGIN_VALUE) is not None
    assert await _stored_display_name(db_session, user) == "Ada"
