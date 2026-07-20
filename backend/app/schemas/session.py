from uuid import UUID

from pydantic import BaseModel

from app.services.session_manager import SessionMode


class SessionCreate(BaseModel):
    """Request body for starting a new notebook session."""

    notebook_id: UUID
    mode: SessionMode


class SessionOut(BaseModel):
    """A started session and the URL the client should proxy through."""

    id: UUID
    notebook_id: UUID
    mode: SessionMode
    proxy_url: str
