from dataclasses import dataclass
from typing import cast

import httpx
import pytest

from app.core.config import Settings
from app.services.gitlab_import import GitLabImportError, import_gitlab_notebook

_MARIMO_SOURCE = "import marimo as mo\n\napp = mo.App()\n"


@dataclass
class _FakeSettings:
    """The three fields `import_gitlab_notebook` reads off `Settings`.

    A real `Settings()` also requires `DATABASE_URL`/`SECRET_KEY` and runs
    the full validator chain; this narrower stand-in keeps these tests
    focused on the import gate itself. `cast` tells the type checker this
    satisfies the `Settings` parameter, matching the fake-object convention
    used elsewhere in this test suite (e.g. test_notebooks.py's
    `MockAsyncClient`).
    """

    GITLAB_IMPORT_ENABLED: bool = True
    GITLAB_IMPORT_ALLOWED_HOSTS: list[str] | None = None
    GITLAB_IMPORT_PROXY_URL: str | None = None

    def __post_init__(self) -> None:
        if self.GITLAB_IMPORT_ALLOWED_HOSTS is None:
            self.GITLAB_IMPORT_ALLOWED_HOSTS = ["gitlab.example.com"]


def _settings(**overrides: object) -> Settings:
    # Every value passed in tests below is a valid _FakeSettings field value
    # at runtime; the generic **overrides: object can't line up with its
    # concrete field types for the type checker (same convention as
    # test_config.py's `_settings`).
    return cast("Settings", _FakeSettings(**overrides))  # ty: ignore[invalid-argument-type]


def _mock_client(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, httpx.Response]
) -> list[str]:
    """Route every `client.get(url)` in `responses` through a fake, recording each url visited."""
    visited: list[str] = []

    class MockAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

        async def __aenter__(self) -> "MockAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
            visited.append(url)
            return responses[url]

    monkeypatch.setattr("app.services.gitlab_import.httpx.AsyncClient", MockAsyncClient)
    return visited


def _response(
    url: str, status_code: int = 200, *, content: str | bytes = "", location: str | None = None
) -> httpx.Response:
    headers = {"location": location} if location else None
    return httpx.Response(
        status_code, headers=headers, content=content, request=httpx.Request("GET", url)
    )


@pytest.mark.asyncio
async def test_import_disabled_by_default_rejects_before_any_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visited = _mock_client(monkeypatch, {})

    with pytest.raises(GitLabImportError, match="disabled"):
        await import_gitlab_notebook(
            "https://gitlab.example.com/x.py", settings=_settings(GITLAB_IMPORT_ENABLED=False)
        )

    assert visited == []


@pytest.mark.asyncio
async def test_import_rejects_non_https_url(monkeypatch: pytest.MonkeyPatch) -> None:
    visited = _mock_client(monkeypatch, {})

    with pytest.raises(GitLabImportError, match="https"):
        await import_gitlab_notebook("http://gitlab.example.com/x.py", settings=_settings())

    assert visited == []


@pytest.mark.asyncio
async def test_import_rejects_host_outside_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    visited = _mock_client(monkeypatch, {})

    with pytest.raises(GitLabImportError, match="allowlist"):
        await import_gitlab_notebook(
            "https://evil.example.com/x.py",
            settings=_settings(GITLAB_IMPORT_ALLOWED_HOSTS=["gitlab.example.com"]),
        )

    assert visited == []


@pytest.mark.asyncio
async def test_import_rejects_loopback_address(monkeypatch: pytest.MonkeyPatch) -> None:
    visited = _mock_client(monkeypatch, {})

    with pytest.raises(GitLabImportError, match="disallowed address"):
        await import_gitlab_notebook(
            "https://localhost/x.py", settings=_settings(GITLAB_IMPORT_ALLOWED_HOSTS=["localhost"])
        )

    assert visited == []


@pytest.mark.asyncio
async def test_import_follows_an_allowlisted_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    start = "https://gitlab.example.com/x.py"
    target = "https://gitlab.example.com/raw/x.py"
    visited = _mock_client(
        monkeypatch,
        {
            start: _response(start, 302, location=target),
            target: _response(target, 200, content=_MARIMO_SOURCE),
        },
    )

    result = await import_gitlab_notebook(start, settings=_settings())

    assert visited == [start, target]
    assert result.source == _MARIMO_SOURCE


@pytest.mark.asyncio
async def test_import_rejects_a_redirect_to_a_disallowed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = "https://gitlab.example.com/x.py"
    evil = "https://evil.example.com/x.py"
    _mock_client(
        monkeypatch,
        {start: _response(start, 302, location=evil)},
    )

    with pytest.raises(GitLabImportError, match="allowlist"):
        await import_gitlab_notebook(start, settings=_settings())


@pytest.mark.asyncio
async def test_import_bounds_redirect_chains(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {}
    for hop in range(10):
        url = f"https://gitlab.example.com/{hop}.py"
        next_url = f"https://gitlab.example.com/{hop + 1}.py"
        responses[url] = _response(url, 302, location=next_url)
    _mock_client(monkeypatch, responses)

    with pytest.raises(GitLabImportError, match="redirected too many times"):
        await import_gitlab_notebook("https://gitlab.example.com/0.py", settings=_settings())
