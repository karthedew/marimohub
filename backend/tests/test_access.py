from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notebook, NotebookVisibility, User, Workspace, WorkspaceMember, WorkspaceRole
from app.services.access import (
    Action,
    AuthenticationRequired,
    PermissionDenied,
    ResourceHidden,
    authorize_notebook,
    authorize_workspace,
    can_access,
    get_role,
    load_notebook_for,
    load_workspace_for,
    role_at_least,
    visible_notebooks,
)

PRIVATE = NotebookVisibility.PRIVATE
UNLISTED = NotebookVisibility.UNLISTED
PUBLIC = NotebookVisibility.PUBLIC
VIEWER = WorkspaceRole.VIEWER
EDITOR = WorkspaceRole.EDITOR
OWNER = WorkspaceRole.OWNER


async def make_user(db: AsyncSession, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com")
    db.add(user)
    await db.flush()
    return user


async def make_workspace(db: AsyncSession, slug: str, *, archived: bool = False) -> Workspace:
    now = datetime.now(UTC)
    workspace = Workspace(
        slug=slug,
        name=slug,
        archived_at=now if archived else None,
        purge_after=(now + timedelta(days=30)) if archived else None,
    )
    db.add(workspace)
    await db.flush()
    return workspace


async def add_member(
    db: AsyncSession, workspace: Workspace, user: User, role: WorkspaceRole
) -> None:
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role))
    await db.flush()


async def make_notebook(
    db: AsyncSession,
    workspace: Workspace,
    *,
    title: str = "Notebook",
    visibility: NotebookVisibility = PRIVATE,
    created_by: User | None = None,
) -> Notebook:
    notebook = Notebook(
        workspace_id=workspace.id,
        created_by=created_by.id if created_by else None,
        title=title,
        visibility=visibility,
    )
    db.add(notebook)
    await db.flush()
    return notebook


# ── pure matrix: can_access, 3 visibilities x 4 role states x 2 actions ──────

_READ_EXPECTED: dict[tuple[NotebookVisibility, WorkspaceRole | None], bool] = {
    (PRIVATE, None): False,
    (PRIVATE, VIEWER): True,
    (PRIVATE, EDITOR): True,
    (PRIVATE, OWNER): True,
    (UNLISTED, None): True,
    (UNLISTED, VIEWER): True,
    (UNLISTED, EDITOR): True,
    (UNLISTED, OWNER): True,
    (PUBLIC, None): True,
    (PUBLIC, VIEWER): True,
    (PUBLIC, EDITOR): True,
    (PUBLIC, OWNER): True,
}

_WRITE_EXPECTED = {
    None: False,
    VIEWER: False,
    EDITOR: True,
    OWNER: True,
}


@pytest.mark.parametrize("visibility", [PRIVATE, UNLISTED, PUBLIC])
@pytest.mark.parametrize("role", [None, VIEWER, EDITOR, OWNER])
def test_can_access_read_matrix(visibility: NotebookVisibility, role: WorkspaceRole | None) -> None:
    assert can_access(visibility, role, Action.READ) is _READ_EXPECTED[(visibility, role)]


@pytest.mark.parametrize("visibility", [PRIVATE, UNLISTED, PUBLIC])
@pytest.mark.parametrize("role", [None, VIEWER, EDITOR, OWNER])
def test_can_access_write_matrix(
    visibility: NotebookVisibility, role: WorkspaceRole | None
) -> None:
    assert can_access(visibility, role, Action.WRITE) is _WRITE_EXPECTED[role]


def test_role_at_least_orders_owner_over_editor_over_viewer() -> None:
    assert role_at_least(OWNER, OWNER)
    assert role_at_least(OWNER, EDITOR)
    assert role_at_least(EDITOR, EDITOR)
    assert not role_at_least(EDITOR, OWNER)
    assert not role_at_least(VIEWER, EDITOR)


def test_role_at_least_none_never_suffices() -> None:
    assert not role_at_least(None, VIEWER)


# ── denial selection: _deny via authorize_notebook ───────────────────────────


