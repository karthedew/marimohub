from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models import Notebook, NotebookData
from app.schemas import NotebookDataCreated, NotebookDataOut

router = APIRouter(prefix="/api/notebooks", tags=["data"])


def _notebook_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notebook not found")


async def _ensure_notebook_exists(db: AsyncSession, notebook_id: UUID) -> None:
    exists = await db.scalar(select(Notebook.id).where(Notebook.id == notebook_id))
    if exists is None:
        raise _notebook_not_found()


@router.post("/{notebook_id}/data", response_model=NotebookDataCreated, status_code=status.HTTP_201_CREATED)
async def create_notebook_data(
    notebook_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: Annotated[Any, Body()],
    source: Annotated[str | None, Query(max_length=255)] = None,
) -> NotebookDataCreated:
    await _ensure_notebook_exists(db, notebook_id)
    data = NotebookData(notebook_id=notebook_id, payload=payload, source=source)
    db.add(data)
    await db.commit()
    await db.refresh(data)
    return NotebookDataCreated(id=data.id)


@router.get("/{notebook_id}/data", response_model=NotebookDataOut)
async def get_latest_notebook_data(
    notebook_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NotebookDataOut:
    await _ensure_notebook_exists(db, notebook_id)
    data = await db.scalar(
        select(NotebookData)
        .where(NotebookData.notebook_id == notebook_id)
        .order_by(NotebookData.created_at.desc(), NotebookData.id.desc())
        .limit(1)
    )
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notebook data not found")
    return NotebookDataOut.model_validate(data, from_attributes=True)
