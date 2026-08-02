from datetime import UTC, datetime, timedelta
from uuid import UUID

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError
from app.main import app
from app.models import Deployment, Notebook, User, Workspace, WorkspaceMember, WorkspaceRole
from app.services.session_manager import get_session_manager
from app.services.workspace_service import delete_user, owner_count, purge_due_workspaces
from test_auth import login_user, register_user
from test_deployments import FakeDeploymentSessionManager
from test_notebooks import create_notebook, json_dict, register_and_login

pytestmark = pytest.mark.asyncio


async def _register(client: AsyncClient, username: str) -> tuple[dict[str, object], dict[str, str]]:
    """Register and log in a bare user with no workspace of their own."""
    user = await register_user(client, username=username, email=f"{username}@example.com")
    token = await login_user(client, username=username)
    return user, {"Authorization": f"Bearer {token}"}


async def _create_workspace(
    client: AsyncClient, headers: dict[str, str], name: str, slug: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {"name": name}
    if slug is not None:
        payload["slug"] = slug
    response = await client.post("/api/workspaces", headers=headers, json=payload)
    assert response.status_code == 201
    return json_dict(response)


async def test_create_workspace_derives_slug_and_grants_owner_membership(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await _register(api_client, "ws-creator")

    created = await _create_workspace(api_client, headers, "My Cool Team!")

    assert created["slug"] == "my-cool-team"
    assert created["role"] == "owner"

    workspace = await db_session.scalar(select(Workspace).where(Workspace.slug == "my-cool-team"))
    assert workspace is not None
    members = (
        await db_session.scalars(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace.id)
        )
    ).all()
    assert len(members) == 1
    assert members[0].role == WorkspaceRole.OWNER


async def test_create_workspace_explicit_slug_conflict_leaves_no_orphan(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    first_user, first_headers = await _register(api_client, "slug-first")
    _, second_headers = await _register(api_client, "slug-second")

    await _create_workspace(api_client, first_headers, "Alpha", slug="shared-slug")
    conflict = await api_client.post(
        "/api/workspaces", headers=second_headers, json={"name": "Beta", "slug": "shared-slug"}
    )

    assert conflict.status_code == 409
    rows = (
        await db_session.scalars(select(Workspace).where(Workspace.slug == "shared-slug"))
    ).all()
    assert len(rows) == 1

    members = (
        await db_session.scalars(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == rows[0].id)
        )
    ).all()
    assert len(members) == 1
    assert members[0].user_id == UUID(str(first_user["id"]))


async def test_list_my_workspaces_excludes_archived(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await _register(api_client, "lister")
    first = await _create_workspace(api_client, headers, "First")
    await _create_workspace(api_client, headers, "Second")
    archive = await api_client.delete(f"/api/workspaces/{first['id']}", headers=headers)
    assert archive.status_code == 204

    listed = await api_client.get("/api/workspaces", headers=headers)

    assert listed.status_code == 200
    slugs = [item["slug"] for item in listed.json()]
    assert first["slug"] not in slugs
    assert len(slugs) == 1


async def test_non_member_gets_404_on_get_members_and_rename(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers = await _register(api_client, "priv-owner")
    _, outsider_headers = await _register(api_client, "priv-outsider")
    workspace = await _create_workspace(api_client, owner_headers, "Private Co")
    workspace_id = workspace["id"]

    get_resp = await api_client.get(f"/api/workspaces/{workspace_id}", headers=outsider_headers)
    members_resp = await api_client.get(
        f"/api/workspaces/{workspace_id}/members", headers=outsider_headers
    )
    rename_resp = await api_client.patch(
        f"/api/workspaces/{workspace_id}", headers=outsider_headers, json={"name": "Hijacked"}
    )
    missing_resp = await api_client.get(
        "/api/workspaces/00000000-0000-0000-0000-000000000000", headers=owner_headers
    )

    assert get_resp.status_code == 404
    assert members_resp.status_code == 404
    assert rename_resp.status_code == 404
    assert missing_resp.status_code == 404


async def test_non_owner_members_are_forbidden_from_management(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers = await _register(api_client, "mgmt-owner")
    editor, editor_headers = await _register(api_client, "mgmt-editor")
    outsider, _ = await _register(api_client, "mgmt-outsider")
    workspace = await _create_workspace(api_client, owner_headers, "Managed")
    workspace_id = workspace["id"]

    add = await api_client.post(
        f"/api/workspaces/{workspace_id}/members",
        headers=owner_headers,
        json={"user_id": editor["id"], "role": "editor"},
    )
    assert add.status_code == 201

    forbidden_rename = await api_client.patch(
        f"/api/workspaces/{workspace_id}", headers=editor_headers, json={"name": "Nope"}
    )
    forbidden_add = await api_client.post(
        f"/api/workspaces/{workspace_id}/members",
        headers=editor_headers,
        json={"user_id": outsider["id"], "role": "viewer"},
    )

    assert forbidden_rename.status_code == 403
    assert forbidden_add.status_code == 403


async def test_add_member_unknown_user_404_and_duplicate_409(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers = await _register(api_client, "add-owner")
    member, _ = await _register(api_client, "add-member")
    workspace = await _create_workspace(api_client, owner_headers, "Adders")
    workspace_id = workspace["id"]

    unknown = await api_client.post(
        f"/api/workspaces/{workspace_id}/members",
        headers=owner_headers,
        json={"user_id": "00000000-0000-0000-0000-000000000000", "role": "editor"},
    )
    first_add = await api_client.post(
        f"/api/workspaces/{workspace_id}/members",
        headers=owner_headers,
        json={"user_id": member["id"], "role": "editor"},
    )
    duplicate_add = await api_client.post(
        f"/api/workspaces/{workspace_id}/members",
        headers=owner_headers,
        json={"user_id": member["id"], "role": "viewer"},
    )

    assert unknown.status_code == 404
    assert first_add.status_code == 201
    assert duplicate_add.status_code == 409


async def test_last_owner_cannot_be_demoted_or_removed_but_co_owner_can(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, owner_headers = await _register(api_client, "lastowner-a")
    co_owner, co_owner_headers = await _register(api_client, "lastowner-b")
    workspace = await _create_workspace(api_client, owner_headers, "Lastowner Co")
    workspace_id = workspace["id"]

    solo_demote = await api_client.patch(
        f"/api/workspaces/{workspace_id}/members/{owner['id']}",
        headers=owner_headers,
        json={"role": "editor"},
    )
    solo_remove = await api_client.delete(
        f"/api/workspaces/{workspace_id}/members/{owner['id']}", headers=owner_headers
    )
    assert solo_demote.status_code == 409
    assert solo_remove.status_code == 409

    added = await api_client.post(
        f"/api/workspaces/{workspace_id}/members",
        headers=owner_headers,
        json={"user_id": co_owner["id"], "role": "owner"},
    )
    assert added.status_code == 201

    demote_with_co_owner = await api_client.patch(
        f"/api/workspaces/{workspace_id}/members/{owner['id']}",
        headers=owner_headers,
        json={"role": "editor"},
    )
    assert demote_with_co_owner.status_code == 200

    final_demote = await api_client.patch(
        f"/api/workspaces/{workspace_id}/members/{co_owner['id']}",
        headers=co_owner_headers,
        json={"role": "editor"},
    )
    assert final_demote.status_code == 409


async def test_archive_hides_workspace_and_its_notebooks(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, workspace_id = await register_and_login(
        api_client, db_session, "arch-owner", WorkspaceRole.OWNER
    )
    notebook = await create_notebook(api_client, owner_headers, workspace_id, "Doomed", "x = 1")

    archived = await api_client.delete(f"/api/workspaces/{workspace_id}", headers=owner_headers)
    assert archived.status_code == 204

    hidden_workspace = await api_client.get(
        f"/api/workspaces/{workspace_id}", headers=owner_headers
    )
    hidden_notebook = await api_client.get(
        f"/api/notebooks/{notebook['id']}", headers=owner_headers
    )
    assert hidden_workspace.status_code == 404
    assert hidden_notebook.status_code == 404


async def test_owner_archive_listing_and_restore_clears_stamps(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers = await _register(api_client, "restore-owner")
    workspace = await _create_workspace(api_client, owner_headers, "Restorable")
    workspace_id = workspace["id"]

    await api_client.delete(f"/api/workspaces/{workspace_id}", headers=owner_headers)

    archived_list = await api_client.get("/api/workspaces/archived", headers=owner_headers)
    assert archived_list.status_code == 200
    archived_items = archived_list.json()
    assert len(archived_items) == 1
    assert archived_items[0]["id"] == workspace_id
    assert archived_items[0]["archived_at"] is not None
    assert archived_items[0]["purge_after"] is not None

    restore_missing_when_active = await api_client.post(
        "/api/workspaces/00000000-0000-0000-0000-000000000000/restore", headers=owner_headers
    )
    assert restore_missing_when_active.status_code == 404

    restored = await api_client.post(
        f"/api/workspaces/{workspace_id}/restore", headers=owner_headers
    )
    assert restored.status_code == 200

    row = await db_session.get(Workspace, UUID(str(workspace_id)))
    assert row is not None
    assert row.archived_at is None
    assert row.purge_after is None

    visible_again = await api_client.get(f"/api/workspaces/{workspace_id}", headers=owner_headers)
    assert visible_again.status_code == 200


async def test_restore_requires_owner_role(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers = await _register(api_client, "restore-perm-owner")
    editor, editor_headers = await _register(api_client, "restore-perm-editor")
    workspace = await _create_workspace(api_client, owner_headers, "Guarded")
    workspace_id = workspace["id"]
    await api_client.post(
        f"/api/workspaces/{workspace_id}/members",
        headers=owner_headers,
        json={"user_id": editor["id"], "role": "editor"},
    )
    await api_client.delete(f"/api/workspaces/{workspace_id}", headers=owner_headers)

    forbidden = await api_client.post(
        f"/api/workspaces/{workspace_id}/restore", headers=editor_headers
    )

    assert forbidden.status_code == 403


async def test_archived_purge_deadline_is_fixed_at_retention_window(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await _register(api_client, "deadline-owner")
    workspace = await _create_workspace(api_client, headers, "Deadline Co")
    workspace_id = workspace["id"]

    await api_client.delete(f"/api/workspaces/{workspace_id}", headers=headers)

    row = await db_session.get(Workspace, UUID(str(workspace_id)))
    assert row is not None
    assert row.archived_at is not None
    assert row.purge_after is not None
    expected = row.archived_at + timedelta(days=get_settings().WORKSPACE_ARCHIVE_RETENTION_DAYS)
    assert row.purge_after == expected


async def test_archive_stops_workspace_deployment_runtimes(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, workspace_id = await register_and_login(
        api_client, db_session, "stoprun-owner", WorkspaceRole.OWNER
    )
    notebook = await create_notebook(api_client, owner_headers, workspace_id, "Runnable", "x = 1")
    deployed = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "stoprun-slug"},
    )
    assert deployed.status_code == 200
    deployment_id = await db_session.scalar(
        select(Deployment.id).where(Deployment.slug == "stoprun-slug")
    )
    assert deployment_id is not None

    manager = FakeDeploymentSessionManager()
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        # Wake it once first so the manager actually has a live Runtime
        # tracked to stop -- a Deployment nobody ever visited has no Runtime
        # for archive to tear down in the first place.
        await api_client.get("/api/deployments/stoprun-slug")
        response = await api_client.delete(f"/api/workspaces/{workspace_id}", headers=owner_headers)
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 204
    assert deployment_id in manager.stopped
    assert workspace_id in manager.stopped_workspaces


async def test_purge_due_workspaces_is_idempotent_and_cascades(db_session: AsyncSession) -> None:
    owner = User(username="purge-owner", email="purge-owner@example.com")
    db_session.add(owner)
    await db_session.flush()

    now = datetime.now(UTC)
    due = Workspace(
        slug="purge-due",
        name="Due",
        archived_at=now - timedelta(days=40),
        purge_after=now - timedelta(days=1),
    )
    not_due = Workspace(
        slug="purge-not-due",
        name="Not due",
        archived_at=now - timedelta(days=1),
        purge_after=now + timedelta(days=29),
    )
    db_session.add_all([due, not_due])
    await db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=due.id, user_id=owner.id, role=WorkspaceRole.OWNER))
    db_session.add(
        WorkspaceMember(workspace_id=not_due.id, user_id=owner.id, role=WorkspaceRole.OWNER)
    )
    notebook = Notebook(workspace_id=due.id, created_by=owner.id, title="Cascaded")
    db_session.add(notebook)
    await db_session.commit()
    due_id, not_due_id, notebook_id = due.id, not_due.id, notebook.id

    first_run = await purge_due_workspaces(db_session, FakeDeploymentSessionManager(), now)
    assert first_run == 1
    # The purge's raw DELETE cascades notebooks at the DB level, invisible to this
    # session's identity map; expire everything so the checks below re-query.
    db_session.expire_all()

    assert await db_session.get(Workspace, due_id) is None
    assert await db_session.get(Notebook, notebook_id) is None
    assert await db_session.get(Workspace, not_due_id) is not None

    second_run = await purge_due_workspaces(db_session, FakeDeploymentSessionManager(), now)
    assert second_run == 0


async def test_delete_user_hard_deletes_sole_member_workspaces(db_session: AsyncSession) -> None:
    user = User(username="solo-delete", email="solo-delete@example.com")
    db_session.add(user)
    await db_session.flush()
    workspace = Workspace(slug="solo-ws", name="Solo")
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER)
    )
    await db_session.commit()

    await delete_user(db_session, FakeDeploymentSessionManager(), user.id)

    assert await db_session.get(Workspace, workspace.id) is None
    assert await db_session.get(User, user.id) is None


async def test_delete_user_blocks_on_sole_ownership_of_shared_workspace(
    db_session: AsyncSession,
) -> None:
    sole_owner = User(username="shared-sole-owner", email="shared-sole-owner@example.com")
    other_member = User(username="shared-other", email="shared-other@example.com")
    db_session.add_all([sole_owner, other_member])
    await db_session.flush()
    workspace = Workspace(slug="shared-ws", name="Shared")
    db_session.add(workspace)
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=workspace.id, user_id=sole_owner.id, role=WorkspaceRole.OWNER
            ),
            WorkspaceMember(
                workspace_id=workspace.id, user_id=other_member.id, role=WorkspaceRole.EDITOR
            ),
        ]
    )
    await db_session.commit()

    with pytest.raises(ConflictError):
        await delete_user(db_session, FakeDeploymentSessionManager(), sole_owner.id)

    assert await db_session.get(User, sole_owner.id) is not None
    remaining = await owner_count(db_session, workspace.id)
    assert remaining == 1


async def test_delete_user_succeeds_when_co_owner_of_shared_workspace(
    db_session: AsyncSession,
) -> None:
    co_owner = User(username="co-owner-a", email="co-owner-a@example.com")
    other_owner = User(username="co-owner-b", email="co-owner-b@example.com")
    db_session.add_all([co_owner, other_owner])
    await db_session.flush()
    workspace = Workspace(slug="co-owned-ws", name="Co-owned")
    db_session.add(workspace)
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=workspace.id, user_id=co_owner.id, role=WorkspaceRole.OWNER
            ),
            WorkspaceMember(
                workspace_id=workspace.id, user_id=other_owner.id, role=WorkspaceRole.OWNER
            ),
        ]
    )
    await db_session.commit()

    await delete_user(db_session, FakeDeploymentSessionManager(), co_owner.id)

    assert await db_session.get(User, co_owner.id) is None
    assert await db_session.get(Workspace, workspace.id) is not None
    assert await owner_count(db_session, workspace.id) == 1


