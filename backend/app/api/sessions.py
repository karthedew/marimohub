from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.db.database import get_db
from app.models import Notebook, NotebookVisibility, User
from app.schemas import SessionCreate, SessionOut
from app.services.process_manager import (
    PortAllocationError,
    ProcessManager,
    SessionCapacityError,
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
    return notebook.visibility in {NotebookVisibility.UNLISTED, NotebookVisibility.PUBLIC} or _is_owner(notebook, user)


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> SessionOut:
    notebook = await db.scalar(select(Notebook).where(Notebook.id == payload.notebook_id))
    if notebook is None:
        raise _not_found()

    if payload.mode == "edit":
        if current_user is None:
            raise _auth_error()
        if not _is_owner(notebook, current_user):
            if notebook.visibility == NotebookVisibility.DRAFT:
                raise _not_found()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Notebook owner required")
    elif not _can_view(notebook, current_user):
        raise _not_found()

    try:
        session = await manager.spawn(notebook, payload.mode, current_user.id if current_user is not None else None)
    except (SessionCapacityError, PortAllocationError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    return SessionOut(
        id=session.id,
        notebook_id=session.notebook_id,
        mode=session.mode,
        proxy_url=str(request.url_for("proxy_http", session_id=session.id, path="")),
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_session(
    session_id: UUID,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> Response:
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if session.creator_id is None:
        try:
            await manager.stop(session_id)
        except SessionNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found") from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if current_user is None:
        raise _auth_error()
    if session.creator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Session creator required")

    try:
        await manager.stop(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