@pytest.mark.asyncio
async def test_private_notebook_hides_from_anonymous_and_non_member(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session, "priv-owner")
    outsider = await make_user(db_session, "priv-outsider")
    workspace = await make_workspace(db_session, "priv-ws")
    await add_member(db_session, workspace, owner, OWNER)
    notebook = await make_notebook(db_session, workspace, visibility=PRIVATE, created_by=owner)

    with pytest.raises(ResourceHidden):
        await authorize_notebook(db_session, notebook, None, Action.READ)
    with pytest.raises(ResourceHidden):
        await authorize_notebook(db_session, notebook, outsider, Action.READ)


@pytest.mark.asyncio
async def test_readable_notebook_write_by_anonymous_requires_authentication(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session, "pub-owner")
    workspace = await make_workspace(db_session, "pub-ws")
    await add_member(db_session, workspace, owner, OWNER)
    notebook = await make_notebook(db_session, workspace, visibility=PUBLIC, created_by=owner)

    with pytest.raises(AuthenticationRequired):
        await authorize_notebook(db_session, notebook, None, Action.WRITE)


@pytest.mark.asyncio
async def test_readable_notebook_write_by_identified_viewer_is_permission_denied(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session, "view-owner")
    viewer = await make_user(db_session, "view-viewer")
    workspace = await make_workspace(db_session, "view-ws")
    await add_member(db_session, workspace, owner, OWNER)
    await add_member(db_session, workspace, viewer, VIEWER)
    notebook = await make_notebook(db_session, workspace, visibility=PUBLIC, created_by=owner)

    with pytest.raises(PermissionDenied):
        await authorize_notebook(db_session, notebook, viewer, Action.WRITE)


# ── get_role: membership lookup ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_role_returns_member_role(db_session: AsyncSession) -> None:
    member = await make_user(db_session, "role-member")
    workspace = await make_workspace(db_session, "role-ws")
    await add_member(db_session, workspace, member, EDITOR)

    assert await get_role(db_session, workspace.id, member.id) is EDITOR


@pytest.mark.asyncio
async def test_get_role_returns_none_for_non_member_and_anonymous(db_session: AsyncSession) -> None:
    outsider = await make_user(db_session, "role-outsider")
    workspace = await make_workspace(db_session, "role-ws-2")

    assert await get_role(db_session, workspace.id, outsider.id) is None
    assert await get_role(db_session, workspace.id, None) is None


# ── active-workspace filtering ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_notebook_for_hides_notebook_in_archived_workspace_even_for_member(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session, "arch-owner")
    workspace = await make_workspace(db_session, "arch-ws", archived=True)
    await add_member(db_session, workspace, owner, OWNER)
    notebook = await make_notebook(db_session, workspace, visibility=PUBLIC, created_by=owner)

    with pytest.raises(ResourceHidden):
        await load_notebook_for(db_session, notebook.id, owner, Action.READ)


@pytest.mark.asyncio
async def test_visible_notebooks_excludes_archived_workspace(db_session: AsyncSession) -> None:
    owner = await make_user(db_session, "arch-vis-owner")
    workspace = await make_workspace(db_session, "arch-vis-ws", archived=True)
    await add_member(db_session, workspace, owner, OWNER)
    await make_notebook(db_session, workspace, visibility=PUBLIC, created_by=owner)

    rows = (
        await db_session.scalars(select(Notebook).where(visible_notebooks(None)))
    ).all()

    assert rows == []


# ── missing-id vs. hidden-notebook equivalence ────────────────────────────────


@pytest.mark.asyncio
async def test_missing_and_hidden_notebook_raise_the_same_indistinguishable_error(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session, "hide-owner")
    outsider = await make_user(db_session, "hide-outsider")
    workspace = await make_workspace(db_session, "hide-ws")
    await add_member(db_session, workspace, owner, OWNER)
    notebook = await make_notebook(db_session, workspace, visibility=PRIVATE, created_by=owner)

    with pytest.raises(ResourceHidden) as missing:
        await load_notebook_for(db_session, uuid4(), outsider, Action.READ)
    with pytest.raises(ResourceHidden) as hidden:
        await load_notebook_for(db_session, notebook.id, outsider, Action.READ)

    assert missing.value.detail == hidden.value.detail


