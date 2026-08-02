"""Production entrypoint: runs `app.main` or `app.internal_main` under Uvicorn.

This is what the production image's `CMD` invokes -- `python -m app.serve` --
instead of the bare `uvicorn` CLI, for two reasons neither the CLI nor a
shell wrapper covers on its own:

1. One image, two Deployments. `MARIMOHUB_APP` selects which ASGI app to
   serve (`public` -> `app.main:app`, `internal` -> `app.internal_main:app`),
   so the exact same image runs as either the public or the internal
   backend, matching the split those two modules already implement.
2. Certificate rotation without dropping the process. When `TLS_CERT_FILE`/
   `TLS_KEY_FILE` are set, Uvicorn is started with a real `ssl.SSLContext`
   and a background task polls both files; on change it calls
   `SSLContext.load_cert_chain` again on that same context object. Every
   in-flight connection already completed its handshake against the old
   context and is untouched; every new connection accepted afterward
   handshakes against the updated one. No restart, no dropped socket.

Graceful shutdown on SIGTERM/SIGINT is Uvicorn's own built-in behavior
(`Server.serve()` stops accepting new connections and waits for in-flight
requests/WebSockets to finish, up to `GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`)
and is not reimplemented here.
"""

import asyncio
from collections.abc import Callable, Mapping
import contextlib
import logging
import os
import ssl

import uvicorn

logger = logging.getLogger("app.serve")

_APP_IMPORT_STRINGS = {
    "public": "app.main:app",
    "internal": "app.internal_main:app",
}

_DEFAULT_HOST = "0.0.0.0"  # noqa: S104 -- the production container's only interface; not a local-dev default.
_DEFAULT_PORT = 8000
_DEFAULT_TLS_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 30


def _app_import_string(env: Mapping[str, str]) -> str:
    """Resolve `MARIMOHUB_APP` to the Uvicorn import string it selects.

    Raises:
        ValueError: `MARIMOHUB_APP` is set to anything other than a known app.
    """
    selected = env.get("MARIMOHUB_APP", "public")
    try:
        return _APP_IMPORT_STRINGS[selected]
    except KeyError:
        known = ", ".join(sorted(_APP_IMPORT_STRINGS))
        raise ValueError(f"MARIMOHUB_APP must be one of: {known} (got {selected!r})") from None


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    """Read a positive float from the environment, falling back to `default`."""
    raw = env.get(name)
    return default if raw is None else float(raw)


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Read an int from the environment, falling back to `default`."""
    raw = env.get(name)
    return default if raw is None else int(raw)


def _certificate_fingerprint(certfile: str, keyfile: str) -> tuple[int, int, int, int] | None:
    """Return a cheap on-disk identity for the cert/key pair, or `None` if unreadable.

    Kubernetes projects a Secret volume as a symlink into a versioned
    `..data_<timestamp>` directory and swaps that symlink atomically on
    rotation. An inotify watch on the original file/inode does not survive
    that swap, so this polls `os.stat` on the resolved path instead of
    trying to keep a filesystem-event watch alive across it.
    """
    try:
        cert_stat = os.stat(certfile)
        key_stat = os.stat(keyfile)
    except OSError:
        return None
    return (cert_stat.st_mtime_ns, cert_stat.st_ino, key_stat.st_mtime_ns, key_stat.st_ino)


async def watch_certificates(
    context: ssl.SSLContext,
    certfile: str,
    keyfile: str,
    poll_interval_seconds: float,
    is_done: Callable[[], bool],
) -> None:
    """Reload `context` in place whenever `certfile`/`keyfile` change on disk.

    Runs until `is_done()` returns `True`. A failed reload (partially
    written file caught mid-swap, invalid PEM) is logged and left for the
    next poll rather than raised, so a bad rotation attempt never brings
    down a server that is still serving perfectly good, currently loaded
    certificates.
    """
    last = _certificate_fingerprint(certfile, keyfile)
    while not is_done():
        await asyncio.sleep(poll_interval_seconds)
        current = _certificate_fingerprint(certfile, keyfile)
        if current is None or current == last:
            continue
        try:
            context.load_cert_chain(certfile, keyfile)
        except ssl.SSLError:
            logger.exception(
                "certificate rotation failed to load %s; keeping prior certificate", certfile
            )
            continue
        last = current
        logger.info("reloaded rotated certificate from %s", certfile)


def _build_config(
    app_import_string: str,
    host: str,
    port: int,
    certfile: str | None,
    keyfile: str | None,
    graceful_timeout_seconds: int,
) -> uvicorn.Config:
    """Build the Uvicorn configuration for `app_import_string`."""
    return uvicorn.Config(
        app_import_string,
        host=host,
        port=port,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        timeout_graceful_shutdown=graceful_timeout_seconds,
        server_header=False,
        proxy_headers=False,
    )


async def _run(
    config: uvicorn.Config,
    certfile: str | None,
    keyfile: str | None,
    poll_interval_seconds: float,
) -> None:
    """Serve `config`'s app, watching for certificate rotation alongside it."""
    config.load()
    server = uvicorn.Server(config)

    watcher: asyncio.Task[None] | None = None
    if certfile is not None and keyfile is not None and config.ssl is not None:
        watcher = asyncio.create_task(
            watch_certificates(
                config.ssl, certfile, keyfile, poll_interval_seconds, lambda: server.should_exit
            )
        )

    try:
        await server.serve()
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher


async def _main(env: Mapping[str, str]) -> None:
    """Run the server `main()` starts, reading configuration from `env`."""
    app_import_string = _app_import_string(env)
    certfile = env.get("TLS_CERT_FILE")
    keyfile = env.get("TLS_KEY_FILE")
    config = _build_config(
        app_import_string,
        host=env.get("HOST", _DEFAULT_HOST),
        port=_env_int(env, "PORT", _DEFAULT_PORT),
        certfile=certfile,
        keyfile=keyfile,
        graceful_timeout_seconds=_env_int(
            env, "GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS", _DEFAULT_GRACEFUL_SHUTDOWN_SECONDS
        ),
    )
    poll_interval = _env_float(
        env, "TLS_CERT_POLL_INTERVAL_SECONDS", _DEFAULT_TLS_POLL_INTERVAL_SECONDS
    )
    await _run(config, certfile, keyfile, poll_interval)


def main() -> None:
    """Entrypoint for `python -m app.serve`."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main(os.environ))


if __name__ == "__main__":
    main()
