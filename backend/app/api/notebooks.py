from collections.abc import Sequence
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import NotebookRead, NotebookWrite, get_current_user, get_current_user_optional
from app.db.database import get_db
from app.models import Notebook, NotebookVisibility, User, Workspace, WorkspaceRole
from app.schemas import (
    NotebookCreate,
    NotebookFork,
    NotebookImport,
    NotebookListOut,
    NotebookOut,
    NotebookPublish,
    NotebookUpdate,
)
from app.services.access import (
    Action,
    authorize_workspace,
    can_access,
    get_role,
    load_notebook_for,
    visible_notebooks,
)
from app.services.embedding_service import EmbeddingService, embedding_service
from app.services.gitlab_import import import_gitlab_notebook
from app.services.notebook_storage import NotebookStorageService, get_notebook_storage

router = APIRouter(prefix="/api/notebooks", tags=["notebooks"])


class NotebookListQuery(BaseModel):
    """Query parameters for listing and searching notebooks."""

    q: str | None = None
    semantic: str | None = None
    tags: Annotated[list[str] | None, Query()] = None
    workspace_id: UUID | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


def get_embedding_service() -> EmbeddingService:
    """Provide the shared embedding service."""
    return embedding_service


def _embedding_text(notebook: Notebook) -> str:
    parts = [notebook.title, notebook.description or "", " ".join(notebook.tags)]
    return " ".join(part for part in parts if part).strip()


type ParentAttribution = tuple[str, UUID, str]  # title, workspace_id, workspace slug


