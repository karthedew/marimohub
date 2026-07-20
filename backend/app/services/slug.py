import re

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

DNS_LABEL_MAX_LENGTH = 63
DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")  # RFC-1123 label


def to_dns_label(name: str) -> str:
    """Sanitise arbitrary text into a lowercase RFC-1123 DNS label, <=63 chars."""
    label = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (label or "resource")[:DNS_LABEL_MAX_LENGTH].rstrip("-") or "resource"


async def unique_slug(
    db: AsyncSession,
    base: str,
    column: InstrumentedAttribute[str],
    *,
    max_length: int = DNS_LABEL_MAX_LENGTH,
    exclude: ColumnElement[bool] | None = None,
) -> str:
    """Return `base`, or `base` suffixed `-2`, `-3`, ... until no row has `column == candidate`.

    `column` names the slugged table to probe (e.g. `Workspace.slug`,
    `Deployment.slug`); `exclude` narrows the collision check to ignore a row's
    own current value on update (e.g. a deployment being re-slugged).
    """
    candidate = base
    suffix = 2
    while True:
        clause = column == candidate
        taken = await db.scalar(
            select(column).where(clause if exclude is None else and_(clause, exclude)).limit(1)
        )
        if taken is None:
            return candidate
        suffix_text = f"-{suffix}"
        candidate = f"{base[: max_length - len(suffix_text)].rstrip('-')}{suffix_text}"
        suffix += 1
