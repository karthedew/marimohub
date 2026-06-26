from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.api.notebooks import get_notebook_storage
from app.db.database import get_db
from app.models import Notebook, NotebookVisibility, User
from app.schemas import SessionCreate, SessionOut
from app.services.notebook_storage import NotebookStorageService
from app.services.process_manager import (
    PortAllocationError,
    ProcessManager,
    SessionCapacityError,
    SessionInfo,
    SessionMode,
    SessionNotFoundError,
    get_process_manager,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notebook not found")


def _auth_error() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def _is_owner(notebook: Notebook, user: User | None) -> bool:
    return user is not None and notebook.user_id == user.id


def _can_view(notebook: Notebook, user: User | None) -> bool:
    return notebook.visibility in {
        NotebookVisibility.UNLISTED,
        NotebookVisibility.PUBLIC,
    } or _is_owner(notebook, user)


def _authorize_create(notebook: Notebook, mode: SessionMode, current_user: User | None) -> None:
    if mode != "edit":
        if not _can_view(notebook, current_user):
            raise _not_found()
        return
    if current_user is None:
        raise _auth_error()
    if _is_owner(notebook, current_user):
        return
    if notebook.visibility == NotebookVisibility.DRAFT:
        raise _not_found()
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Notebook owner required")


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> SessionOut:
    """Start a notebook session and return its proxy URL."""
    notebook = await db.scalar(select(Notebook).where(Notebook.id == payload.notebook_id))
    if notebook is None:
        raise _not_found()

    _authorize_create(notebook, payload.mode, current_user)

    try:
        session = await manager.spawn(
            notebook, payload.mode, current_user.id if current_user is not None else None
        )
    except (SessionCapacityError, PortAllocationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return SessionOut(
        id=session.id,
        notebook_id=session.notebook_id,
        # A session spawned here is only ever "edit" or "run" (validated above).
        mode=cast("SessionMode", session.mode),
        proxy_url=str(request.url_for("proxy_http", session_id=session.id, path="")),
    )


async def _persist_edit_session(
    session: SessionInfo,
    manager: ProcessManager,
    db: AsyncSession,
    storage: NotebookStorageService,
) -> None:
    if session.mode != "edit":
        return
    source = manager.current_source(session.id)
    if source is None:
        return
    notebook = await db.scalar(select(Notebook).where(Notebook.id == session.notebook_id))
    if notebook is None:
        return
    await storage.put(notebook, source)
    await db.commit()


def _authorize_session(session: SessionInfo, current_user: User | None) -> None:
    if session.creator_id is None:
        return
    if current_user is None:
        raise _auth_error()
    if session.creator_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Session creator required"
        )


@router.post("/{session_id}/save", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def save_session(
    session_id: UUID,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> Response:
    """Persist an edit session's current source without stopping it."""
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    _authorize_session(session, current_user)
    await _persist_edit_session(session, manager, db, storage)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_session(
    session_id: UUID,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> Response:
    """Persist an edit session's source and stop the session."""
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    _authorize_session(session, current_user)
    await _persist_edit_session(session, manager, db, storage)

    try:
        await manager.stop(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
