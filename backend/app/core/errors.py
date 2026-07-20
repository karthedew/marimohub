import logging

from fastapi import FastAPI, Request, status as http_status
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.errors")


class DomainError(Exception):
    """Base for every error the HTTP/WS boundary renders.

    ``str(self)`` is the internal, loggable message; ``self.detail`` is the
    client-safe response body. Subclasses set ``status`` (and, on the gateway
    path, ``ws_close_code``) as class attributes; ``GitLabImportError`` overrides
    ``status`` per instance.
    """

    # Assigning the class attribute `status` here would shadow the `status`
    # module for the rest of this class body, so the import is aliased.
    status: int = http_status.HTTP_500_INTERNAL_SERVER_ERROR
    ws_close_code: int = http_status.WS_1011_INTERNAL_ERROR

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        """Store the internal message and derive the client-safe detail."""
        super().__init__(message)
        self.detail = detail if detail is not None else message


class ConflictError(DomainError):
    """A domain-state conflict (never an access decision)."""

    status = http_status.HTTP_409_CONFLICT


class NotFoundError(DomainError):
    """A resource that is genuinely absent (not existence-hiding)."""

    status = http_status.HTTP_404_NOT_FOUND


class Unauthenticated(DomainError):  # noqa: N818 -- named for the auth failure, not "Error" noise
    """No or invalid credentials were presented (login, token decode)."""

    status = http_status.HTTP_401_UNAUTHORIZED


def register_error_handlers(app: FastAPI) -> None:
    """Install the single `DomainError` renderer and the catch-all 500 handler."""

    @app.exception_handler(DomainError)
    async def _render(request: Request, exc: DomainError) -> JSONResponse:
        if exc.status >= http_status.HTTP_500_INTERNAL_SERVER_ERROR:
            # Internal message only (str(exc)), never the client detail; no stack
            # trace — these are expected operational states, not bugs.
            logger.warning("%s %s -> %d: %s", request.method, request.url.path, exc.status, exc)
        headers = (
            {"WWW-Authenticate": "Bearer"}
            if exc.status == http_status.HTTP_401_UNAUTHORIZED
            else None
        )
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail}, headers=headers)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
