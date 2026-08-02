from uuid import UUID, uuid4

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.internal_main import app as internal_app
from app.models import Deployment, DeploymentDesiredState, Notebook, NotebookData
from app.services.runtime_credentials import get_runtime_credential_verifier
from test_notebooks import create_notebook, json_dict, register_and_login
from test_runtime_credentials import FakeRuntimeCluster


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _use_cluster(cluster: FakeRuntimeCluster) -> None:
    internal_app.dependency_overrides[get_runtime_credential_verifier] = cluster.verifier


async def _create_deployment(
    db: AsyncSession,
    notebook_id: UUID,
    *,
    revision: int = 1,
    desired_state: DeploymentDesiredState = DeploymentDesiredState.ACTIVE,
    source_snapshot: str = "x = 1",
    slug: str = "deployed-notebook",
) -> Deployment:
    deployment = Deployment(
        notebook_id=notebook_id,
        slug=slug,
        revision=revision,
        desired_state=desired_state,
        source_snapshot=source_snapshot,
        source_sha256="deadbeef",
        runtime_image="registry.example/marimo-runtime@sha256:" + "0" * 64,
    )
    db.add(deployment)
    await db.commit()
    await db.refresh(deployment)
    return deployment


@pytest.mark.asyncio
async def test_absent_credential_returns_401(internal_api_client: AsyncClient) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, _ = cluster.mint()

    response = await internal_api_client.get(f"/api/internal/runtimes/{runtime_id}/source")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_garbage_credential_returns_401(internal_api_client: AsyncClient) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, _ = cluster.mint()

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer("not-a-credential")
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_user_access_token_rejected_by_internal_endpoint(
    internal_api_client: AsyncClient,
) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, _ = cluster.mint()
    user_token = create_access_token(uuid4())

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(user_token)
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_runtime_credential_rejected_by_public_auth_endpoint(
    api_client: AsyncClient,
) -> None:
    cluster = FakeRuntimeCluster()
    _, credential = cluster.mint()

    response = await api_client.post("/api/auth/logout", headers=_bearer(credential))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_credential_for_a_different_runtime_id_is_rejected(
    internal_api_client: AsyncClient,
) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    _, credential = cluster.mint()
    other_runtime_id, _ = cluster.mint()

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{other_runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_deleted_cr_revokes_even_though_secret_still_exists(
    internal_api_client: AsyncClient,
) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, credential = cluster.mint()
    cluster.delete_cr(runtime_id)  # Secret is untouched -- GC has not caught up yet.

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_deleted_secret_revokes(internal_api_client: AsyncClient) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, credential = cluster.mint()
    cluster.delete_secret(runtime_id)

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_mutated_secret_revokes(internal_api_client: AsyncClient) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, credential = cluster.mint()
    cluster.corrupt_secret(runtime_id, immutable=False)

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_recreated_cr_with_new_uid_cannot_use_old_token(
    internal_api_client: AsyncClient,
) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, old_credential = cluster.mint()
    cluster.recreate_cr_with_new_uid(runtime_id)

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(old_credential)
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_credential_never_appears_in_the_401_response(
    internal_api_client: AsyncClient,
) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, credential = cluster.mint()
    cluster.delete_secret(runtime_id)

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 401
    assert credential not in response.text


@pytest.mark.asyncio
async def test_edit_source_matches_storage_exactly(
    api_client: AsyncClient, internal_api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-src-match")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "x = 42\ny = x + 1")
    notebook_id = UUID(str(notebook["id"]))
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, credential = cluster.mint(notebook_id=notebook_id, mode="edit")

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-python")
    assert response.text == "x = 42\ny = x + 1"


@pytest.mark.asyncio
async def test_source_empty_returns_200_with_empty_body(
    api_client: AsyncClient, internal_api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-src-empty")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", source=None)
    notebook_id = UUID(str(notebook["id"]))
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, credential = cluster.mint(notebook_id=notebook_id, mode="run")

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 200
    assert response.text == ""


@pytest.mark.asyncio
async def test_deploy_source_returns_snapshot_when_revision_matches(
    api_client: AsyncClient, internal_api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-deploy-ok")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "current source")
    notebook_id = UUID(str(notebook["id"]))
    deployment = await _create_deployment(
        db_session, notebook_id, revision=2, source_snapshot="deployed snapshot"
    )
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, credential = cluster.mint(
        notebook_id=notebook_id,
        mode="deploy",
        deployment_revision=deployment.revision,
        runtime_id=deployment.id,
    )

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 200
    assert response.text == "deployed snapshot"


@pytest.mark.asyncio
async def test_deploy_source_fails_closed_on_stale_revision(
    api_client: AsyncClient, internal_api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "int-deploy-stale"
    )
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "x = 1")
    notebook_id = UUID(str(notebook["id"]))
    deployment = await _create_deployment(db_session, notebook_id, revision=3)
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    # The Runtime's own CR still reports the previous revision -- a redeploy
    # bumped the row underneath it, and the stale Runtime must not serve.
    runtime_id, credential = cluster.mint(
        notebook_id=notebook_id, mode="deploy", deployment_revision=2, runtime_id=deployment.id
    )

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_deploy_source_fails_closed_when_stopped(
    api_client: AsyncClient, internal_api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-deploy-stop")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "N", "x = 1")
    notebook_id = UUID(str(notebook["id"]))
    deployment = await _create_deployment(
        db_session, notebook_id, revision=1, desired_state=DeploymentDesiredState.STOPPED
    )
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    runtime_id, credential = cluster.mint(
        notebook_id=notebook_id, mode="deploy", deployment_revision=1, runtime_id=deployment.id
    )

    response = await internal_api_client.get(
        f"/api/internal/runtimes/{runtime_id}/source", headers=_bearer(credential)
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_data_token_for_other_notebook_returns_403(
    internal_api_client: AsyncClient,
) -> None:
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    notebook_a, notebook_b = uuid4(), uuid4()
    _, credential_for_a = cluster.mint(notebook_id=notebook_a)

    response = await internal_api_client.get(
        f"/api/internal/notebooks/{notebook_b}/data", headers=_bearer(credential_for_a)
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_data_write_persists_and_readback_ignores_visibility(
    api_client: AsyncClient, internal_api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "int-data-owner")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Private", "x = 1")
    notebook_id = UUID(str(notebook["id"]))
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    _, credential = cluster.mint(notebook_id=notebook_id)

    write_response = await internal_api_client.post(
        f"/api/internal/notebooks/{notebook_id}/data",
        headers=_bearer(credential),
        params={"source": "runtime"},
        json={"metric": "cpu", "value": 12.5},
    )

    assert write_response.status_code == 201
    written_id = UUID(str(json_dict(write_response)["id"]))
    row = await db_session.scalar(select(NotebookData).where(NotebookData.id == written_id))
    assert row is not None
    assert row.payload == {"metric": "cpu", "value": 12.5}
    assert row.source == "runtime"

    # A second, unrelated Runtime bound to the same notebook can read it back
    # -- the credential's notebook binding is the entire authorization.
    _, other_credential = cluster.mint(notebook_id=notebook_id)
    read_response = await internal_api_client.get(
        f"/api/internal/notebooks/{notebook_id}/data", headers=_bearer(other_credential)
    )

    assert read_response.status_code == 200
    body = read_response.json()
    assert body["payload"] == {"metric": "cpu", "value": 12.5}
    assert body["source"] == "runtime"


@pytest.mark.asyncio
async def test_data_write_to_nonexistent_notebook_returns_404(
    internal_api_client: AsyncClient, db_session: AsyncSession
) -> None:
    missing_id = uuid4()
    cluster = FakeRuntimeCluster()
    _use_cluster(cluster)
    _, credential = cluster.mint(notebook_id=missing_id)

    response = await internal_api_client.post(
        f"/api/internal/notebooks/{missing_id}/data",
        headers=_bearer(credential),
        json={"metric": "cpu"},
    )

    assert response.status_code == 404
    stored = await db_session.scalar(select(Notebook).where(Notebook.id == missing_id))
    assert stored is None
