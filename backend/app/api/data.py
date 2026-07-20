from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.core.errors import NotFoundError
from app.db.database import get_db
from app.models import NotebookData, User
from app.schemas import NotebookDataOut
from app.services.access import Action, load_notebook_for

router = APIRouter(prefix="/api/notebooks", tags=["data"])


@router.get("/{notebook_id}/data", response_model=NotebookDataOut)
async def get_latest_notebook_data(
    notebook_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User | None, Depends(get_current_user_optional)],
) -> NotebookDataOut:
    """Return the most recently stored data payload for a notebook the caller can read."""
    await load_notebook_for(db, notebook_id, actor, Action.READ)
    data = await db.scalar(
        select(NotebookData)
        .where(NotebookData.notebook_id == notebook_id)
        .order_by(NotebookData.created_at.desc(), NotebookData.id.desc())
        .limit(1)
    )
    if data is None:
        raise NotFoundError("Notebook data not found")
    return NotebookDataOut.model_validate(data, from_attributes=True)
