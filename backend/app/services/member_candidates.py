"""Person search: find the Users an Owner may add to a Workspace.

Registration is open, so anyone can become an Owner and run this search; it
must resolve a person the caller has in mind to a user id without becoming a
directory that lists everyone's email address (docs/adr/0004):

- A name search matches usernames and Display Names only, never emails,
  and never finds anyone whose username or Display Name holds an address.
- An email search matches one whole address, never a prefix or a domain.
- Only an email search, whose caller already typed the address, returns it
  in full; every other result carries a masked hint ("k•••@example.com").

Read-only and request-scoped like `services/access.py`: it takes the caller's
`AsyncSession` and never commits. Who may search is the route's decision.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, WorkspaceMember
from app.services.auth_service import normalize_email
from app.services.identity_text import has_control_character

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 255
MAX_CANDIDATES = 10
EMAIL_MASK = "•••"
_LIKE_ESCAPE = "\\"  # PostgreSQL's default LIKE escape character, made explicit
_HOLDS_AN_ADDRESS = "%@%"


@dataclass(frozen=True, slots=True)
class MemberCandidate:
    """A User who is not yet a member of the Workspace, as much as the search reveals."""

    user_id: UUID
    username: str
    display_name: str | None
    email_hint: str  # the full email only for an exact email search, else `mask_email`


async def find_member_candidates(
    db: AsyncSession, workspace_id: UUID, query: str
) -> list[MemberCandidate]:
    """Return up to `MAX_CANDIDATES` non-members of ``workspace_id`` that ``query`` finds.

    ``query`` is trimmed, and must then be `MIN_QUERY_LENGTH` to
    `MAX_QUERY_LENGTH` characters without a control character (else
    `ValueError`). The first rule that applies decides what it matches:

    1. It parses as a UUID: the user with that id.
    2. It contains "@": the user whose email is exactly ``query``, ignoring
       case. A partial or domain-only address matches nobody.
    3. Otherwise: users whose username or Display Name contains ``query``,
       ignoring case, with LIKE's wildcards in ``query`` matching only
       themselves. Ranked exact username, then username prefix, then
       Display Name prefix, then any other match. A user whose username or
       Display Name contains "@" never matches: it may be an email address,
       which a name search must not show.

    Every rule excludes the Workspace's existing members, and ties are
    broken by username.
    """
    text = query.strip()
    if not MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH:
        raise ValueError(
            f"a person search needs {MIN_QUERY_LENGTH} to {MAX_QUERY_LENGTH} characters"
        )
    if has_control_character(text):  # e.g. NUL, which PostgreSQL cannot even compare
        raise ValueError("a person search must not contain control characters")
    is_member = (
        select(WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == User.id)
        .exists()
    )
    statement = select(User.id, User.username, User.display_name, User.email).where(~is_member)
    reveals_email = False
    user_id = _as_uuid(text)
    if user_id is not None:
        statement = statement.where(User.id == user_id)
    elif "@" in text:
        # Emails are stored canonical, so this is an exact, index-backed match.
        statement = statement.where(User.email == normalize_email(text))
        reveals_email = True  # the caller already knows this whole address
    else:
        matches, rank = _name_match(text)
        statement = statement.where(matches).order_by(rank)
    rows = await db.execute(statement.order_by(User.username).limit(MAX_CANDIDATES))
    return [
        MemberCandidate(
            user_id=row.id,
            username=row.username,
            display_name=row.display_name,
            email_hint=row.email if reveals_email else mask_email(row.email),
        )
        for row in rows
    ]


def mask_email(email: str) -> str:
    """Return the first character of ``email``'s local part, `EMAIL_MASK`, "@" and its domain.

    "kate@example.com" becomes "k•••@example.com": enough to tell two people
    with the same name apart, without revealing the address. The domain
    follows the last "@"; a value with no "@" keeps only its first character.
    """
    local, at, domain = email.rpartition("@")
    if not at:
        return f"{email[:1]}{EMAIL_MASK}"
    return f"{local[:1]}{EMAIL_MASK}@{domain}"


def escape_like(text: str) -> str:
    """Escape LIKE's ``%``, ``_`` and escape character, so ``text`` matches only itself."""
    return (
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


def _as_uuid(text: str) -> UUID | None:
    try:
        return UUID(text)
    except ValueError:
        return None


def _name_match(text: str) -> tuple[ColumnElement[bool], ColumnElement[int]]:
    """Match ``text`` within a username or Display Name, and rank the matches (lower first).

    Leaves out anyone whose username or Display Name contains "@": an
    account registered with its address as username, or named by an identity
    provider that sends the address as the name, would otherwise show that
    address, in full, to a search for a fragment of its domain.
    """
    literal = escape_like(text)
    contains, prefix = f"%{literal}%", f"{literal}%"
    matches = and_(
        or_(
            User.username.ilike(contains, escape=_LIKE_ESCAPE),
            User.display_name.ilike(contains, escape=_LIKE_ESCAPE),
        ),
        User.username.not_like(_HOLDS_AN_ADDRESS),
        func.coalesce(User.display_name, "").not_like(_HOLDS_AN_ADDRESS),
    )
    rank = case(
        (User.username.ilike(literal, escape=_LIKE_ESCAPE), 0),
        (User.username.ilike(prefix, escape=_LIKE_ESCAPE), 1),
        (User.display_name.ilike(prefix, escape=_LIKE_ESCAPE), 2),
        else_=3,
    )
    return matches, rank
