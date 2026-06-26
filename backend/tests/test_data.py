from uuid import UUID

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deployment, DeploymentStatus, NotebookData
from test_notebooks import create_notebook, register_and_login


@pytest.mark.asyncio
async def test_post_data_while_deployment_sleeps_then_get_latest(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, owner_headers = await register_and_login(api_client, "data-owner")
    notebook = await create_notebook(api_client, owner_headers, "Data Buffer", "x = 1")
    notebook_id = UUID(str(notebook["id"]))
    db_session.add(Deployment(notebook_id=notebook_id, slug="data-buffer", status=DeploymentStatus.SLEEPING))
    await db_session.commit()

    first = await api_client.post(
        f"/api/notebooks/{notebook_id}/data",
        params={"source": "grafana"},
        json={"metric": "cpu", "value": 91.5},
    )
    second = await api_client.post(
        f"/api/notebooks/{notebook_id}/data",
        params={"source": "grafana-panel"},
        json={"metric": "memory", "value": 42},
    )
    latest = await api_client.get(f"/api/notebooks/{notebook_id}/data")

    assert first.status_code == 201
    first_body = first.json()
    assert set(first_body) == {"id"}
    assert UUID(first_body["id"])
    assert second.status_code == 201
    second_body = second.json()
    assert set(second_body) == {"id"}
    assert UUID(second_body["id"])
    assert latest.status_code == 200
    assert latest.json()["payload"] == {"metric": "memory", "value": 42}
    assert latest.json()["source"] == "grafana-panel"
    assert latest.json()["created_at"] is not None


@pytest.mark.asyncio
async def test_get_data_returns_404_when_notebook_has_no_data(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "empty-data-owner")
    notebook = await create_notebook(api_client, owner_headers, "Empty Data", "x = 1")

    response = await api_client.get(f"/api/notebooks/{notebook['id']}/data")

    assert response.status_code == 404
    assert response.json()["detail"] == "Notebook data not found"


@pytest.mark.asyncio
async def test_data_post_requires_existing_notebook(api_client: AsyncClient, db_session: AsyncSession) -> None:
    missing_id = UUID("00000000-0000-0000-0000-000000000001")

    response = await api_client.post(
        f"/api/notebooks/{missing_id}/data",
        params={"source": "grafana"},
        json={"metric": "cpu"},
    )
    stored = await db_session.scalar(select(NotebookData).where(NotebookData.notebook_id == missing_id))

    assert response.status_code == 404
    assert stored is None
