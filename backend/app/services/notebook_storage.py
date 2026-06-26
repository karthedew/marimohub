from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notebook


class NotebookStorageService(ABC):
    @abstractmethod
    async def get(self, notebook: Notebook) -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def put(self, notebook: Notebook, source: str | None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete(self, notebook_id: UUID) -> None:
        raise NotImplementedError


class PostgresNotebookStorage(NotebookStorageService):
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, notebook: Notebook) -> str | None:
        return notebook.source

    async def put(self, notebook: Notebook, source: str | None) -> None:
        notebook.source = source
        self.db.add(notebook)

    async def delete(self, notebook_id: UUID) -> None:
        notebook = await self.db.get(Notebook, notebook_id)
        if notebook is not None:
            notebook.source = None
            self.db.add(notebook)
