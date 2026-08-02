from uuid import UUID, uuid4

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_session_token
from app.models import Notebook, NotebookData
from test_notebooks import create_notebook, json_dict, register_and_login


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_source_token_for_other_notebook_returns_403(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-source-a")
    notebook_a = await create_notebook(api_client, owner_headers, owner_ws, "A", "x = 1")
    notebook_b = await create_notebook(api_client, owner_headers, owner_ws, "B", "y = 2")
    token_for_a = create_session_token(uuid4(), UUID(str(notebook_a["id"])))

    response = await api_client.get(
        f"/api/internal/notebooks/{notebook_b['id']}/source", headers=_bearer(token_for_a)
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_data_write_token_for_other_notebook_returns_403(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-write-a")
    notebook_a = await create_notebook(api_client, owner_headers, owner_ws, "A", "x = 1")
    notebook_b = await create_notebook(api_client, owner_headers, owner_ws, "B", "y = 2")
    token_for_a = create_session_token(uuid4(), UUID(str(notebook_a["id"])))

    response = await api_client.post(
        f"/api/internal/notebooks/{notebook_b['id']}/data",
        headers=_bearer(token_for_a),
        json={"metric": "cpu"},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_data_read_token_for_other_notebook_returns_403(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-read-a")
    notebook_a = await create_notebook(api_client, owner_headers, owner_ws, "A", "x = 1")
    notebook_b = await create_notebook(api_client, owner_headers, owner_ws, "B", "y = 2")
    token_for_a = create_session_token(uuid4(), UUID(str(notebook_a["id"])))

    response = await api_client.get(
        f"/api/internal/notebooks/{notebook_b['id']}/data", headers=_bearer(token_for_a)
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_absent_token_returns_401(api_client: AsyncClient, db_session: AsyncSession) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-absent")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "x = 1")

    response = await api_client.get(f"/api/internal/notebooks/{notebook['id']}/source")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_garbage_token_returns_401(api_client: AsyncClient, db_session: AsyncSession) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-garbage")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "x = 1")

    response = await api_client.get(
        f"/api/internal/notebooks/{notebook['id']}/source", headers=_bearer("not-a-jwt")
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_returns_401(api_client: AsyncClient, db_session: AsyncSession) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-expired")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "x = 1")
    expired_token = create_session_token(uuid4(), UUID(str(notebook["id"])), ttl_seconds=-1)

    response = await api_client.get(
        f"/api/internal/notebooks/{notebook['id']}/source", headers=_bearer(expired_token)
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_user_access_token_rejected_by_internal_endpoint(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-usertok")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "x = 1")
    user_token = owner_headers["Authorization"].removeprefix("Bearer ")

    response = await api_client.get(
        f"/api/internal/notebooks/{notebook['id']}/source", headers=_bearer(user_token)
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_session_token_rejected_by_user_endpoint(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-sesstok")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "x = 1")
    session_token = create_session_token(uuid4(), UUID(str(notebook["id"])))

    response = await api_client.post("/api/auth/logout", headers=_bearer(session_token))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_source_matches_storage_exactly(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-src-match")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "x = 42\ny = x + 1")
    token = create_session_token(uuid4(), UUID(str(notebook["id"])))

    response = await api_client.get(
        f"/api/internal/notebooks/{notebook['id']}/source", headers=_bearer(token)
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-python")
    assert response.text == "x = 42\ny = x + 1"


@pytest.mark.asyncio
async def test_source_empty_returns_200_with_empty_body(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-src-empty")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", source=None)
    token = create_session_token(uuid4(), UUID(str(notebook["id"])))

    response = await api_client.get(
        f"/api/internal/notebooks/{notebook['id']}/source", headers=_bearer(token)
    )

    assert response.status_code == 200
    assert response.text == ""


@pytest.mark.asyncio
async def test_data_write_persists_and_readback_ignores_visibility(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-data-owner")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Private", "x = 1")
    notebook_id = UUID(str(notebook["id"]))
    token = create_session_token(uuid4(), notebook_id)

    write_response = await api_client.post(
        f"/api/internal/notebooks/{notebook_id}/data",
        headers=_bearer(token),
        params={"source": "runtime"},
        json={"metric": "cpu", "value": 12.5},
    )

    assert write_response.status_code == 201
    written_id = UUID(str(json_dict(write_response)["id"]))
    row = await db_session.scalar(select(NotebookData).where(NotebookData.id == written_id))
    assert row is not None
    assert row.payload == {"metric": "cpu", "value": 12.5}
    assert row.source == "runtime"

    # No membership on this token at all -- the token alone authorizes the read.
    anon_token = create_session_token(uuid4(), notebook_id)
    read_response = await api_client.get(
        f"/api/internal/notebooks/{notebook_id}/data", headers=_bearer(anon_token)
    )

    assert read_response.status_code == 200
    body = read_response.json()
    assert body["payload"] == {"metric": "cpu", "value": 12.5}
    assert body["source"] == "runtime"


@pytest.mark.asyncio
async def test_data_write_to_nonexistent_notebook_returns_404(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    missing_id = uuid4()
    token = create_session_token(uuid4(), missing_id)

    response = await api_client.post(
        f"/api/internal/notebooks/{missing_id}/data",
        headers=_bearer(token),
        json={"metric": "cpu"},
    )
    stored = await db_session.scalar(select(Notebook).where(Notebook.id == missing_id))

    assert response.status_code == 404
    assert stored is None
