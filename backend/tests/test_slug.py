from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deployment, Workspace
from app.services.slug import DNS_LABEL_RE, to_dns_label, unique_slug
from test_notebooks import create_notebook, register_and_login


def test_to_dns_label_lowercases_and_replaces_invalid_characters() -> None:
    assert to_dns_label("My Cool Notebook!") == "my-cool-notebook"


def test_to_dns_label_strips_leading_trailing_hyphens() -> None:
    assert to_dns_label("--Weird__Title--") == "weird-title"


def test_to_dns_label_falls_back_when_nothing_survives_sanitisation() -> None:
    assert to_dns_label("!!!") == "resource"


def test_to_dns_label_truncates_to_63_characters() -> None:
    label = to_dns_label("x" * 100)

    assert len(label) == 63
    assert DNS_LABEL_RE.match(label)


def test_to_dns_label_output_always_matches_dns_label_regex() -> None:
    assert DNS_LABEL_RE.match(to_dns_label("Some Title (v2) [notes]"))


@pytest.mark.asyncio
async def test_unique_slug_returns_base_when_free(db_session: AsyncSession) -> None:
    slug = await unique_slug(db_session, "free-base", Deployment.slug)

    assert slug == "free-base"


@pytest.mark.asyncio
async def test_unique_slug_suffixes_on_collision(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "slug-owner")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Slug Base", "x = 1")
    taken = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "taken-slug"},
    )
    assert taken.status_code == 200

    first = await unique_slug(db_session, "taken-slug", Deployment.slug)
    assert first == "taken-slug-2"


@pytest.mark.asyncio
async def test_unique_slug_advances_past_multiple_collisions(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "slug-owner-2")
    for i, name in enumerate(["Chain Base", "Chain Base 2"]):
        notebook = await create_notebook(api_client, owner_headers, owner_ws, name, "x = 1")
        slug = "chain-slug" if i == 0 else "chain-slug-2"
        response = await api_client.post(
            f"/api/notebooks/{notebook['id']}/deploy",
            headers=owner_headers,
            json={"slug": slug},
        )
        assert response.status_code == 200

    next_slug = await unique_slug(db_session, "chain-slug", Deployment.slug)
    assert next_slug == "chain-slug-3"


@pytest.mark.asyncio
async def test_unique_slug_is_scoped_to_the_given_column(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A slug taken in one slugged table doesn't collide against another."""
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "slug-scoped")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Scoped Base", "x = 1")
    deployed = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "scoped-slug"},
    )
    assert deployed.status_code == 200

    slug = await unique_slug(db_session, "scoped-slug", Workspace.slug)
    assert slug == "scoped-slug"


@pytest.mark.asyncio
async def test_unique_slug_excludes_a_row_via_the_exclude_clause(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`exclude` lets a row keep its own slug instead of being bumped to `-2`."""
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "slug-exclude")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Exclude Base", "x = 1")
    deployed = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "exclude-slug"},
    )
    assert deployed.status_code == 200
    deployment_id = await db_session.scalar(
        select(Deployment.id).where(Deployment.slug == "exclude-slug")
    )
    assert deployment_id is not None

    kept = await unique_slug(
        db_session, "exclude-slug", Deployment.slug, exclude=Deployment.id != deployment_id
    )
    assert kept == "exclude-slug"

    bumped = await unique_slug(db_session, "exclude-slug", Deployment.slug)
    assert bumped == "exclude-slug-2"
