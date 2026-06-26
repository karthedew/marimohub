from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_current_user_optional
from app.db.database import get_db
from app.models import Notebook, NotebookVisibility, User
from app.schemas import NotebookCreate, NotebookImport, NotebookListOut, NotebookOut, NotebookPublish, NotebookUpdate
from app.services.embedding_service import EmbeddingService, embedding_service
from app.services.gitlab_import import GitLabImportError, import_gitlab_notebook
from app.services.notebook_storage import NotebookStorageService, PostgresNotebookStorage

router = APIRouter(prefix="/api/notebooks", tags=["notebooks"])


def get_notebook_storage(db: Annotated[AsyncSession, Depends(get_db)]) -> NotebookStorageService:
    return PostgresNotebookStorage(db)


def get_embedding_service() -> EmbeddingService:
    return embedding_service


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notebook not found")


def _is_owner(notebook: Notebook, user: User | None) -> bool:
    return user is not None and notebook.user_id == user.id


def _can_view(notebook: Notebook, user: User | None) -> bool:
    return notebook.visibility in {NotebookVisibility.UNLISTED, NotebookVisibility.PUBLIC} or _is_owner(notebook, user)


def _embedding_text(notebook: Notebook) -> str:
    parts = [notebook.title, notebook.description or "", " ".join(notebook.tags)]
    return " ".join(part for part in parts if part).strip()


