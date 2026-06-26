import ast
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import httpx


MAX_IMPORT_BYTES = 1_000_000


class GitLabImportError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ImportedNotebook:
    title: str
    source: str


def _title_from_url(url: str) -> str:
    filename = PurePosixPath(unquote(urlparse(url).path)).name
    if not filename:
        return "Imported notebook"
    if filename.endswith(".py"):
        return filename[:-3] or filename
    return filename


def _has_marimo_import(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "marimo" or alias.name.startswith("marimo.") for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "marimo" or node.module.startswith("marimo."):
                return True
    return False


def _validate_source(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise GitLabImportError(422, "Imported file is not valid Python") from exc
    if not _has_marimo_import(tree):
        raise GitLabImportError(422, "Imported file is not a marimo notebook")


async def import_gitlab_notebook(url: str, pat: str | None = None) -> ImportedNotebook:
    headers = {"PRIVATE-TOKEN": pat} if pat else None
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise GitLabImportError(502, "Unable to fetch notebook from upstream") from exc

    if response.status_code in {401, 403, 404}:
        raise GitLabImportError(response.status_code, f"Upstream returned {response.status_code} while fetching notebook")
    if response.status_code >= 400:
        raise GitLabImportError(502, "Upstream failed while fetching notebook")
    if len(response.content) > MAX_IMPORT_BYTES:
        raise GitLabImportError(413, "Imported notebook exceeds the 1 MB size limit")

    source = response.text
    _validate_source(source)
    return ImportedNotebook(title=_title_from_url(str(response.url)), source=source)
