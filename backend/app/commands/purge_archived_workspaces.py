"""Scheduler-agnostic CLI entrypoint for the workspace purge sweep.

Run periodically (cron, k8s CronJob, ...) via:

    uv run python -m app.commands.purge_archived_workspaces
"""

import asyncio
from datetime import UTC, datetime
import logging

from app.db.database import get_sessionmaker
from app.services.session_manager import get_session_manager
from app.services.workspace_service import purge_due_workspaces

logger = logging.getLogger("app.commands.purge_archived_workspaces")


async def _run() -> int:
    manager = get_session_manager()
    async with get_sessionmaker()() as db:
        return await purge_due_workspaces(db, manager, datetime.now(UTC))


def main() -> None:
    """Run the purge sweep once and log how many workspaces were removed."""
    logging.basicConfig(level=logging.INFO)
    count = asyncio.run(_run())
    logger.info("purged %d workspace(s) past their purge deadline", count)


if __name__ == "__main__":
    main()
