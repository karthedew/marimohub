import asyncio
from collections.abc import Generator, Mapping
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
import pytest
from starlette.types import Receive, Scope, Send
import uvicorn

from app import serve
from app.serve import _app_import_string, _certificate_fingerprint, watch_certificates


def _write_self_signed_cert(cert_path: Path, key_path: Path, common_name: str) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def test_app_import_string_defaults_to_public() -> None:
    assert _app_import_string({}) == "app.main:app"


def test_app_import_string_selects_internal() -> None:
    assert _app_import_string({"MARIMOHUB_APP": "internal"}) == "app.internal_main:app"


def test_app_import_string_rejects_unknown_selection() -> None:
    with pytest.raises(ValueError, match="MARIMOHUB_APP"):
        _app_import_string({"MARIMOHUB_APP": "bogus"})


def test_certificate_fingerprint_none_when_files_missing(tmp_path: Path) -> None:
    assert (
        _certificate_fingerprint(str(tmp_path / "missing.crt"), str(tmp_path / "missing.key"))
        is None
    )


def test_certificate_fingerprint_changes_after_rewrite(tmp_path: Path) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    _write_self_signed_cert(cert_path, key_path, "first")
    first = _certificate_fingerprint(str(cert_path), str(key_path))

    _write_self_signed_cert(cert_path, key_path, "second")
    second = _certificate_fingerprint(str(cert_path), str(key_path))

    assert first is not None
    assert second is not None
    assert first != second


@pytest.mark.asyncio
async def test_watch_certificates_reloads_context_on_rotation(tmp_path: Path) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    _write_self_signed_cert(cert_path, key_path, "original")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))

    reload_calls: list[str] = []
    original_load_cert_chain = context.load_cert_chain

    def _recording_load_cert_chain(
        certfile: str, keyfile: str | None = None, password: str | None = None
    ) -> None:
        reload_calls.append(certfile)
        original_load_cert_chain(certfile, keyfile, password)

    context.load_cert_chain = _recording_load_cert_chain  # ty: ignore[invalid-assignment]

    done = False

    def is_done() -> bool:
        return done

    watcher = asyncio.create_task(
        watch_certificates(context, str(cert_path), str(key_path), 0.02, is_done)
    )
    await asyncio.sleep(0.05)
    _write_self_signed_cert(cert_path, key_path, "rotated")
    await asyncio.sleep(0.2)
    done = True
    await watcher

    assert reload_calls == [str(cert_path)]


@pytest.mark.asyncio
async def test_watch_certificates_stops_once_is_done_returns_true(tmp_path: Path) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    _write_self_signed_cert(cert_path, key_path, "only")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))

    await asyncio.wait_for(
        watch_certificates(context, str(cert_path), str(key_path), 0.01, lambda: True),
        timeout=1,
    )


_URL_LOGGERS = ("httpx", "httpcore")


@pytest.fixture
def _unconfigured_url_loggers() -> Generator[None, None, None]:
    """Start from library defaults and put back whatever levels were set before."""
    loggers = [logging.getLogger(name) for name in _URL_LOGGERS]
    previous = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.NOTSET)
    yield
    for logger, level in zip(loggers, previous, strict=True):
        logger.setLevel(level)


@pytest.mark.usefixtures("_unconfigured_url_loggers")
def test_main_keeps_runtime_access_tokens_out_of_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: the public backend logged every proxied URL, access token included.

    Seen live as `INFO:httpx:HTTP Request: GET http://msess-...:8080/...?access_token=...`.
    """
    token = "marimo-access-token-value"  # noqa: S105 -- a fake upstream token for the test

    async def _proxied_request(env: Mapping[str, str]) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(
                "http://msess-6f1c1d0e.marimohub-sessions.svc:8080/api/proxy/6f1c1d0e/",
                params={"access_token": token},
            )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_proxied_request({}))
    assert token in caplog.text  # sanity: unconfigured, httpx does log the full URL
    caplog.clear()

    monkeypatch.setattr(serve, "_main", _proxied_request)
    with caplog.at_level(logging.DEBUG):
        serve.main()

    assert token not in caplog.text


_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def _restore_uvicorn_loggers() -> Generator[None, None, None]:
    """Building a uvicorn `Config` reconfigures its loggers; put them back as they were."""
    loggers = [logging.getLogger(name) for name in _UVICORN_LOGGERS]
    saved = [
        (logger.handlers[:], logger.filters[:], logger.level, logger.propagate)
        for logger in loggers
    ]
    yield
    for logger, (handlers, filters, level, propagate) in zip(loggers, saved, strict=True):
        logger.handlers[:] = handlers
        logger.filters[:] = filters
        logger.setLevel(level)
        logger.propagate = propagate


async def _empty_ok(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer every HTTP request with an empty 200: a stand-in for the API."""
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


@pytest.mark.asyncio
@pytest.mark.usefixtures("_restore_uvicorn_loggers")
async def test_the_access_log_keeps_the_path_but_never_the_query_string(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: the person search's text, whole email addresses included, was logged.

    Seen as `INFO: ... "GET /api/workspaces/<id>/member-candidates?q=grace.hopper%40navy.example
    HTTP/1.1" 401 Unauthorized`, for every keystroke, even on refused requests.
    """
    # Built the way `_main` builds it, which applies serve's logging setup;
    # only the app behind it is a stand-in.
    config = serve._build_config("app.main:app", "127.0.0.1", 0, None, None, 1)
    config.app = _empty_ok
    config.lifespan = "off"
    server = uvicorn.Server(config)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/api/workspaces/ws/member-candidates"
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(500):  # up to 5 s
                if server.started:
                    break
                await asyncio.sleep(0.01)
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params={"q": "grace.hopper@navy.example"})
        finally:
            server.should_exit = True
            await serving

    access_log = capsys.readouterr().out
    assert response.status_code == 200
    assert '"GET /api/workspaces/ws/member-candidates HTTP/1.1" 200' in access_log
    assert "grace" not in access_log
    assert "?" not in access_log
