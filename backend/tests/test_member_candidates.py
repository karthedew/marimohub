"""Person search: `find_member_candidates` and `GET /api/workspaces/{id}/member-candidates`.

Registration is open, so every rule here doubles as a privacy rule: name
searches never touch emails, email searches only match a whole address, and
only that exact match may echo the address back (docs/adr/0004).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models import User, Workspace, WorkspaceMember, WorkspaceRole
from app.services.auth_service import OIDCAuthService, OIDCClaims
from app.services.member_candidates import (
    MAX_CANDIDATES,
    MemberCandidate,
    escape_like,
    find_member_candidates,
    mask_email,
)


async def _person(
    db: AsyncSession, username: str, *, display_name: str | None = None, email: str | None = None
) -> User:
    user = User(
        username=username,
        email=email if email is not None else f"{username}@example.com",
        display_name=display_name,
    )
    db.add(user)
    await db.flush()
    return user


async def _workspace(db: AsyncSession, owner: User, slug: str = "search-team") -> Workspace:
    workspace = Workspace(slug=slug, name=slug)
    db.add(workspace)
    await db.flush()
    await _join(db, workspace, owner, WorkspaceRole.OWNER)
    return workspace


async def _join(db: AsyncSession, workspace: Workspace, user: User, role: WorkspaceRole) -> None:
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role))
    await db.flush()


async def _usernames(db: AsyncSession, workspace: Workspace, query: str) -> list[str]:
    return [found.username for found in await find_member_candidates(db, workspace.id, query)]


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


# ── rule 1: a user id ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_user_id_finds_that_user_with_a_masked_email(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    grace = await _person(
        db_session, "grace", display_name="Grace Hopper", email="grace@navy.example"
    )
    expected = [MemberCandidate(grace.id, "grace", "Grace Hopper", "g•••@navy.example")]

    assert await find_member_candidates(db_session, workspace.id, str(grace.id)) == expected
    padded_upper = f"  {str(grace.id).upper()}  "
    assert await find_member_candidates(db_session, workspace.id, padded_upper) == expected
    assert await find_member_candidates(db_session, workspace.id, str(uuid4())) == []


@pytest.mark.asyncio
async def test_a_user_id_never_falls_back_to_a_name_search(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    unknown_id = str(uuid4())
    await _person(db_session, "named-after-an-id", display_name=unknown_id)

    assert await _usernames(db_session, workspace, unknown_id) == []


# ── rule 2: an exact email address ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["grace@navy.example", "  Grace@NAVY.example\t", "GRACE@NAVY.EXAMPLE"],
)
async def test_an_exact_email_finds_that_user_with_the_full_email(
    db_session: AsyncSession, query: str
) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    grace = await _person(db_session, "grace", email="grace@navy.example")

    assert await find_member_candidates(db_session, workspace.id, query) == [
        MemberCandidate(grace.id, "grace", None, "grace@navy.example")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("@navy.example", id="domain-only"),
        pytest.param("@navy", id="partial-domain"),
        pytest.param("grace@", id="local-part-only"),
        pytest.param("grace@navy", id="prefix"),
        pytest.param("race@navy.example", id="suffix"),
        pytest.param("g@navy.example", id="initial"),
        pytest.param("navy.example", id="domain-without-at-is-a-name-search"),
        pytest.param("%@navy.example", id="wildcard-local-part"),
        pytest.param("_race@navy.example", id="wildcard-character"),
    ],
)
async def test_a_partial_or_domain_only_email_finds_nobody(
    db_session: AsyncSession, query: str
) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    await _person(db_session, "grace", email="grace@navy.example")
    await _person(db_session, "hopper", email="hopper@navy.example")

    assert await _usernames(db_session, workspace, query) == []


# ── rule 3: a name ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["ada", "ADA", "  aDa  "])
async def test_a_name_search_matches_usernames_and_display_names_best_first(
    db_session: AsyncSession, query: str
) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    for username, display_name in [
        ("nevada", None),  # username contains it
        ("bob", "Grenada Smith"),  # display name contains it
        ("abe-adams", None),  # username contains it, and sorts before "ada"
        ("zed", "Ada Lovelace"),  # display name starts with it
        ("aaron", "Ada Smith"),  # display name starts with it, and sorts before "ada"
        ("adams", None),  # username starts with it
        ("adalbert", None),  # username starts with it
        ("ada", None),  # exactly the username
        ("eve", None),  # no match
    ]:
        await _person(db_session, username, display_name=display_name)
    # Only its email mentions the name: a name search never matches emails.
    await _person(db_session, "carol", email="ada-fan@example.com")

    assert await _usernames(db_session, workspace, query) == [
        "ada",
        "adalbert",
        "adams",
        "aaron",
        "zed",
        "abe-adams",
        "bob",
        "nevada",
    ]


@pytest.mark.asyncio
async def test_exact_usernames_rank_before_longer_ones_whatever_the_collation(
    db_session: AsyncSession,
) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    for number, username in enumerate(["ada-", "ADAM", "ADA", "ada"]):
        await _person(db_session, username, email=f"person-{number}@example.com")

    found = await _usernames(db_session, workspace, "ada")

    # Sorting by username alone interleaves exact and prefix matches under
    # en_US ("ada", "ada-", "ADA", "ADAM") and C ("ADA", "ADAM", "ada", "ada-").
    assert set(found[:2]) == {"ada", "ADA"}
    assert set(found[2:]) == {"ada-", "ADAM"}


@pytest.mark.asyncio
async def test_a_name_search_never_matches_an_email(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    await _person(db_session, "zed", email="secret-handle@hidden.example")

    for query in ["secret-handle", "hidden.example", "handle", "example"]:
        assert await _usernames(db_session, workspace, query) == [], query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param("%%", [], id="percent-signs"),
        pytest.param("__", [], id="underscores"),
        pytest.param("%_", [], id="both-wildcards"),
        pytest.param("_r", ["x_ray"], id="literal-underscore"),
        pytest.param("%o", ["50%off"], id="literal-percent-sign"),
        pytest.param("k\\s", ["back\\slash"], id="literal-escape-character"),
    ],
)
async def test_like_wildcards_in_a_name_search_match_only_themselves(
    db_session: AsyncSession, query: str, expected: list[str]
) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    for username in ["alice", "bob", "carol", "x_ray", "50%off", "back\\slash"]:
        await _person(db_session, username)

    assert await _usernames(db_session, workspace, query) == expected


@pytest.mark.asyncio
async def test_a_name_search_never_shows_an_address_held_in_a_username_or_display_name(
    db_session: AsyncSession,
) -> None:
    # Regression: "corp.example" (no "@", so a name search) listed both of
    # these, each address in full beside its masked hint.
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    await _person(
        db_session,
        "hidden-person",
        display_name="hidden.person@corp.example",  # an identity provider sent the address as name
        email="hidden.person@corp.example",
    )
    await _person(db_session, "other.person@corp.example", email="other.person@corp.example")
    await _person(db_session, "corp-person", display_name="Corp Person")

    assert await _usernames(db_session, workspace, "corp.example") == []
    assert await _usernames(db_session, workspace, "person") == ["corp-person"]
    # Whoever already has the address, or the user id, still finds them.
    assert await _usernames(db_session, workspace, "Hidden.Person@corp.example") == [
        "hidden-person"
    ]
    assert await _usernames(db_session, workspace, "other.person@corp.example") == [
        "other.person@corp.example"
    ]


def test_escape_like_escapes_both_wildcards_and_the_escape_character() -> None:
    assert escape_like("50%_off\\") == "50\\%\\_off\\\\"
    assert escape_like("plain text") == "plain text"


# ── every rule ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_existing_members_are_never_candidates(db_session: AsyncSession) -> None:
    owner = await _person(db_session, "owner")
    workspace = await _workspace(db_session, owner)
    grace = await _person(db_session, "grace", email="grace@navy.example")
    await _join(db_session, workspace, grace, WorkspaceRole.VIEWER)
    await _person(db_session, "gracie")
    # Belonging to some other workspace changes nothing here.
    elsewhere = await _person(db_session, "graceland")
    await _workspace(db_session, elsewhere, slug="elsewhere")

    assert await _usernames(db_session, workspace, str(grace.id)) == []
    assert await _usernames(db_session, workspace, "grace@navy.example") == []
    assert await _usernames(db_session, workspace, str(owner.id)) == []
    assert await _usernames(db_session, workspace, "owner") == []
    assert await _usernames(db_session, workspace, "grac") == ["graceland", "gracie"]


@pytest.mark.asyncio
async def test_at_most_ten_candidates_are_returned_best_first(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    for number in range(1, 13):
        await _person(db_session, f"teammate-{number:02}")
    await _person(db_session, "teammate")

    found = await _usernames(db_session, workspace, "teammate")

    assert len(found) == MAX_CANDIDATES == 10
    assert found == ["teammate"] + [f"teammate-{number:02}" for number in range(1, 10)]


@pytest.mark.asyncio
async def test_only_an_exact_email_search_returns_the_full_email(
    db_session: AsyncSession,
) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))
    grace = await _person(
        db_session, "grace", display_name="Grace Hopper", email="grace.hopper@navy.example"
    )

    hints = {
        query: [
            found.email_hint
            for found in await find_member_candidates(db_session, workspace.id, query)
        ]
        for query in ["grace", "hopper", str(grace.id), "Grace.Hopper@navy.example"]
    }

    assert hints == {
        "grace": ["g•••@navy.example"],
        "hopper": ["g•••@navy.example"],
        str(grace.id): ["g•••@navy.example"],
        "Grace.Hopper@navy.example": ["grace.hopper@navy.example"],
    }


@pytest.mark.parametrize(
    ("email", "hint"),
    [
        ("kate@example.com", "k•••@example.com"),
        ("k@example.com", "k•••@example.com"),
        ("first.last+tag@mail.example.org", "f•••@mail.example.org"),
        ('"a@b"@example.com', '"•••@example.com'),  # the domain follows the last "@"
        ("@example.com", "•••@example.com"),
        ("not-an-address", "n•••"),  # registration never validated it
        ("", "•••"),
    ],
)
def test_mask_email_keeps_one_character_and_the_domain(email: str, hint: str) -> None:
    assert mask_email(email) == hint


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "a", "  a  ", "    ", "x" * 256])
async def test_a_query_of_the_wrong_length_is_refused_rather_than_matching_everyone(
    db_session: AsyncSession, query: str
) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))

    with pytest.raises(ValueError, match="2 to 255 characters"):
        await find_member_candidates(db_session, workspace.id, query)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("ab" + chr(0), id="trailing-nul"),
        pytest.param("a" + chr(0) + "b", id="nul"),
        pytest.param("a" + chr(7) + "b", id="bell"),
    ],
)
async def test_a_query_with_a_control_character_is_refused(
    db_session: AsyncSession, query: str
) -> None:
    workspace = await _workspace(db_session, await _person(db_session, "owner"))

    with pytest.raises(ValueError, match="control characters"):
        await find_member_candidates(db_session, workspace.id, query)


# ── GET /api/workspaces/{workspace_id}/member-candidates ─────────────────────


@pytest.mark.asyncio
async def test_an_owner_finds_a_person_by_name_and_adds_them_by_id(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _person(db_session, "owner")
    workspace = await _workspace(db_session, owner)
    grace = await _person(
        db_session, "grace", display_name="Grace Hopper", email="grace@navy.example"
    )
    search_url = f"/api/workspaces/{workspace.id}/member-candidates"
    members_url = f"/api/workspaces/{workspace.id}/members"

    by_name = await api_client.get(search_url, params={"q": " hopper "}, headers=_bearer(owner))
    assert by_name.status_code == 200
    assert by_name.json() == [
        {
            "user_id": str(grace.id),
            "username": "grace",
            "display_name": "Grace Hopper",
            "email_hint": "g•••@navy.example",
        }
    ]
    by_email = await api_client.get(
        search_url, params={"q": "Grace@Navy.example"}, headers=_bearer(owner)
    )
    assert [found["email_hint"] for found in by_email.json()] == ["grace@navy.example"]

    added = await api_client.post(
        members_url, json={"user_id": str(grace.id), "role": "viewer"}, headers=_bearer(owner)
    )
    assert added.status_code == 201
    assert added.json()["display_name"] == "Grace Hopper"

    again = await api_client.get(search_url, params={"q": "hopper"}, headers=_bearer(owner))
    assert again.status_code == 200
    assert again.json() == []
    members = await api_client.get(members_url, headers=_bearer(owner))
    assert {(m["username"], m["display_name"]) for m in members.json()} == {
        ("owner", None),
        ("grace", "Grace Hopper"),
    }
    promoted = await api_client.patch(
        f"{members_url}/{grace.id}", json={"role": "editor"}, headers=_bearer(owner)
    )
    assert promoted.json()["display_name"] == "Grace Hopper"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("anonymous", 401),
        ("outsider", 404),
        ("viewer", 403),
        ("editor", 403),
        ("owner-of-archived", 404),
        ("owner-of-unknown", 404),
    ],
)
async def test_only_owners_of_an_active_workspace_may_search(
    api_client: AsyncClient, db_session: AsyncSession, caller: str, expected: int
) -> None:
    owner = await _person(db_session, "owner")
    workspace = await _workspace(db_session, owner)
    await _person(db_session, "findable")
    workspace_id: UUID = workspace.id
    headers: dict[str, str] = {}
    if caller in {"viewer", "editor", "outsider"}:
        person = await _person(db_session, caller)
        headers = _bearer(person)
        if caller != "outsider":
            await _join(db_session, workspace, person, WorkspaceRole(caller))
    elif caller == "owner-of-archived":
        now = datetime.now(UTC)
        workspace.archived_at, workspace.purge_after = now, now + timedelta(days=30)
        await db_session.flush()
        headers = _bearer(owner)
    elif caller == "owner-of-unknown":
        workspace_id, headers = uuid4(), _bearer(owner)

    search = await api_client.get(
        f"/api/workspaces/{workspace_id}/member-candidates",
        params={"q": "findable"},
        headers=headers,
    )
    # The same denial the existing owner-only routes give the same caller.
    add = await api_client.post(
        f"/api/workspaces/{workspace_id}/members",
        json={"user_id": str(uuid4()), "role": "viewer"},
        headers=headers,
    )

    assert search.status_code == expected
    assert add.status_code == expected


@pytest.mark.asyncio
async def test_owner_access_is_checked_before_the_query(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _person(db_session, "owner")
    workspace = await _workspace(db_session, owner)
    viewer = await _person(db_session, "viewer")
    await _join(db_session, workspace, viewer, WorkspaceRole.VIEWER)
    outsider = await _person(db_session, "outsider")
    url = f"/api/workspaces/{workspace.id}/member-candidates"

    # A non-owner learns nothing, not even that a query is malformed.
    assert (await api_client.get(url, params={"q": "a"})).status_code == 401
    assert (await api_client.get(url, headers=_bearer(outsider))).status_code == 404
    assert (
        await api_client.get(url, params={"q": "a"}, headers=_bearer(viewer))
    ).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({}, 422, id="missing"),
        pytest.param({"q": ""}, 422, id="empty"),
        pytest.param({"q": "a"}, 422, id="one-character"),
        # A lone wildcard is refused outright, so it can never match everyone.
        pytest.param({"q": "%"}, 422, id="lone-percent-sign"),
        pytest.param({"q": "_"}, 422, id="lone-underscore"),
        pytest.param({"q": "  a  "}, 422, id="one-character-once-trimmed"),
        # str.strip removes "\x1c" though Unicode's White_Space does not include it.
        pytest.param({"q": "\x1ca"}, 422, id="one-character-once-str-stripped"),
        pytest.param({"q": "x" * 256}, 422, id="too-long"),
        pytest.param({"q": "ab"}, 200, id="two-characters"),
        pytest.param({"q": " " + "x" * 255 + " "}, 200, id="255-characters-once-trimmed"),
    ],
)
async def test_the_query_must_be_2_to_255_characters_once_trimmed(
    api_client: AsyncClient, db_session: AsyncSession, params: dict[str, str], expected: int
) -> None:
    owner = await _person(db_session, "owner")
    workspace = await _workspace(db_session, owner)

    response = await api_client.get(
        f"/api/workspaces/{workspace.id}/member-candidates", params=params, headers=_bearer(owner)
    )

    assert response.status_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["ab" + chr(0), "a" + chr(0) + "b", "a" + chr(10) + "b"])
async def test_a_query_with_a_control_character_is_a_422_not_a_server_error(
    api_client: AsyncClient, db_session: AsyncSession, query: str
) -> None:
    # Regression: NUL reached PostgreSQL, which cannot compare it: a 500, and
    # a logged traceback quoting the search text.
    owner = await _person(db_session, "owner")
    workspace = await _workspace(db_session, owner)

    response = await api_client.get(
        f"/api/workspaces/{workspace.id}/member-candidates",
        params={"q": query},
        headers=_bearer(owner),
    )

    assert response.status_code == 422


# ── what adding a person found by name reveals ───────────────────────────────


@pytest.mark.asyncio
async def test_only_the_member_themselves_sees_their_email_in_full(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Regression: search by name, add, read `email` from the 201, remove,
    # repeat: any self-registered Owner collected every user's address.
    owner = await _person(db_session, "owner", email="owner@example.com")
    workspace = await _workspace(db_session, owner)
    grace = await _person(
        db_session, "grace", display_name="Grace Hopper", email="grace.hopper@navy.example"
    )
    search_url = f"/api/workspaces/{workspace.id}/member-candidates"
    members_url = f"/api/workspaces/{workspace.id}/members"

    [found] = (
        await api_client.get(search_url, params={"q": "hopper"}, headers=_bearer(owner))
    ).json()
    added = await api_client.post(
        members_url, json={"user_id": found["user_id"], "role": "viewer"}, headers=_bearer(owner)
    )
    promoted = await api_client.patch(
        f"{members_url}/{grace.id}", json={"role": "owner"}, headers=_bearer(owner)
    )
    as_owner = await api_client.get(members_url, headers=_bearer(owner))
    as_grace = await api_client.get(members_url, headers=_bearer(grace))
    # Now a co-owner, grace changes her own role and the owner's.
    own_change = await api_client.patch(
        f"{members_url}/{grace.id}", json={"role": "owner"}, headers=_bearer(grace)
    )
    others_change = await api_client.patch(
        f"{members_url}/{owner.id}", json={"role": "editor"}, headers=_bearer(grace)
    )

    assert added.status_code == 201
    assert added.json()["email"] == "g•••@navy.example"
    assert promoted.json()["email"] == "g•••@navy.example"
    assert {m["username"]: m["email"] for m in as_owner.json()} == {
        "owner": "owner@example.com",
        "grace": "g•••@navy.example",
    }
    assert {m["username"]: m["email"] for m in as_grace.json()} == {
        "owner": "o•••@example.com",
        "grace": "grace.hopper@navy.example",
    }
    assert own_change.json()["email"] == "grace.hopper@navy.example"
    assert others_change.json()["email"] == "o•••@example.com"


@pytest.mark.asyncio
async def test_a_google_accounts_username_and_hint_do_not_spell_out_its_email(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Regression: Google sends no preferred_username, so the username was the
    # email's local part, and "<username>@<the hint's domain>" was an exact
    # email search that confirmed the whole address.
    owner = await _person(db_session, "owner")
    workspace = await _workspace(db_session, owner)
    await OIDCAuthService(db_session, "google").complete_login(
        OIDCClaims(
            provider="google",
            subject="google-karl",
            email="karthedew@gmail.com",
            email_verified=True,
            name="Karl Schmidt",
        )
    )
    search_url = f"/api/workspaces/{workspace.id}/member-candidates"

    [found] = (
        await api_client.get(search_url, params={"q": "Karl"}, headers=_bearer(owner))
    ).json()
    domain = found["email_hint"].partition("@")[2]
    guess = await api_client.get(
        search_url, params={"q": f"{found['username']}@{domain}"}, headers=_bearer(owner)
    )

    assert found["username"] == "karl-schmidt"
    assert found["email_hint"] == "k•••@gmail.com"
    assert guess.json() == []
