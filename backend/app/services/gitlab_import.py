import ast
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import httpx

from app.core.errors import DomainError

MAX_IMPORT_BYTES = 1_000_000


class GitLabImportError(DomainError):
    """Raised when importing a notebook from a URL fails.

    Unlike other domain errors, the HTTP status is genuinely dynamic (it
    mirrors whatever the upstream returned), so it rides on the instance.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        """Capture the HTTP status and client-safe detail for the failure."""
        super().__init__(detail, detail=detail)
        self.status = status_code


@dataclass(frozen=True)
class ImportedNotebook:
    """A notebook successfully fetched and validated from a URL."""

    title: str
    source: str


def _title_from_url(url: str) -> str:
    filename = PurePosixPath(unquote(urlparse(url).path)).name
    if not filename:
        return "Imported notebook"
    if filename.endswith(".py"):
        return filename[:-3] or filename
    return filename


def _is_marimo_module(name: str) -> bool:
    return name == "marimo" or name.startswith("marimo.")


def _imports_marimo(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(_is_marimo_module(alias.name) for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.module is not None and _is_marimo_module(node.module)
    return False


def _has_marimo_import(tree: ast.AST) -> bool:
    return any(_imports_marimo(node) for node in ast.walk(tree))


def _validate_source(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise GitLabImportError(422, "Imported file is not valid Python") from exc
    if not _has_marimo_import(tree):
        raise GitLabImportError(422, "Imported file is not a marimo notebook")


async def import_gitlab_notebook(url: str, pat: str | None = None) -> ImportedNotebook:
    """Fetch and validate a marimo notebook from a (GitLab) URL."""
    headers = {"PRIVATE-TOKEN": pat} if pat else None
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise GitLabImportError(502, "Unable to fetch notebook from upstream") from exc

    if response.status_code in {
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
    }:
        raise GitLabImportError(
            response.status_code,
            f"Upstream returned {response.status_code} while fetching notebook",
        )
    if response.status_code >= HTTPStatus.BAD_REQUEST:
        raise GitLabImportError(502, "Upstream failed while fetching notebook")
    if len(response.content) > MAX_IMPORT_BYTES:
        raise GitLabImportError(413, "Imported notebook exceeds the 1 MB size limit")

    source = response.text
    _validate_source(source)
    return ImportedNotebook(title=_title_from_url(str(response.url)), source=source)
