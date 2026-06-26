# Marimo Process Spike

Installed version: `marimo==0.23.10` via `uv sync` from `backend/pyproject.toml`.

## CLI flags verified

Commands run:

```sh
uv run marimo --version
uv run marimo edit --help
uv run marimo run --help
```

`marimo edit` accepts:

- `-p, --port INTEGER`
- `--host TEXT` with default `127.0.0.1`
- `--headless`
- `--proxy TEXT`
- `--token / --no-token`; default is `--token`
- `--token-password TEXT`
- `--token-password-file TEXT`
- `--base-url TEXT`
- `--allow-origins TEXT`
- `--skip-update-check`
- `--sandbox / --no-sandbox`
- `--trusted / --untrusted`
- `--watch`
- `--skew-protection / --no-skew-protection`; default is `--skew-protection`
- `--timeout FLOAT`; global shutdown timeout in minutes with no connection
- `--session-ttl INTEGER`

`marimo run` accepts:

- `-p, --port INTEGER`
- `--host TEXT` with default `127.0.0.1`
- `--headless`
- `--proxy TEXT`
- `--token / --no-token`; default is `--no-token`
- `--token-password TEXT`
- `--token-password-file TEXT`
- `--include-code`
- `--session-ttl INTEGER`; default is `120`
- `--watch`
- `--skew-protection / --no-skew-protection`; default is `--skew-protection`
- `--base-url TEXT`
- `--allow-origins TEXT`
- `--redirect-console-to-browser`
- `--sandbox / --no-sandbox`
- `--check / --no-check`; default is `--check`
- `--trusted / --untrusted`
- `--show-tracebacks / --no-show-tracebacks`

Both modes can be launched headless on a chosen host/port with explicit token auth:

```sh
marimo edit notebook.py --headless --host 127.0.0.1 --port <port> --base-url <base_url> --token-password <token> --skip-update-check
marimo run notebook.py --headless --host 127.0.0.1 --port <port> --base-url <base_url> --token-password <token>
```

Passing `--token-password` enables session auth and prints a URL containing `?access_token=<token>` for both modes. `run` defaults to `--no-token` unless token auth is requested.

## Readiness signal

The readiness probe polls `GET <base_url>/?access_token=<token>` on the chosen host/port until the server returns HTTP `200`. In the verified run, both `edit` and `run` returned HTTP `200` before shutdown.

Observed startup output:

```text
Edit spike_notebook.py in your browser
URL: http://localhost:<port>?access_token=molab-spike-token

Running spike_notebook.py
URL: http://localhost:<port>?access_token=molab-spike-token
```

## Shutdown behavior

The spike sends `SIGTERM` through `subprocess.terminate()` after readiness succeeds. On macOS, both `edit` and `run` exited within the 10 second graceful shutdown window with return code `-15`. No extra child-process cleanup was needed for the verified minimal notebook.

Verification command:

```sh
uv run python spikes/marimo_process_spike.py
```

Result: both modes started, returned HTTP `200`, and stopped cleanly with exit code `-15`.
