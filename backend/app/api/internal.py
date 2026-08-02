"""Runtime-authenticated routes: source delivery and Notebook data.

Every route here is reached only from inside the sessions namespace -- a
Runtime's source-fetcher init container or its own marimo process -- and is
authenticated by the Runtime's own `RUNTIME_CREDENTIAL`, never a user
session. `RuntimeBound` verifies the credential alone; `RuntimeNotebookBound`
additionally requires the verified Runtime be bound to the `notebook_id` in
the path, which is what lets `read_data`/`write_data` stay Notebook-addressed
while still being Runtime-authenticated.

The source-fetcher's contract for `read_runtime_source` (see
`images/source-fetcher` and the Pod builder's fetcher init container):

- `INTERNAL_API_URL` -- this app's base URL, injected by the chart.
- the Runtime's own id, taken from its own CR name / `$HOSTNAME`-derived
  identity, used to build the request path.
- `RUNTIME_CREDENTIAL`, read from a mounted Secret projection file and sent
  as ``Authorization: Bearer <credential>``.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, Unauthenticated
from app.db.database import get_db
from app.models import Deployment, DeploymentDesiredState, Notebook, NotebookData
from app.schemas import NotebookDataCreated, NotebookDataOut
from app.services.access import PermissionDenied
from app.services.notebook_storage import NotebookStorageService, get_notebook_storage
from app.services.runtime_credentials import (
    RuntimeCredentialVerifier,
    RuntimePrincipal,
    get_runtime_credential_verifier,
)

router = APIRouter(prefix="/api/internal", tags=["internal"])

# Distinct from `deps.oauth2_scheme`: this scheme resolves RUNTIME_CREDENTIALs,
# never user-facing access tokens.
runtime_bearer = HTTPBearer(auto_error=False)


async def _authenticate(
    credentials: HTTPAuthorizationCredentials | None, verifier: RuntimeCredentialVerifier
) -> RuntimePrincipal:
    if credentials is None:
        raise Unauthenticated("Missing runtime credential")
    return await verifier.verify(credentials.credentials)


async def require_runtime(
    runtime_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(runtime_bearer)],
    verifier: Annotated[RuntimeCredentialVerifier, Depends(get_runtime_credential_verifier)],
) -> RuntimePrincipal:
    """Verify the presented credential and require it name this exact Runtime.

    No DB or extra cluster I/O beyond `verifier.verify` itself: a credential
    that verifies but names a different Runtime than the path is rejected
    before anything else runs.
    """
    principal = await _authenticate(credentials, verifier)
    if principal.runtime_id != runtime_id:
        raise Unauthenticated("Runtime credential does not match this runtime")
    return principal


async def require_runtime_notebook(
    notebook_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(runtime_bearer)],
    verifier: Annotated[RuntimeCredentialVerifier, Depends(get_runtime_credential_verifier)],
) -> RuntimePrincipal:
    """Verify the presented credential and require it be bound to the path notebook."""
    principal = await _authenticate(credentials, verifier)
    if principal.notebook_id != notebook_id:
        raise PermissionDenied("Runtime credential not scoped to this notebook")
    return principal


RuntimeBound = Annotated[RuntimePrincipal, Depends(require_runtime)]
RuntimeNotebookBound = Annotated[RuntimePrincipal, Depends(require_runtime_notebook)]


async def _deploy_snapshot(db: AsyncSession, principal: RuntimePrincipal) -> str | None:
    """Return a deploy Runtime's exact deployed snapshot, or fail closed.

    A deploy Runtime's CR name is the Deployment id (see the Runtime
    Contract's naming table), so `principal.runtime_id` doubles as the
    Deployment primary key here. Wrong Notebook, stopped intent, a
    superseded revision, and a since-deleted row are all one
    indistinguishable "not found" -- the fetcher never learns which
    precondition failed, only that it must not proceed.
    """
    deployment = await db.get(Deployment, principal.runtime_id)
    if (
        deployment is None
        or deployment.notebook_id != principal.notebook_id
        or deployment.desired_state is not DeploymentDesiredState.ACTIVE
        or principal.deployment_revision != deployment.revision
    ):
        raise NotFoundError("Deployment not found")
    return deployment.source_snapshot


@router.get("/runtimes/{runtime_id}/source", response_class=PlainTextResponse)
async def read_runtime_source(
    principal: RuntimeBound,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
) -> PlainTextResponse:
    """Return the source this Runtime's fetcher should write to `/work/notebook.py`.

    A deploy Runtime always reads its immutable `Deployment.source_snapshot`
    bound to `spec.deploymentRevision`; edit/run read the Notebook's current
    source through `NotebookStorageService` instead, since neither mode runs
    against a frozen snapshot. Either way, no source yet is a valid empty
    document, not a 404 -- the fetcher's `--retry` depends on only a genuine
    authorization or binding failure producing a non-2xx.
    """
    if principal.mode == "deploy":
        source = await _deploy_snapshot(db, principal)
    else:
        notebook = await db.get(Notebook, principal.notebook_id)
        source = await storage.get(notebook) if notebook is not None else None
    return PlainTextResponse(source or "", media_type="text/x-python")


@router.post("/notebooks/{notebook_id}/data", response_model=NotebookDataCreated, status_code=201)
async def write_data(
    notebook_id: UUID,
    _: RuntimeNotebookBound,
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: Annotated[JsonValue, Body()],
    source: Annotated[str | None, Query(max_length=255)] = None,
) -> NotebookDataCreated:
    """Persist a data payload written by the Runtime's own marimo process."""
    if await db.get(Notebook, notebook_id) is None:
        raise NotFoundError("Notebook not found")
    data = NotebookData(notebook_id=notebook_id, payload=payload, source=source)
    db.add(data)
    await db.commit()
    await db.refresh(data)
    return NotebookDataCreated(id=data.id)


@router.get("/notebooks/{notebook_id}/data", response_model=NotebookDataOut)
async def read_data(
    notebook_id: UUID,
    _: RuntimeNotebookBound,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NotebookDataOut:
    """Return the notebook's latest data payload; the credential itself is the authorization."""
    data = await db.scalar(
        select(NotebookData)
        .where(NotebookData.notebook_id == notebook_id)
        .order_by(NotebookData.created_at.desc(), NotebookData.id.desc())
        .limit(1)
    )
    if data is None:
        raise NotFoundError("Notebook data not found")
    return NotebookDataOut.model_validate(data, from_attributes=True)
