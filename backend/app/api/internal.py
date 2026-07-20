from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, Unauthenticated
from app.core.security import decode_session_token
from app.db.database import get_db
from app.models import Notebook, NotebookData
from app.schemas import NotebookDataCreated, NotebookDataOut
from app.services.access import PermissionDenied
from app.services.notebook_storage import NotebookStorageService, get_notebook_storage

router = APIRouter(prefix="/api/internal", tags=["internal"])

# Distinct from `deps.oauth2_scheme`: this scheme resolves SESSION_TOKENs, never user JWTs.
session_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    """The session pod identity carried by a verified SESSION_TOKEN."""

    session_id: UUID
    notebook_id: UUID


def require_session_notebook(
    notebook_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(session_bearer)],
) -> SessionPrincipal:
    """Validate the SESSION_TOKEN and require it be bound to the path notebook.

    Signature and claim check only, no DB or cluster I/O: a pod pointed at the
    wrong notebook is rejected before any query runs.
    """
    if credentials is None:
        raise Unauthenticated("Missing session token")
    decoded = decode_session_token(credentials.credentials)
    if decoded is None:
        raise Unauthenticated("Invalid session token")
    session_id, token_notebook_id = decoded
    if token_notebook_id != notebook_id:
        raise PermissionDenied("Session token not scoped to this notebook")
    return SessionPrincipal(session_id=session_id, notebook_id=notebook_id)


SessionBound = Annotated[SessionPrincipal, Depends(require_session_notebook)]


@router.get("/notebooks/{notebook_id}/source", response_class=PlainTextResponse)
async def read_source(
    notebook_id: UUID,
    _: SessionBound,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> PlainTextResponse:
    """Return the notebook's stored source for the init container to fetch.

    A notebook with no source yet (a fresh edit session) is a 200 with an
    empty body, never a 404 -- the init container's `--retry` depends on
    only genuine absence of the notebook producing a non-2xx.
    """
    notebook = await db.get(Notebook, notebook_id)
    if notebook is None:
        raise NotFoundError("Notebook not found")
    source = await storage.get(notebook)
    return PlainTextResponse(source or "", media_type="text/x-python")


@router.post("/notebooks/{notebook_id}/data", response_model=NotebookDataCreated, status_code=201)
async def write_data(
    notebook_id: UUID,
    _: SessionBound,
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: Annotated[JsonValue, Body()],
    source: Annotated[str | None, Query(max_length=255)] = None,
) -> NotebookDataCreated:
    """Persist a data payload written by the session pod's own runtime."""
    if await db.get(Notebook, notebook_id) is None:
        raise NotFoundError("Notebook not found")
    data = NotebookData(notebook_id=notebook_id, payload=payload, source=source)
    db.add(data)
    await db.commit()
    await db.refresh(data)
    return NotebookDataCreated(id=data.id)


@router.get("/notebooks/{notebook_id}/data", response_model=NotebookDataOut)
async def read_data(
    notebook_id: UUID,
    _: SessionBound,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NotebookDataOut:
    """Return the notebook's latest data payload; the token itself is the authorization."""
    data = await db.scalar(
        select(NotebookData)
        .where(NotebookData.notebook_id == notebook_id)
        .order_by(NotebookData.created_at.desc(), NotebookData.id.desc())
        .limit(1)
    )
    if data is None:
        raise NotFoundError("Notebook data not found")
    return NotebookDataOut.model_validate(data, from_attributes=True)