async def test_delete_user_blocked_by_shared_workspace_leaves_sole_member_workspace_untouched(
    db_session: AsyncSession,
) -> None:
    user = User(username="combined-sole", email="combined-sole@example.com")
    other_member = User(username="combined-other", email="combined-other@example.com")
    db_session.add_all([user, other_member])
    await db_session.flush()
    solo_workspace = Workspace(slug="combined-solo-ws", name="Combined Solo")
    shared_workspace = Workspace(slug="combined-shared-ws", name="Combined Shared")
    db_session.add_all([solo_workspace, shared_workspace])
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=solo_workspace.id, user_id=user.id, role=WorkspaceRole.OWNER
            ),
            WorkspaceMember(
                workspace_id=shared_workspace.id, user_id=user.id, role=WorkspaceRole.OWNER
            ),
            WorkspaceMember(
                workspace_id=shared_workspace.id, user_id=other_member.id, role=WorkspaceRole.EDITOR
            ),
        ]
    )
    await db_session.commit()

    with pytest.raises(ConflictError):
        await delete_user(db_session, FakeDeploymentSessionManager(), user.id)

    assert await db_session.get(User, user.id) is not None
    assert await db_session.get(Workspace, solo_workspace.id) is not None
    remaining_members = (
        await db_session.scalars(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == solo_workspace.id)
        )
    ).all()
    assert len(remaining_members) == 1