# ── workspace denial behavior ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_workspace_hides_missing_workspace(db_session: AsyncSession) -> None:
    actor = await make_user(db_session, "ws-missing-actor")

    with pytest.raises(ResourceHidden):
        await authorize_workspace(db_session, uuid4(), actor, VIEWER)


@pytest.mark.asyncio
async def test_load_workspace_for_hides_archived_workspace_even_for_its_owner(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session, "ws-archived-owner")
    workspace = await make_workspace(db_session, "ws-archived", archived=True)
    await add_member(db_session, workspace, owner, OWNER)

    with pytest.raises(ResourceHidden):
        await load_workspace_for(db_session, workspace.id, owner, VIEWER)


@pytest.mark.asyncio
async def test_authorize_workspace_hides_workspace_from_non_member(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session, "ws-nonmember-owner")
    outsider = await make_user(db_session, "ws-nonmember-outsider")
    workspace = await make_workspace(db_session, "ws-nonmember")
    await add_member(db_session, workspace, owner, OWNER)

    with pytest.raises(ResourceHidden):
        await authorize_workspace(db_session, workspace.id, outsider, VIEWER)


@pytest.mark.asyncio
async def test_authorize_workspace_denies_member_below_minimum_role(
    db_session: AsyncSession,
) -> None:
    viewer = await make_user(db_session, "ws-below-viewer")
    workspace = await make_workspace(db_session, "ws-below")
    await add_member(db_session, workspace, viewer, VIEWER)

    with pytest.raises(PermissionDenied):
        await authorize_workspace(db_session, workspace.id, viewer, EDITOR)


@pytest.mark.asyncio
async def test_authorize_workspace_allows_member_at_or_above_minimum_role(
    db_session: AsyncSession,
) -> None:
    editor = await make_user(db_session, "ws-at-editor")
    workspace = await make_workspace(db_session, "ws-at")
    await add_member(db_session, workspace, editor, EDITOR)

    assert await authorize_workspace(db_session, workspace.id, editor, EDITOR) is EDITOR


# ── visible_notebooks discovery predicate ─────────────────────────────────────


@pytest.mark.asyncio
async def test_visible_notebooks_includes_own_private_and_others_public_only(
    db_session: AsyncSession,
) -> None:
    me = await make_user(db_session, "disc-me")
    other = await make_user(db_session, "disc-other")
    my_ws = await make_workspace(db_session, "disc-my-ws")
    other_ws = await make_workspace(db_session, "disc-other-ws")
    await add_member(db_session, my_ws, me, OWNER)
    await add_member(db_session, other_ws, other, OWNER)

    my_private = await make_notebook(
        db_session, my_ws, title="mine", visibility=PRIVATE, created_by=me
    )
    others_public = await make_notebook(
        db_session, other_ws, title="their-public", visibility=PUBLIC, created_by=other
    )
    others_private = await make_notebook(
        db_session, other_ws, title="their-private", visibility=PRIVATE, created_by=other
    )
    others_unlisted = await make_notebook(
        db_session, other_ws, title="their-unlisted", visibility=UNLISTED, created_by=other
    )

    visible_ids = set(
        (await db_session.scalars(select(Notebook.id).where(visible_notebooks(me.id)))).all()
    )

    assert visible_ids == {my_private.id, others_public.id}
    assert others_private.id not in visible_ids
    assert others_unlisted.id not in visible_ids


@pytest.mark.asyncio
async def test_visible_notebooks_anonymous_sees_only_active_public(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session, "disc-anon-owner")
    workspace = await make_workspace(db_session, "disc-anon-ws")
    await add_member(db_session, workspace, owner, OWNER)

    public = await make_notebook(db_session, workspace, visibility=PUBLIC, created_by=owner)
    await make_notebook(
        db_session, workspace, title="private", visibility=PRIVATE, created_by=owner
    )
    await make_notebook(
        db_session, workspace, title="unlisted", visibility=UNLISTED, created_by=owner
    )

    visible_ids = set(
        (await db_session.scalars(select(Notebook.id).where(visible_notebooks(None)))).all()
    )

    assert visible_ids == {public.id}
