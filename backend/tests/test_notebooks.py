from typing import Protocol, cast
from uuid import UUID

import httpx
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notebook, NotebookVisibility
from app.services.gitlab_import import MAX_IMPORT_BYTES


class _EmbeddingRecorder(Protocol):
    """Structural type for the fake embedding service used in tests."""

    calls: list[str]


def json_dict(response: httpx.Response) -> dict[str, object]:
    """Parse a JSON object response as a typed mapping (httpx ``.json()`` is ``Any``)."""
    return cast("dict[str, object]", response.json())


def json_items(response: httpx.Response) -> list[dict[str, object]]:
    """Parse a paginated list response's ``items`` as typed mappings."""
    return cast("list[dict[str, object]]", json_dict(response)["items"])


async def register_and_login(
    client: AsyncClient, username: str
) -> tuple[dict[str, object], dict[str, str]]:
    login_value = "correct-horse"
    register_response = await client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": login_value},
    )
    assert register_response.status_code == 201

    login_response = await client.post(
        "/api/auth/login", json={"username": username, "password": login_value}
    )
    assert login_response.status_code == 200

    headers = {"Authorization": f"Bearer {json_dict(login_response)['access_token']}"}
    return json_dict(register_response), headers


async def create_notebook(
    client: AsyncClient,
    headers: dict[str, str],
    title: str,
    source: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, object]:
    response = await client.post(
        "/api/notebooks",
        headers=headers,
        json={
            "title": title,
            "description": description if description is not None else f"{title} description",
            "tags": tags if tags is not None else [title.lower()],
            "source": source,
        },
    )
    assert response.status_code == 201
    return json_dict(response)


async def publish_notebook(
    client: AsyncClient,
    headers: dict[str, str],
    notebook_id: str,
    visibility: str,
) -> dict[str, object]:
    response = await client.post(
        f"/api/notebooks/{notebook_id}/publish",
        headers=headers,
        json={"visibility": visibility},
    )
    assert response.status_code == 200
    return json_dict(response)


