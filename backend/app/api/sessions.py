from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.db.database import get_db
from app.models import User
from app.schemas import SessionCreate, SessionOut
from app.services.access import Action, load_notebook_for
from app.services.session_manager import (
    SessionManager,
    SessionMode,
    get_session_manager,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User | None, Depends(get_current_user_optional)],
    manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> SessionOut:
    """Start a notebook session and return its proxy URL."""
    action = Action.WRITE if payload.mode == "edit" else Action.READ
    notebook, _ = await load_notebook_for(db, payload.notebook_id, actor, action)

    session = await manager.spawn(notebook, payload.mode, actor.id if actor is not None else None)

    return SessionOut(
        id=session.id,
        notebook_id=session.notebook_id,
        # A session spawned here is only ever "edit" or "run" (validated above).
        mode=cast("SessionMode", session.mode),
        proxy_url=str(request.url_for("proxy_http", session_id=session.id, path="")),
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_session(
    session_id: UUID,
    manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> Response:
    """Stop a session; holding its id is the sole authorization to do so."""
    await manager.stop(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
