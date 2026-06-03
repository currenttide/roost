"""Logic for `roost up` — the zero-to-running single-node on-ramp.

`roost up` takes a fresh machine from nothing to a working fleet-of-one:

  1. ensure a control plane is reachable (reuse one, or start `roost serve`),
  2. persist url + admin token so later `roost` commands work flag-free,
  3. enroll THIS machine as a worker and start it in the background,
  4. smoke-test that a worker registered and can run a trivial job,
  5. print the panel URL and next steps.

Everything here is best-effort and idempotent. The pure helpers (URL building,
config payload, panel URL) live here so they can be unit-tested without spawning
processes; the orchestration (`up`) is driven from cli.py.
"""

from __future__ import annotations

import os
import secrets
import socket
import time
from pathlib import Path
from typing import Any, Optional

import httpx

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def build_url(host: str, port: int) -> str:
    """Build the client-facing base URL for a (host, port) the CP is bound to.

    A server bound to 0.0.0.0 listens on all interfaces but is reached over
    loopback locally, so we advertise 127.0.0.1 rather than 0.0.0.0 (which is
    not a connectable address). Anything else is used verbatim.
    """
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host
    return f"http://{connect_host}:{port}"


def panel_url(url: str, token: str) -> str:
    """The live dashboard URL with the admin token baked in."""
    base = url.rstrip("/")
    if token:
        return f"{base}/panel?token={token}"
    return f"{base}/panel"


def is_loopback(host: str) -> bool:
    """True if binding to `host` keeps the CP on the local machine only.

    The server refuses to run unauthenticated on a non-loopback bind, so `up`
    uses this to decide whether it must mint a token before serving.
    """
    return host in _LOOPBACK_HOSTS


def config_payload(url: str, token: str, worker_id: Optional[str] = None,
                   name: Optional[str] = None) -> dict[str, Any]:
    """The config.toml mapping `roost up` persists so later commands need no flags.

    `credential` is the key the resolver (config.resolve_url_token) reads as the
    bearer token, so storing the admin token there makes `roost workers`/`roost do`
    authenticate automatically. We merge on top of any existing config in cli.py.
    """
    payload: dict[str, Any] = {"url": url}
    if token:
        payload["credential"] = token
    if worker_id:
        payload["worker_id"] = worker_id
    if name:
        payload["name"] = name
    return payload


def env_file_text(url: str, token: str) -> str:
    """Shell-sourceable ~/.roost/env contents (a convenience alongside the toml)."""
    return (
        f"export ROOST_URL={url}\n"
        f"export ROOST_TOKEN={token}\n"
    )


def write_env_file(url: str, token: str, path: Optional[Path] = None) -> Path:
    """Write ~/.roost/env (0600) so `source` makes ROOST_URL/ROOST_TOKEN available.

    Honors ROOST_HOME for test isolation; defaults to ~/.roost/env.
    """
    if path is None:
        base = os.environ.get("ROOST_HOME")
        root = Path(base) if base else (Path.home() / ".roost")
        path = root / "env"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, env_file_text(url, token).encode("utf-8"))
    finally:
        os.close(fd)
    return path


def gen_admin_token() -> str:
    """A fresh admin/shared token for a newly-started control plane."""
    return secrets.token_hex(16)


def default_worker_name() -> str:
    return socket.gethostname()


def ping_ok(url: str, token: str = "", timeout: float = 3.0) -> bool:
    """True if a control plane answers /healthz at `url` (reuse instead of start)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(base_url=url.rstrip("/"), headers=headers,
                          timeout=timeout) as c:
            r = c.get("/healthz")
        return r.status_code < 400
    except httpx.HTTPError:
        return False


def wait_for_health(url: str, token: str = "", timeout: float = 15.0,
                    interval: float = 0.3) -> bool:
    """Poll /healthz until the CP responds or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ping_ok(url, token, timeout=interval + 1.0):
            return True
        time.sleep(interval)
    return False


def wait_for_worker(url: str, token: str, worker_id: Optional[str] = None,
                    timeout: float = 15.0, interval: float = 0.5) -> Optional[dict]:
    """Poll /workers until the (optionally specific) worker appears online.

    Returns the worker dict once it registers, else None on timeout.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with httpx.Client(base_url=url.rstrip("/"), headers=headers,
                              timeout=5.0) as c:
                r = c.get("/workers")
            if r.status_code < 400:
                workers = r.json()
                for w in workers:
                    if worker_id and w.get("id") != worker_id:
                        continue
                    if w.get("status") in ("idle", "busy"):
                        return w
        except httpx.HTTPError:
            pass
        time.sleep(interval)
    return None
