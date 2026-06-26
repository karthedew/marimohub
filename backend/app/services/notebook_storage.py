from abc import ABC, abstractmethod
from typing import override
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notebook


class NotebookStorageService(ABC):
    """Abstract store for a notebook's source code."""

    @abstractmethod
    async def get(self, notebook: Notebook) -> str | None:
        """Return the stored source for the notebook, if any."""
        raise NotImplementedError

    @abstractmethod
    async def put(self, notebook: Notebook, source: str | None) -> None:
        """Store the given source for the notebook."""
        raise NotImplementedError

    @abstractmethod
    async def delete(self, notebook_id: UUID) -> None:
        """Remove the stored source for the notebook."""
        raise NotImplementedError


class PostgresNotebookStorage(NotebookStorageService):
    """Notebook source storage backed by the notebook row itself."""

    def __init__(self, db: AsyncSession) -> None:
        """Bind the storage to an active database session."""
        super().__init__()
        self.db = db

    @override
    async def get(self, notebook: Notebook) -> str | None:
        """Return the notebook's stored source."""
        return notebook.source

    @override
    async def put(self, notebook: Notebook, source: str | None) -> None:
        """Persist the given source onto the notebook row."""
        notebook.source = source
        self.db.add(notebook)

    @override
    async def delete(self, notebook_id: UUID) -> None:
        """Clear the stored source for the notebook, if it exists."""
        notebook = await self.db.get(Notebook, notebook_id)
        if notebook is not None:
            notebook.source = None
            self.db.add(notebook)
