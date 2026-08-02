from uuid import UUID

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from test_notebooks import create_notebook, register_and_login


@pytest.mark.asyncio
async def test_get_data_returns_404_when_notebook_has_no_data(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "empty-data-owner"
    )
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Empty Data", "x = 1")

    response = await api_client.get(f"/api/notebooks/{notebook['id']}/data", headers=owner_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Notebook data not found"


@pytest.mark.asyncio
async def test_get_data_hides_private_notebook_from_non_members(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "data-priv-owner")
    _, other_headers, _ = await register_and_login(api_client, db_session, "data-priv-other")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Private Data", "x = 1")

    other_get = await api_client.get(f"/api/notebooks/{notebook['id']}/data", headers=other_headers)

    assert other_get.status_code == 404


@pytest.mark.asyncio
async def test_get_data_returns_404_for_missing_notebook(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    missing_id = UUID("00000000-0000-0000-0000-000000000001")

    response = await api_client.get(f"/api/notebooks/{missing_id}/data")

    assert response.status_code == 404
