import ast
from dataclasses import dataclass
from http import HTTPStatus
import ipaddress
from pathlib import PurePosixPath
import socket
from urllib.parse import unquote, urlparse

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import DomainError

MAX_IMPORT_BYTES = 1_000_000

# Bounds manual redirect-following (disabled on the httpx client itself so
# every hop can be re-validated against the allowlist/SSRF checks below
# instead of being fetched blind).
_MAX_REDIRECTS = 5


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


def _require_https_host(url: str) -> str:
    """Return `url`'s hostname, rejecting anything but an explicit `https://` scheme."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise GitLabImportError(422, "Notebook URL must use https://")
    if not parsed.hostname:
        raise GitLabImportError(422, "Notebook URL is missing a host")
    return parsed.hostname


def _require_allowed_host(hostname: str, allowed_hosts: list[str]) -> None:
    if hostname.lower() not in {allowed.lower() for allowed in allowed_hosts}:
        raise GitLabImportError(
            403, f"Host {hostname!r} is not on the configured GitLab import allowlist"
        )


def _require_public_address(hostname: str) -> None:
    """Reject a hostname that resolves to a private/loopback/link-local/reserved address.

    A host that fails to resolve at all is not blocked here: the allowlist
    already bounds which hostnames are acceptable, and letting resolution
    failure surface from the actual fetch keeps this check from needing a
    working resolver of its own to be exercised in tests.
    """
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except OSError:
        return
    for family_info in addresses:
        raw_address = family_info[4][0]
        ip = ipaddress.ip_address(raw_address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise GitLabImportError(403, f"Host {hostname!r} resolves to a disallowed address")


def _validate_target(url: str, allowed_hosts: list[str]) -> None:
    """Apply the full SSRF gate (HTTPS-only, allowlisted host, non-private address) to `url`.

    Called on the initial URL and again on every redirect hop: a redirect
    that lands on a disallowed host or a private address is rejected exactly
    like a direct request to it would be.
    """
    hostname = _require_https_host(url)
    _require_allowed_host(hostname, allowed_hosts)
    _require_public_address(hostname)


async def _fetch_validated(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None, allowed_hosts: list[str]
) -> httpx.Response:
    """GET `url`, manually validating and following redirects up to `_MAX_REDIRECTS` hops."""
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        _validate_target(current_url, allowed_hosts)
        response = await client.get(current_url, headers=headers)
        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        if not location:
            raise GitLabImportError(502, "Upstream redirect is missing a Location header")
        current_url = str(response.url.join(location))
    raise GitLabImportError(502, "Upstream redirected too many times while fetching notebook")


async def import_gitlab_notebook(
    url: str, pat: str | None = None, *, settings: Settings | None = None
) -> ImportedNotebook:
    """Fetch and validate a marimo notebook from an administrator-allowlisted GitLab URL.

    Fails closed: import is rejected outright unless an administrator has
    both enabled it and configured a non-empty host allowlist (see
    `Settings.GITLAB_IMPORT_ENABLED`/`GITLAB_IMPORT_ALLOWED_HOSTS`). Every
    request -- the initial URL and each redirect hop -- is re-validated
    against that allowlist plus an HTTPS-only, non-private-address check
    before it is fetched, through an optional administrator-supplied egress
    proxy when one is configured.
    """
    settings = settings if settings is not None else get_settings()
    if not settings.GITLAB_IMPORT_ENABLED:
        raise GitLabImportError(403, "GitLab import is disabled")
    allowed_hosts = settings.GITLAB_IMPORT_ALLOWED_HOSTS
    if not allowed_hosts:
        raise GitLabImportError(403, "GitLab import has no configured host allowlist")

    headers = {"PRIVATE-TOKEN": pat} if pat else None
    proxy = str(settings.GITLAB_IMPORT_PROXY_URL) if settings.GITLAB_IMPORT_PROXY_URL else None

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False, proxy=proxy) as client:
            response = await _fetch_validated(client, url, headers, allowed_hosts)
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