async def _notebook_out(
    db: AsyncSession,
    notebook: Notebook,
    user: User | None,
    storage: NotebookStorageService,
) -> NotebookOut:
    payload = {
        "id": notebook.id,
        "user_id": notebook.user_id,
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
        parent_row = (
            await db.execute(
                select(Notebook, User.username)
                .join(User, Notebook.user_id == User.id)
                .where(Notebook.id == notebook.parent_id)
            )
        ).one_or_none()
        if parent_row is not None:
            parent, parent_username = parent_row
            if _can_view(parent, user):
                payload["parent_title"] = parent.title
                payload["parent_owner_id"] = parent.user_id
                payload["parent_owner_username"] = parent_username
    if _is_owner(notebook, user):
        payload["source"] = await storage.get(notebook)
    return NotebookOut.model_validate(payload)


async def _get_visible_notebook(db: AsyncSession, notebook_id: UUID, user: User | None) -> Notebook:
    notebook = await db.scalar(select(Notebook).where(Notebook.id == notebook_id))
    if notebook is None or not _can_view(notebook, user):
        raise _not_found()
    return notebook


async def _get_owned_notebook(db: AsyncSession, notebook_id: UUID, user: User) -> Notebook:
    notebook = await db.scalar(select(Notebook).where(Notebook.id == notebook_id))
    if notebook is None:
        raise _not_found()
    if notebook.user_id != user.id:
        if notebook.visibility == NotebookVisibility.DRAFT:
            raise _not_found()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Notebook owner required")
    return notebook


@router.get("", response_model=NotebookListOut, response_model_exclude_unset=True)
async def list_notebooks(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
    embeddings: Annotated[EmbeddingService, Depends(get_embedding_service)],
    q: str | None = None,
    semantic: str | None = None,
    tags: Annotated[list[str] | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> NotebookListOut:
    semantic_query = semantic.strip() if semantic and semantic.strip() else None
    visibility_filter = Notebook.visibility == NotebookVisibility.PUBLIC
    if current_user is not None and semantic_query is None:
        visibility_filter = or_(visibility_filter, Notebook.user_id == current_user.id)

    filters = [visibility_filter]
    order_by = [Notebook.updated_at.desc()]
    if semantic_query is not None:
        query_embedding = await embeddings.embed(semantic_query)
        distance = Notebook.embedding.cosine_distance(query_embedding)
        filters.append(Notebook.embedding.is_not(None))
        order_by = [distance.asc(), Notebook.updated_at.desc()]
    if tags:
        filters.append(Notebook.tags.contains(tags))
    if q and q.strip():
        tsquery = func.websearch_to_tsquery("english", q.strip())
        filters.append(Notebook.search_vector.op("@@")(tsquery))
        if semantic_query is None:
            order_by.insert(0, func.ts_rank(Notebook.search_vector, tsquery).desc())

    total = await db.scalar(select(func.count()).select_from(Notebook).where(*filters))
    result = await db.scalars(
        select(Notebook).where(*filters).order_by(*order_by).offset((page - 1) * page_size).limit(page_size)
    )

    items = [await _notebook_out(db, notebook, current_user, storage) for notebook in result]

    return NotebookListOut(
        items=items,
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=NotebookOut, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
async def create_notebook(
    payload: NotebookCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    notebook = Notebook(
        user_id=current_user.id,
        title=payload.title,
        description=payload.description,
        tags=payload.tags,
        visibility=NotebookVisibility.DRAFT,
    )
    db.add(notebook)
    await db.flush()
    await storage.put(notebook, payload.source)
    await db.commit()
    await db.refresh(notebook)
    return await _notebook_out(db, notebook, current_user, storage)


@router.post("/import", response_model=NotebookOut, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
async def import_notebook(
    payload: NotebookImport,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    try:
        imported = await import_gitlab_notebook(payload.url, payload.pat)
    except GitLabImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    notebook = Notebook(
        user_id=current_user.id,
        title=imported.title,
        visibility=NotebookVisibility.DRAFT,
    )
    db.add(notebook)
    await db.flush()
    await storage.put(notebook, imported.source)
    await db.commit()
    await db.refresh(notebook)
    return await _notebook_out(db, notebook, current_user, storage)


@router.get("/{notebook_id}", response_model=NotebookOut, response_model_exclude_unset=True)
async def get_notebook(
    notebook_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    notebook = await _get_visible_notebook(db, notebook_id, current_user)
    return await _notebook_out(db, notebook, current_user, storage)


@router.post("/{notebook_id}/fork", response_model=NotebookOut, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
async def fork_notebook(
    notebook_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    source = await _get_visible_notebook(db, notebook_id, current_user)
    source_code = await storage.get(source)
    fork = Notebook(
        user_id=current_user.id,
        parent_id=source.id,
        title=source.title,
        description=source.description,
        tags=list(source.tags),
        visibility=NotebookVisibility.DRAFT,
    )
    db.add(fork)
    await db.flush()
    await storage.put(fork, source_code)
    await db.execute(update(Notebook).where(Notebook.id == source.id).values(fork_count=Notebook.fork_count + 1))
    await db.commit()
    await db.refresh(fork)
    return await _notebook_out(db, fork, current_user, storage)


@router.put("/{notebook_id}", response_model=NotebookOut, response_model_exclude_unset=True)
async def update_notebook(
    notebook_id: UUID,
    payload: NotebookUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> NotebookOut:
    notebook = await _get_owned_notebook(db, notebook_id, current_user)
    data = payload.model_dump(exclude_unset=True)

    if "title" in data:
        notebook.title = data["title"]
    if "description" in data:
        notebook.description = data["description"]
    if "tags" in data:
        notebook.tags = data["tags"]
    if "source" in data:
        await storage.put(notebook, data["source"])

    db.add(notebook)
    await db.commit()
    await db.refresh(notebook)
    return await _notebook_out(db, notebook, current_user, storage)


@router.delete("/{notebook_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_notebook(
    notebook_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> Response:
    notebook = await _get_owned_notebook(db, notebook_id, current_user)
    await storage.delete(notebook.id)
    await db.delete(notebook)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{notebook_id}/publish", response_model=NotebookOut, response_model_exclude_unset=True)
async def publish_notebook(
    notebook_id: UUID,
    payload: NotebookPublish,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
    embeddings: Annotated[EmbeddingService, Depends(get_embedding_service)],
) -> NotebookOut:
    notebook = await _get_owned_notebook(db, notebook_id, current_user)
    leaving_draft = notebook.visibility == NotebookVisibility.DRAFT and payload.visibility != NotebookVisibility.DRAFT
    notebook.visibility = payload.visibility
    if leaving_draft:
        notebook.embedding = await embeddings.embed(_embedding_text(notebook))
    db.add(notebook)
    await db.commit()
    await db.refresh(notebook)
    return await _notebook_out(db, notebook, current_user, storage)