async def _parent_attribution(
    db: AsyncSession, actor: User | None, parent_id: UUID
) -> ParentAttribution | None:
    """Resolve one parent's attribution, or `None` if the actor cannot read it.

    A since-privatized parent or one whose workspace was archived must not leak
    through a fork's response; `can_access` alone doesn't know about archival,
    so the join filters it explicitly.
    """
    row = (
        await db.execute(
            select(Notebook, Workspace.slug)
            .join(Workspace, Notebook.workspace_id == Workspace.id)
            .where(Notebook.id == parent_id, Workspace.archived_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        return None
    parent, parent_slug = row._tuple()
    role = await get_role(db, parent.workspace_id, actor.id if actor else None)
    if can_access(parent.visibility, role, Action.READ):
        return parent.title, parent.workspace_id, parent_slug
    return None


async def _parent_attribution_batch(
    db: AsyncSession, actor: User | None, notebooks: Sequence[Notebook]
) -> dict[UUID, ParentAttribution | None]:
    """Batch-resolve parent attribution for a page of notebooks in one query.

    Per-row lookups here would be an N+1 against forked notebooks; a listing
    page instead loads every distinct parent it needs up front.
    """
    parent_ids = {notebook.parent_id for notebook in notebooks if notebook.parent_id is not None}
    if not parent_ids:
        return {}
    rows = await db.execute(
        select(Notebook, Workspace.slug)
        .join(Workspace, Notebook.workspace_id == Workspace.id)
        .where(Notebook.id.in_(parent_ids), Workspace.archived_at.is_(None))
    )
    attribution: dict[UUID, ParentAttribution | None] = dict.fromkeys(parent_ids)
    for parent, parent_slug in rows:
        role = await get_role(db, parent.workspace_id, actor.id if actor else None)
        if can_access(parent.visibility, role, Action.READ):
            attribution[parent.id] = (parent.title, parent.workspace_id, parent_slug)
    return attribution


async def _notebook_out(
    db: AsyncSession,
    notebook: Notebook,
    actor: User | None,
    storage: NotebookStorageService,
    *,
    include_source: bool,
    parent_cache: dict[UUID, ParentAttribution | None] | None = None,
) -> NotebookOut:
    payload = {
        "id": notebook.id,
        "workspace_id": notebook.workspace_id,
        "created_by": notebook.created_by,
        "parent_id": notebook.parent_id,
        "title": notebook.title,
        "description": notebook.description,
        "tags": notebook.tags,
        "visibility": notebook.visibility,
        "fork_count": notebook.fork_count,
        "created_at": notebook.created_at,
        "updated_at": notebook.updated_at,
    }
    if notebook.parent_id is not None:
        attribution = (
            parent_cache.get(notebook.parent_id)
            if parent_cache is not None
            else await _parent_attribution(db, actor, notebook.parent_id)
        )
        if attribution is not None:
            parent_title, parent_workspace_id, parent_workspace_slug = attribution
            payload["parent_title"] = parent_title
            payload["parent_workspace_id"] = parent_workspace_id
            payload["parent_workspace_slug"] = parent_workspace_slug
    if include_source:
        payload["source"] = await storage.get(notebook)
    return NotebookOut.model_validate(payload)


async def _resolve_target_workspace(db: AsyncSession, actor: User, requested: UUID) -> UUID:
    """Require EDITOR in the explicitly selected active target workspace."""
    await authorize_workspace(db, requested, actor, WorkspaceRole.EDITOR)
    return requested


@router.get("", response_model=NotebookListOut, response_model_exclude_unset=True)
async def list_notebooks(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
    embeddings: Annotated[EmbeddingService, Depends(get_embedding_service)],
    query: Annotated[NotebookListQuery, Query()],
) -> NotebookListOut:
    """List and search notebooks visible to the caller, with pagination."""
    semantic_query = query.semantic.strip() if query.semantic and query.semantic.strip() else None

    filters = [visible_notebooks(current_user.id if current_user else None)]
    if query.workspace_id is not None:
        filters.append(Notebook.workspace_id == query.workspace_id)
    order_by = [Notebook.updated_at.desc()]
    if semantic_query is not None:
        query_embedding = await embeddings.embed(semantic_query)
        # cosine_distance is contributed by pgvector, which ships no type stubs.
        distance: ColumnElement[float] = cast(
            "ColumnElement[float]",
            Notebook.embedding.cosine_distance(query_embedding),  # pyright: ignore[reportAny]
        )
        filters.append(Notebook.embedding.is_not(None))
        order_by = [distance.asc(), Notebook.updated_at.desc()]
    if query.tags:
        filters.append(Notebook.tags.contains(query.tags))
    if query.q and query.q.strip():
        tsquery = func.websearch_to_tsquery("english", query.q.strip())
        filters.append(Notebook.search_vector.op("@@")(tsquery))
        if semantic_query is None:
            order_by.insert(0, func.ts_rank(Notebook.search_vector, tsquery).desc())

    total = await db.scalar(select(func.count()).select_from(Notebook).where(*filters))
    notebooks = (
        await db.scalars(
            select(Notebook)
            .where(*filters)
            .order_by(*order_by)
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        )
    ).all()

    parent_cache = await _parent_attribution_batch(db, current_user, notebooks)
    items = [
        await _notebook_out(
            db, notebook, current_user, storage, include_source=False, parent_cache=parent_cache
        )
        for notebook in notebooks
    ]

    return NotebookListOut(
        items=items,
        total=total or 0,
        page=query.page,
        page_size=query.page_size,
    )


@router.post(
    "",
    response_model=NotebookOut,
    response_model_exclude_unset=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_notebook(
    payload: NotebookCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    """Create a new private notebook in the caller's chosen workspace."""
    workspace_id = await _resolve_target_workspace(db, current_user, payload.workspace_id)
    notebook = Notebook(
        workspace_id=workspace_id,
        created_by=current_user.id,
        title=payload.title,
        description=payload.description,
        tags=payload.tags,
        visibility=NotebookVisibility.PRIVATE,
    )
    db.add(notebook)
    await db.flush()
    await storage.put(notebook, payload.source)
    await db.commit()
    await db.refresh(notebook)
    return await _notebook_out(db, notebook, current_user, storage, include_source=True)


@router.post(
    "/import",
    response_model=NotebookOut,
    response_model_exclude_unset=True,
    status_code=status.HTTP_201_CREATED,
)
async def import_notebook(
    payload: NotebookImport,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    """Import a notebook from an external URL into the caller's chosen workspace."""
    workspace_id = await _resolve_target_workspace(db, current_user, payload.workspace_id)
    imported = await import_gitlab_notebook(payload.url, payload.pat)

    notebook = Notebook(
        workspace_id=workspace_id,
        created_by=current_user.id,
        title=imported.title,
        visibility=NotebookVisibility.PRIVATE,
    )
    db.add(notebook)
    await db.flush()
    await storage.put(notebook, imported.source)
    await db.commit()
    await db.refresh(notebook)
    return await _notebook_out(db, notebook, current_user, storage, include_source=True)


@router.get("/{notebook_id}", response_model=NotebookOut, response_model_exclude_unset=True)
async def get_notebook(
    ctx: NotebookRead,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    """Return a single notebook visible to the caller."""
    return await _notebook_out(db, ctx.notebook, ctx.actor, storage, include_source=True)


@router.post(
    "/{notebook_id}/fork",
    response_model=NotebookOut,
    response_model_exclude_unset=True,
    status_code=status.HTTP_201_CREATED,
)
async def fork_notebook(
    notebook_id: UUID,
    payload: NotebookFork,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    """Fork a readable notebook into a new private notebook in the chosen workspace."""
    source, _ = await load_notebook_for(db, notebook_id, current_user, Action.READ)
    target_workspace_id = await _resolve_target_workspace(db, current_user, payload.workspace_id)
    source_code = await storage.get(source)
    fork = Notebook(
        workspace_id=target_workspace_id,
        created_by=current_user.id,
        parent_id=source.id,
        title=source.title,
        description=source.description,
        tags=list(source.tags),
        visibility=NotebookVisibility.PRIVATE,
    )
    db.add(fork)
    await db.flush()
    await storage.put(fork, source_code)
    await db.execute(
        update(Notebook).where(Notebook.id == source.id).values(fork_count=Notebook.fork_count + 1)
    )
    await db.commit()
    await db.refresh(fork)
    return await _notebook_out(db, fork, current_user, storage, include_source=True)


@router.put("/{notebook_id}", response_model=NotebookOut, response_model_exclude_unset=True)
async def update_notebook(
    ctx: NotebookWrite,
    payload: NotebookUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    """Apply a partial update to a notebook the caller can edit."""
    notebook = ctx.notebook
    fields = payload.model_fields_set

    if "title" in fields and payload.title is not None:
        notebook.title = payload.title
    if "description" in fields:
        notebook.description = payload.description
    if "tags" in fields and payload.tags is not None:
        notebook.tags = payload.tags
    if "source" in fields:
        await storage.put(notebook, payload.source)

    db.add(notebook)
    await db.commit()
    await db.refresh(notebook)
    return await _notebook_out(db, notebook, ctx.actor, storage, include_source=True)


@router.delete("/{notebook_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_notebook(
    ctx: NotebookWrite,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> Response:
    """Delete a notebook the caller can edit."""
    await storage.delete(ctx.notebook.id)
    await db.delete(ctx.notebook)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{notebook_id}/publish", response_model=NotebookOut, response_model_exclude_unset=True
)
async def publish_notebook(
    ctx: NotebookWrite,
    payload: NotebookPublish,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
    embeddings: Annotated[EmbeddingService, Depends(get_embedding_service)],
) -> NotebookOut:
    """Change a notebook's visibility, computing its embedding when leaving private."""
    notebook = ctx.notebook
    leaving_private = (
        notebook.visibility == NotebookVisibility.PRIVATE
        and payload.visibility != NotebookVisibility.PRIVATE
    )
    notebook.visibility = payload.visibility
    if leaving_private:
        notebook.embedding = await embeddings.embed(_embedding_text(notebook))
    db.add(notebook)
    await db.commit()
    await db.refresh(notebook)
    return await _notebook_out(db, notebook, ctx.actor, storage, include_source=True)
