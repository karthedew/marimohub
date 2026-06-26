from uuid import UUID

from pydantic import BaseModel

from app.services.process_manager import SessionMode


class SessionCreate(BaseModel):
    notebook_id: UUID
    mode: SessionMode


class SessionOut(BaseModel):
    id: UUID
    notebook_id: UUID
    mode: SessionMode
    proxy_url: str