@pytest.mark.asyncio
async def test_crud_happy_path_persists_notebook_schema_behavior(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner, owner_headers = await register_and_login(api_client, "owner")

    anonymous_create = await api_client.post(
        "/api/notebooks", json={"title": "Draft", "source": "x = 1"}
    )
    created = await create_notebook(api_client, owner_headers, "Draft", "x = 1")
    notebook_id = str(created["id"])
    persisted = await db_session.get(Notebook, UUID(notebook_id))

    assert anonymous_create.status_code == 401
    assert persisted is not None
    assert persisted.user_id == UUID(str(owner["id"]))
    assert persisted.title == "Draft"
    assert persisted.tags == ["draft"]
    assert persisted.visibility == NotebookVisibility.DRAFT
    assert persisted.source == "x = 1"
    assert created["user_id"] == owner["id"]
    assert created["visibility"] == "draft"
    assert created["source"] == "x = 1"
    assert "embedding" not in created
    assert "search_vector" not in created

    fetched = await api_client.get(f"/api/notebooks/{notebook_id}", headers=owner_headers)
    listed = await api_client.get("/api/notebooks", headers=owner_headers)
    updated = await api_client.put(
        f"/api/notebooks/{notebook_id}",
        headers=owner_headers,
        json={"title": "Updated", "description": "New", "tags": ["a"], "source": "x = 2"},
    )
    deleted = await api_client.delete(f"/api/notebooks/{notebook_id}", headers=owner_headers)
    after_delete = await api_client.get(f"/api/notebooks/{notebook_id}", headers=owner_headers)

    assert fetched.status_code == 200
    assert fetched.json()["source"] == "x = 1"
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == notebook_id
    assert updated.status_code == 200
    assert updated.json()["title"] == "Updated"
    assert updated.json()["description"] == "New"
    assert updated.json()["tags"] == ["a"]
    assert updated.json()["source"] == "x = 2"
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert after_delete.status_code == 404


@pytest.mark.asyncio
async def test_list_notebooks_shows_public_and_callers_own_notebooks(
    api_client: AsyncClient,
) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    _, other_headers = await register_and_login(api_client, "other")
    owner_draft = await create_notebook(
        api_client, owner_headers, "Owner Draft", "print('owner draft')"
    )
    owner_public = await create_notebook(
        api_client, owner_headers, "Owner Public", "print('owner public')"
    )
    other_public = await create_notebook(
        api_client, other_headers, "Other Public", "print('other public')"
    )
    other_unlisted = await create_notebook(
        api_client, other_headers, "Other Unlisted", "print('other unlisted')"
    )
    await publish_notebook(api_client, owner_headers, str(owner_public["id"]), "public")
    await publish_notebook(api_client, other_headers, str(other_public["id"]), "public")
    await publish_notebook(api_client, other_headers, str(other_unlisted["id"]), "unlisted")

    anonymous = await api_client.get("/api/notebooks")
    owner_response = await api_client.get("/api/notebooks", headers=owner_headers)

    anonymous_items = json_items(anonymous)
    owner_items = json_items(owner_response)
    owner_items_by_id = {item["id"]: item for item in owner_items}
    assert anonymous.status_code == 200
    assert {item["id"] for item in anonymous_items} == {
        str(owner_public["id"]),
        str(other_public["id"]),
    }
    assert all("source" not in item for item in anonymous_items)
    assert owner_response.status_code == 200
    assert owner_response.json()["total"] == 3
    assert owner_response.json()["page"] == 1
    assert owner_response.json()["page_size"] == 20
    assert set(owner_items_by_id) == {
        str(owner_draft["id"]),
        str(owner_public["id"]),
        str(other_public["id"]),
    }
    assert owner_items_by_id[str(owner_draft["id"])]["source"] == "print('owner draft')"
    assert owner_items_by_id[str(owner_public["id"])]["source"] == "print('owner public')"
    assert "source" not in owner_items_by_id[str(other_public["id"])]


@pytest.mark.asyncio
async def test_list_notebooks_full_text_search_matches_title_description_and_tags(
    api_client: AsyncClient,
) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    title_match = await create_notebook(
        api_client,
        owner_headers,
        "Cryogenic Pump Model",
        description="Thermal simulation",
        tags=["hardware"],
    )
    description_match = await create_notebook(
        api_client,
        owner_headers,
        "Turbine Analysis",
        description="Cryogenic flow measurements",
        tags=["analysis"],
    )
    tag_match = await create_notebook(
        api_client,
        owner_headers,
        "Telemetry Dashboard",
        description="Sensor trends",
        tags=["cryogenic"],
    )
    miss = await create_notebook(
        api_client,
        owner_headers,
        "Combustion Report",
        description="Hot fire campaign",
        tags=["propulsion"],
    )
    for notebook in [title_match, description_match, tag_match, miss]:
        await publish_notebook(api_client, owner_headers, str(notebook["id"]), "public")

    response = await api_client.get(
        "/api/notebooks", params={"q": "cryogenic", "page": 1, "page_size": 2}
    )

    assert response.status_code == 200
    body = json_dict(response)
    result_ids = {item["id"] for item in json_items(response)}
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(json_items(response)) == 2
    assert result_ids <= {
        str(title_match["id"]),
        str(description_match["id"]),
        str(tag_match["id"]),
    }
    assert str(miss["id"]) not in result_ids


@pytest.mark.asyncio
async def test_list_notebooks_orders_full_text_results_by_rank(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    strongest = await create_notebook(
        api_client,
        owner_headers,
        "Plasma Plasma Plasma",
        description="Modeling notes",
        tags=["physics"],
    )
    weaker = await create_notebook(
        api_client,
        owner_headers,
        "Plasma",
        description="Modeling notes",
        tags=["physics"],
    )
    for notebook in [strongest, weaker]:
        await publish_notebook(api_client, owner_headers, str(notebook["id"]), "public")

    response = await api_client.get("/api/notebooks", params={"q": "plasma"})

    assert response.status_code == 200
    ids = [item["id"] for item in json_items(response)]
    assert ids[:2] == [str(strongest["id"]), str(weaker["id"])]


@pytest.mark.asyncio
async def test_list_notebooks_filters_by_tags(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    matching = await create_notebook(
        api_client,
        owner_headers,
        "Solar Forecast",
        description="Irradiance prediction",
        tags=["solar", "forecast"],
    )
    partial = await create_notebook(
        api_client,
        owner_headers,
        "Solar Archive",
        description="Historical panels",
        tags=["solar"],
    )
    unrelated = await create_notebook(
        api_client,
        owner_headers,
        "Wind Forecast",
        description="Turbine prediction",
        tags=["wind", "forecast"],
    )
    for notebook in [matching, partial, unrelated]:
        await publish_notebook(api_client, owner_headers, str(notebook["id"]), "public")

    response = await api_client.get(
        "/api/notebooks", params=[("tags", "solar"), ("tags", "forecast")]
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [item["id"] for item in json_items(response)] == [str(matching["id"])]


@pytest.mark.asyncio
async def test_list_notebooks_semantic_search_ranks_public_on_topic_notebook_first(
    api_client: AsyncClient,
) -> None:
    _, owner_headers = await register_and_login(api_client, "semantic-owner")
    rocket = await create_notebook(
        api_client,
        owner_headers,
        "Rocket Engine Analysis",
        description="Propulsion chamber thermal model",
        tags=["propulsion"],
    )
    climate = await create_notebook(
        api_client,
        owner_headers,
        "Climate Forecast",
        description="Atmosphere and weather simulation",
        tags=["climate"],
    )
    music = await create_notebook(
        api_client,
        owner_headers,
        "Music Classifier",
        description="Audio melody recognition",
        tags=["audio"],
    )
    unlisted_rocket = await create_notebook(
        api_client,
        owner_headers,
        "Private Rocket Notes",
        description="Propulsion notebook not listed publicly",
        tags=["rocket"],
    )
    draft_with_embedding = await create_notebook(
        api_client,
        owner_headers,
        "Draft Rocket Experiment",
        description="Engine injector work in progress",
        tags=["rocket"],
    )
    for notebook in [rocket, climate, music]:
        await publish_notebook(api_client, owner_headers, str(notebook["id"]), "public")
    await publish_notebook(api_client, owner_headers, str(unlisted_rocket["id"]), "unlisted")
    await publish_notebook(api_client, owner_headers, str(draft_with_embedding["id"]), "public")
    await publish_notebook(api_client, owner_headers, str(draft_with_embedding["id"]), "draft")

    response = await api_client.get(
        "/api/notebooks",
        headers=owner_headers,
        params={"semantic": "rocket propulsion engine"},
    )

    assert response.status_code == 200
    body = json_dict(response)
    ids = [item["id"] for item in json_items(response)]
    assert body["total"] == 3
    assert ids[0] == str(rocket["id"])
    assert set(ids) == {str(rocket["id"]), str(climate["id"]), str(music["id"])}
    assert str(unlisted_rocket["id"]) not in ids
    assert str(draft_with_embedding["id"]) not in ids


@pytest.mark.asyncio
async def test_semantic_search_preserves_text_tags_and_pagination_filters(
    api_client: AsyncClient,
) -> None:
    _, owner_headers = await register_and_login(api_client, "semantic-filters")
    matching = await create_notebook(
        api_client,
        owner_headers,
        "Rocket Market Study",
        description="Propulsion market forecast",
        tags=["finance", "propulsion"],
    )
    wrong_text = await create_notebook(
        api_client,
        owner_headers,
        "Rocket Engine Analysis",
        description="Thermal propulsion notes",
        tags=["propulsion"],
    )
    wrong_tag = await create_notebook(
        api_client,
        owner_headers,
        "Trading Dashboard",
        description="Market signals and finance models",
        tags=["finance"],
    )
    for notebook in [matching, wrong_text, wrong_tag]:
        await publish_notebook(api_client, owner_headers, str(notebook["id"]), "public")

    response = await api_client.get(
        "/api/notebooks",
        params={
            "semantic": "market trading",
            "q": "market",
            "tags": "propulsion",
            "page": 1,
            "page_size": 1,
        },
    )

    assert response.status_code == 200
    body = json_dict(response)
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 1
    assert [item["id"] for item in json_items(response)] == [str(matching["id"])]


@pytest.mark.asyncio
async def test_list_notebooks_combines_visibility_search_and_tag_filters(
    api_client: AsyncClient,
) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    _, other_headers = await register_and_login(api_client, "other")
    owner_draft = await create_notebook(
        api_client,
        owner_headers,
        "Spectrometer Draft",
        description="Argon calibration",
        tags=["spectrometer"],
    )
    owner_public = await create_notebook(
        api_client,
        owner_headers,
        "Spectrometer Public",
        description="Argon calibration",
        tags=["spectrometer"],
    )
    other_public = await create_notebook(
        api_client,
        other_headers,
        "Spectrometer Shared",
        description="Argon calibration",
        tags=["spectrometer"],
    )
    other_draft = await create_notebook(
        api_client,
        other_headers,
        "Spectrometer Private",
        description="Argon calibration",
        tags=["spectrometer"],
    )
    await publish_notebook(api_client, owner_headers, str(owner_public["id"]), "public")
    await publish_notebook(api_client, other_headers, str(other_public["id"]), "public")

    params = {"q": "argon", "tags": "spectrometer"}
    anonymous = await api_client.get("/api/notebooks", params=params)
    owner_response = await api_client.get("/api/notebooks", headers=owner_headers, params=params)

    assert anonymous.status_code == 200
    assert {item["id"] for item in json_items(anonymous)} == {
        str(owner_public["id"]),
        str(other_public["id"]),
    }
    assert owner_response.status_code == 200
    assert {item["id"] for item in json_items(owner_response)} == {
        str(owner_draft["id"]),
        str(owner_public["id"]),
        str(other_public["id"]),
    }
    assert str(other_draft["id"]) not in {item["id"] for item in json_items(owner_response)}


@pytest.mark.asyncio
async def test_get_notebook_visibility_matrix_and_source_exposure(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    _, other_headers = await register_and_login(api_client, "other")
    draft = await create_notebook(api_client, owner_headers, "Draft", "print('draft')")
    unlisted = await create_notebook(api_client, owner_headers, "Unlisted", "print('unlisted')")
    public = await create_notebook(api_client, owner_headers, "Public", "print('public')")
    await publish_notebook(api_client, owner_headers, str(unlisted["id"]), "unlisted")
    await publish_notebook(api_client, owner_headers, str(public["id"]), "public")

    cases = [
        (draft, None, 404, False),
        (draft, owner_headers, 200, True),
        (draft, other_headers, 404, False),
        (unlisted, None, 200, False),
        (unlisted, owner_headers, 200, True),
        (unlisted, other_headers, 200, False),
        (public, None, 200, False),
        (public, owner_headers, 200, True),
        (public, other_headers, 200, False),
    ]

    for notebook, headers, expected_status, includes_source in cases:
        response = await api_client.get(f"/api/notebooks/{notebook['id']}", headers=headers)
        assert response.status_code == expected_status
        if expected_status == 200 and includes_source:
            assert response.json()["source"] == notebook["source"]
        if expected_status == 200 and not includes_source:
            assert "source" not in response.json()


@pytest.mark.asyncio
async def test_publish_transitions_between_all_visibility_states(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    notebook = await create_notebook(api_client, owner_headers, "Notebook", "x = 1")
    notebook_id = str(notebook["id"])

    unlisted = await publish_notebook(api_client, owner_headers, notebook_id, "unlisted")
    public = await publish_notebook(api_client, owner_headers, notebook_id, "public")
    draft = await publish_notebook(api_client, owner_headers, notebook_id, "draft")
    anonymous_after_draft = await api_client.get(f"/api/notebooks/{notebook_id}")

    assert unlisted["visibility"] == "unlisted"
    assert unlisted["source"] == "x = 1"
    assert public["visibility"] == "public"
    assert draft["visibility"] == "draft"
    assert anonymous_after_draft.status_code == 404


@pytest.mark.asyncio
async def test_fork_notebook_copies_public_and_unlisted_sources_with_lineage(
    api_client: AsyncClient,
) -> None:
    owner, owner_headers = await register_and_login(api_client, "fork-source-owner")
    forker, forker_headers = await register_and_login(api_client, "forker")
    public = await create_notebook(
        api_client,
        owner_headers,
        "Public Source",
        "print('public')",
        description="Visible source",
        tags=["public", "fork"],
    )
    unlisted = await create_notebook(
        api_client,
        owner_headers,
        "Unlisted Source",
        "print('unlisted')",
        description="Shared by link",
        tags=["unlisted", "fork"],
    )
    await publish_notebook(api_client, owner_headers, str(public["id"]), "public")
    await publish_notebook(api_client, owner_headers, str(unlisted["id"]), "unlisted")

    anonymous_fork = await api_client.post(f"/api/notebooks/{public['id']}/fork")
    public_fork = await api_client.post(
        f"/api/notebooks/{public['id']}/fork", headers=forker_headers
    )
    unlisted_fork = await api_client.post(
        f"/api/notebooks/{unlisted['id']}/fork", headers=forker_headers
    )
    parent_after_fork = await api_client.get(f"/api/notebooks/{public['id']}")

    assert anonymous_fork.status_code == 401
    assert public_fork.status_code == 201
    public_body = json_dict(public_fork)
    assert public_body["user_id"] == forker["id"]
    assert public_body["parent_id"] == public["id"]
    assert public_body["parent_title"] == "Public Source"
    assert public_body["parent_owner_id"] == owner["id"]
    assert public_body["parent_owner_username"] == "fork-source-owner"
    assert public_body["title"] == "Public Source"
    assert public_body["description"] == "Visible source"
    assert public_body["tags"] == ["public", "fork"]
    assert public_body["visibility"] == "draft"
    assert public_body["fork_count"] == 0
    assert public_body["source"] == "print('public')"
    assert parent_after_fork.status_code == 200
    assert parent_after_fork.json()["fork_count"] == 1

    assert unlisted_fork.status_code == 201
    unlisted_body = json_dict(unlisted_fork)
    assert unlisted_body["parent_id"] == unlisted["id"]
    assert unlisted_body["parent_title"] == "Unlisted Source"
    assert unlisted_body["source"] == "print('unlisted')"


@pytest.mark.asyncio
async def test_forked_draft_is_private_to_forker(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "private-fork-owner")
    _, forker_headers = await register_and_login(api_client, "private-forker")
    _, other_headers = await register_and_login(api_client, "private-fork-other")
    source = await create_notebook(api_client, owner_headers, "Forkable", "x = 1")
    await publish_notebook(api_client, owner_headers, str(source["id"]), "public")
    fork_response = await api_client.post(
        f"/api/notebooks/{source['id']}/fork", headers=forker_headers
    )
    fork_id = str(json_dict(fork_response)["id"])

    anonymous = await api_client.get(f"/api/notebooks/{fork_id}")
    parent_owner = await api_client.get(f"/api/notebooks/{fork_id}", headers=owner_headers)
    other = await api_client.get(f"/api/notebooks/{fork_id}", headers=other_headers)
    forker = await api_client.get(f"/api/notebooks/{fork_id}", headers=forker_headers)

    assert fork_response.status_code == 201
    assert anonymous.status_code == 404
    assert parent_owner.status_code == 404
    assert other.status_code == 404
    assert forker.status_code == 200
    assert forker.json()["source"] == "x = 1"


@pytest.mark.asyncio
async def test_published_fork_hides_parent_metadata_when_parent_is_not_visible(
    api_client: AsyncClient,
) -> None:
    _, owner_headers = await register_and_login(api_client, "hidden-parent-owner")
    _, other_headers = await register_and_login(api_client, "hidden-parent-other")
    parent = await create_notebook(api_client, owner_headers, "Hidden Parent", "secret = True")
    fork_response = await api_client.post(
        f"/api/notebooks/{parent['id']}/fork", headers=owner_headers
    )
    fork_id = str(json_dict(fork_response)["id"])
    await publish_notebook(api_client, owner_headers, fork_id, "public")

    owner = await api_client.get(f"/api/notebooks/{fork_id}", headers=owner_headers)
    other = await api_client.get(f"/api/notebooks/{fork_id}", headers=other_headers)
    anonymous = await api_client.get(f"/api/notebooks/{fork_id}")

    assert fork_response.status_code == 201
    assert owner.status_code == 200
    assert owner.json()["parent_title"] == "Hidden Parent"
    assert owner.json()["parent_owner_username"] == "hidden-parent-owner"
    assert other.status_code == 200
    assert anonymous.status_code == 200
    for body in [json_dict(other), json_dict(anonymous)]:
        assert body["parent_id"] == parent["id"]
        assert "parent_title" not in body
        assert "parent_owner_id" not in body
        assert "parent_owner_username" not in body


@pytest.mark.asyncio
async def test_fork_someone_elses_draft_returns_404(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "draft-source-owner")
    _, other_headers = await register_and_login(api_client, "draft-source-other")
    draft = await create_notebook(api_client, owner_headers, "Hidden Draft", "secret = True")

    response = await api_client.post(f"/api/notebooks/{draft['id']}/fork", headers=other_headers)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_publish_embeds_when_draft_becomes_visible_and_not_when_returning_to_draft(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_embedding_service: _EmbeddingRecorder,
) -> None:
    _, owner_headers = await register_and_login(api_client, "embedding-owner")
    public_notebook = await create_notebook(
        api_client,
        owner_headers,
        "Rocket Notebook",
        description="Engine analysis",
        tags=["propulsion"],
    )
    unlisted_notebook = await create_notebook(
        api_client,
        owner_headers,
        "Climate Notebook",
        description="Weather analysis",
        tags=["atmosphere"],
    )

    await publish_notebook(api_client, owner_headers, str(public_notebook["id"]), "public")
    await publish_notebook(api_client, owner_headers, str(unlisted_notebook["id"]), "unlisted")
    await publish_notebook(api_client, owner_headers, str(public_notebook["id"]), "draft")

    rows = await db_session.scalars(
        select(Notebook).where(
            Notebook.id.in_([UUID(str(public_notebook["id"])), UUID(str(unlisted_notebook["id"]))])
        )
    )
    notebooks = {str(notebook.id): notebook for notebook in rows}
    assert len(fake_embedding_service.calls) == 2
    assert fake_embedding_service.calls == [
        "Rocket Notebook Engine analysis propulsion",
        "Climate Notebook Weather analysis atmosphere",
    ]
    assert notebooks[str(public_notebook["id"])].embedding is not None
    assert notebooks[str(unlisted_notebook["id"])].embedding is not None


@pytest.mark.asyncio
async def test_update_and_delete_enforce_ownership(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    _, other_headers = await register_and_login(api_client, "other")
    notebook = await create_notebook(api_client, owner_headers, "Original", "x = 1")
    notebook_id = str(notebook["id"])
    await publish_notebook(api_client, owner_headers, notebook_id, "public")

    anonymous_update = await api_client.put(f"/api/notebooks/{notebook_id}", json={"title": "Nope"})
    other_update = await api_client.put(
        f"/api/notebooks/{notebook_id}", headers=other_headers, json={"title": "Nope"}
    )
    owner_update = await api_client.put(
        f"/api/notebooks/{notebook_id}", headers=owner_headers, json={"title": "Updated"}
    )
    other_delete_public = await api_client.delete(
        f"/api/notebooks/{notebook_id}", headers=other_headers
    )
    await publish_notebook(api_client, owner_headers, notebook_id, "draft")
    other_delete_hidden_draft = await api_client.delete(
        f"/api/notebooks/{notebook_id}", headers=other_headers
    )
    owner_delete = await api_client.delete(f"/api/notebooks/{notebook_id}", headers=owner_headers)

    assert anonymous_update.status_code == 401
    assert other_update.status_code == 403
    assert owner_update.status_code == 200
    assert owner_update.json()["title"] == "Updated"
    assert other_delete_public.status_code == 403
    assert other_delete_hidden_draft.status_code == 404
    assert owner_delete.status_code == 204


@pytest.mark.asyncio
async def test_update_rejects_null_title_and_tags_without_changing_database(
    api_client: AsyncClient,
) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    notebook = await create_notebook(api_client, owner_headers, "Original", "x = 1")
    notebook_id = str(notebook["id"])

    null_title = await api_client.put(
        f"/api/notebooks/{notebook_id}", headers=owner_headers, json={"title": None}
    )
    null_tags = await api_client.put(
        f"/api/notebooks/{notebook_id}", headers=owner_headers, json={"tags": None}
    )
    persisted = await api_client.get(f"/api/notebooks/{notebook_id}", headers=owner_headers)

    assert null_title.status_code == 422
    assert null_tags.status_code == 422
    assert persisted.status_code == 200
    assert persisted.json()["title"] == "Original"
    assert persisted.json()["tags"] == ["original"]


def mock_gitlab_response(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> list[dict[str, str] | None]:
    calls: list[dict[str, str] | None] = []

    class MockAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

        async def __aenter__(self) -> "MockAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
            calls.append(headers)
            return response

    monkeypatch.setattr("app.services.gitlab_import.httpx.AsyncClient", MockAsyncClient)
    return calls


def raw_file_response(url: str, content: str | bytes, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, content=content, request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_import_notebook_fetches_public_raw_file_and_persists_draft(
    api_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner_headers = await register_and_login(api_client, "importer")
    source = "import marimo as mo\n\napp = mo.App()\n"
    mock_gitlab_response(
        monkeypatch,
        raw_file_response("https://gitlab.example.com/group/project/-/raw/main/pump.py", source),
    )

    response = await api_client.post(
        "/api/notebooks/import",
        headers=owner_headers,
        json={"url": "https://gitlab.example.com/group/project/-/raw/main/pump.py"},
    )

    assert response.status_code == 201
    body = json_dict(response)
    persisted = await db_session.get(Notebook, UUID(str(body["id"])))
    assert body["title"] == "pump"
    assert body["visibility"] == "draft"
    assert body["source"] == source
    assert persisted is not None
    assert persisted.source == source
    assert persisted.visibility == NotebookVisibility.DRAFT


@pytest.mark.asyncio
async def test_import_notebook_sends_pat_header_without_storing_or_exposing_it(
    api_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner_headers = await register_and_login(api_client, "private-importer")
    private_pat = "glpat-secret-token"
    source = "from marimo import App\n\napp = App()\n"
    calls = mock_gitlab_response(
        monkeypatch,
        raw_file_response("https://gitlab.example.com/group/project/-/raw/main/private.py", source),
    )

    response = await api_client.post(
        "/api/notebooks/import",
        headers=owner_headers,
        json={
            "url": "https://gitlab.example.com/group/project/-/raw/main/private.py",
            "pat": private_pat,
        },
    )

    assert response.status_code == 201
    body = json_dict(response)
    persisted = await db_session.get(Notebook, UUID(str(body["id"])))
    assert calls == [{"PRIVATE-TOKEN": private_pat}]
    assert private_pat not in response.text
    assert persisted is not None
    assert persisted.source == source
    assert private_pat not in persisted.source


@pytest.mark.asyncio
async def test_import_notebook_rejects_non_python_source(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner_headers = await register_and_login(api_client, "bad-python-importer")
    mock_gitlab_response(
        monkeypatch,
        raw_file_response(
            "https://gitlab.example.com/group/project/-/raw/main/bad.py", "import marimo as mo\nif"
        ),
    )

    response = await api_client.post(
        "/api/notebooks/import",
        headers=owner_headers,
        json={"url": "https://gitlab.example.com/group/project/-/raw/main/bad.py"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Imported file is not valid Python"


@pytest.mark.asyncio
async def test_import_notebook_rejects_python_without_marimo_import(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner_headers = await register_and_login(api_client, "plain-python-importer")
    mock_gitlab_response(
        monkeypatch,
        raw_file_response(
            "https://gitlab.example.com/group/project/-/raw/main/script.py", "print('hello')\n"
        ),
    )

    response = await api_client.post(
        "/api/notebooks/import",
        headers=owner_headers,
        json={"url": "https://gitlab.example.com/group/project/-/raw/main/script.py"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Imported file is not a marimo notebook"


@pytest.mark.asyncio
async def test_import_notebook_surfaces_upstream_404(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner_headers = await register_and_login(api_client, "missing-importer")
    mock_gitlab_response(
        monkeypatch,
        raw_file_response(
            "https://gitlab.example.com/group/project/-/raw/main/missing.py",
            "missing",
            status_code=404,
        ),
    )

    response = await api_client.post(
        "/api/notebooks/import",
        headers=owner_headers,
        json={"url": "https://gitlab.example.com/group/project/-/raw/main/missing.py"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Upstream returned 404 while fetching notebook"


@pytest.mark.asyncio
async def test_import_notebook_rejects_files_over_size_limit(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner_headers = await register_and_login(api_client, "large-importer")
    mock_gitlab_response(
        monkeypatch,
        raw_file_response(
            "https://gitlab.example.com/group/project/-/raw/main/large.py",
            b"x" * (MAX_IMPORT_BYTES + 1),
        ),
    )

    response = await api_client.post(
        "/api/notebooks/import",
        headers=owner_headers,
        json={"url": "https://gitlab.example.com/group/project/-/raw/main/large.py"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Imported notebook exceeds the 1 MB size limit"
