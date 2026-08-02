import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

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
