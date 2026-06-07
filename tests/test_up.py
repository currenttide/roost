"""Tests for the `roost up` orchestration in cli.py (R16).

R9 covered bootstrap.py's pure helpers; this file covers the cli.py
orchestration that drives them — with every boundary mocked: bootstrap
pollers, process spawning (_spawn_detached), the supervised service install,
the worker-already-running probe, the enroll sub-command, and HTTP (via
httpx.MockTransport). ROOST_CONFIG_DIR/ROOST_HOME isolate all file writes
to tmp dirs. No process is ever spawned, no socket touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from roost import bootstrap, config as roost_config
from roost import cli as roost_cli
from roost import service as roost_service


class Boundaries:
    """Records every boundary the `up` orchestration crosses."""

    def __init__(self):
        self.spawns: list[list[str]] = []
        self.service_installs: list[bool] = []
        self.enrolls: list[dict] = []


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Boundaries:
    """Isolated config/home + all boundaries mocked to a recordable default:
    fresh CP comes up healthy, enroll succeeds, worker registers, smoke OK."""
    monkeypatch.setenv("ROOST_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ROOST_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ROOST_URL", raising=False)
    monkeypatch.delenv("ROOST_TOKEN", raising=False)
    b = Boundaries()

    monkeypatch.setattr(roost_cli, "_spawn_detached",
                        lambda args, log, extra_env=None: b.spawns.append(list(args)))
    monkeypatch.setattr(roost_cli, "_worker_already_running", lambda cfg_dir: False)
    monkeypatch.setattr(roost_cli, "_smoke_test", lambda url, token: True)
    monkeypatch.setattr(bootstrap, "ping_ok", lambda url, token="", **kw: False)
    monkeypatch.setattr(bootstrap, "wait_for_health",
                        lambda url, token="", **kw: True)
    monkeypatch.setattr(bootstrap, "wait_for_worker",
                        lambda url, token, worker_id=None, **kw:
                        {"id": worker_id or "w-up", "name": "this-box"})

    def fake_enroll(enroll_token=None, url_opt=None, **kw):
        # The real enroll writes worker identity into the same config.
        b.enrolls.append({"token": enroll_token, "url": url_opt})
        cfg = roost_config.load()
        cfg.update({"worker_id": "w-up", "credential": "cred-up"})
        roost_config.save(cfg)

    monkeypatch.setattr(roost_cli, "enroll", fake_enroll)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/enroll-tokens":
            return httpx.Response(200, json={"token": "et-1"})
        return httpx.Response(404, json={"detail": "unexpected"})

    real_client = roost_cli._client

    def fake_client(url, token):
        c = real_client(url, token)
        c._transport = httpx.MockTransport(handler)
        return c

    monkeypatch.setattr(roost_cli, "_client", fake_client)
    return b


def _run(args: list[str] = []):
    return CliRunner().invoke(roost_cli.cli, ["up", *args])


# ---------- happy paths ----------


def test_up_fresh_cp_full_path(env: Boundaries, tmp_path: Path):
    res = _run(["--port", "8888"])
    assert res.exit_code == 0, res.output
    # CP spawned with serve args incl. a minted token; worker spawned detached
    # (isolated ROOST_CONFIG_DIR ⇒ never the shared service unit).
    assert len(env.spawns) == 2
    serve = env.spawns[0]
    assert "serve" in serve and "--port" in serve and "8888" in serve
    token = serve[serve.index("--token") + 1]
    assert len(token) == 32  # minted admin token, not ""
    assert env.spawns[1][-1] == "worker"
    assert env.service_installs == []  # supervised path NOT used when isolated
    # enroll ran against the new CP
    assert env.enrolls and env.enrolls[0]["url"] == "http://127.0.0.1:8888"
    # config persisted; enroll then replaced the credential with the WORKER
    # credential (real enroll behavior) while the env file keeps the admin
    # token — the operator's `source ~/.roost/env` stays admin-capable.
    cfg = roost_config.load()
    assert cfg["url"] == "http://127.0.0.1:8888"
    assert cfg["credential"] == "cred-up"
    assert cfg["worker_id"] == "w-up"
    env_file = (tmp_path / "home" / "env").read_text()
    assert f"ROOST_TOKEN={token}" in env_file
    # operator messaging
    assert "smoke test passed" in res.output
    assert "Your Roost fleet is up." in res.output
    assert "/panel?token=" in res.output


def test_up_reuses_reachable_cp(env: Boundaries, monkeypatch):
    monkeypatch.setattr(bootstrap, "ping_ok", lambda url, token="", **kw: True)
    # already enrolled for this URL → no enroll, no CP spawn
    roost_config.save({"url": "http://127.0.0.1:8787", "credential": "adm",
                       "worker_id": "w-old"})
    res = _run()
    assert res.exit_code == 0, res.output
    assert "already reachable" in res.output
    assert "already enrolled as worker w-old" in res.output
    assert env.enrolls == []
    # only the worker spawn (no serve)
    assert [s for s in env.spawns if "serve" in s] == []


def test_up_skips_worker_spawn_when_one_is_serving(env: Boundaries, monkeypatch):
    monkeypatch.setattr(roost_cli, "_worker_already_running", lambda cfg_dir: True)
    res = _run()
    assert res.exit_code == 0, res.output
    assert "not starting another" in res.output
    assert [s for s in env.spawns if s and s[-1] == "worker"] == []


def test_up_supervised_service_when_not_isolated(env: Boundaries, monkeypatch):
    # No ROOST_CONFIG_DIR ⇒ default install ⇒ the durable service unit, NOT a
    # detached spawn. (Config still isolated: load/save are pointed at tmp by
    # patching the config module's dir resolution via ROOST_HOME-relative path.)
    monkeypatch.setattr(roost_service, "install",
                        lambda start: (env.service_installs.append(start)
                                       or (0, "unit at ~/.config")))
    monkeypatch.delenv("ROOST_CONFIG_DIR", raising=False)
    res = _run()
    assert res.exit_code == 0, res.output
    assert env.service_installs == [True]
    assert "worker service installed and started" in res.output
    assert [s for s in env.spawns if s and s[-1] == "worker"] == []


def test_up_service_failure_falls_back_to_detached(env: Boundaries, monkeypatch):
    monkeypatch.setattr(roost_service, "install", lambda start: (2, "no supervisor"))
    monkeypatch.delenv("ROOST_CONFIG_DIR", raising=False)
    res = _run()
    assert res.exit_code == 0, res.output
    assert "supervised service unavailable" in res.output
    assert env.spawns[-1][-1] == "worker"  # detached fallback


# ---------- failure paths ----------


def test_up_explicit_url_unreachable_fails_clearly(env: Boundaries):
    res = _run(["--url", "http://10.9.9.9:8787"])
    assert res.exit_code != 0
    assert "no control plane reachable" in res.output
    assert env.spawns == []  # never silently starts a different CP


def test_up_cp_never_healthy(env: Boundaries, monkeypatch):
    monkeypatch.setattr(bootstrap, "wait_for_health",
                        lambda url, token="", **kw: False)
    res = _run(["--port", "8899"])
    assert res.exit_code != 0
    assert "did not come up within 15s" in res.output
    assert "8899" in res.output  # tells the operator which port to suspect


def test_up_enroll_token_403(env: Boundaries, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "admin auth required"})

    monkeypatch.setattr(
        roost_cli, "_client",
        lambda url, token: httpx.Client(base_url=url,
                                        transport=httpx.MockTransport(handler)))
    res = _run()
    assert res.exit_code != 0
    assert "admin auth required to mint an enroll token" in res.output


def test_up_worker_never_registers(env: Boundaries, monkeypatch):
    monkeypatch.setattr(bootstrap, "wait_for_worker",
                        lambda url, token, worker_id=None, **kw: None)
    res = _run()
    assert res.exit_code != 0
    assert "worker did not register within 20s" in res.output
    assert "re-run `roost up`" in res.output


def test_up_smoke_failure_warns_but_exits_zero(env: Boundaries, monkeypatch):
    monkeypatch.setattr(roost_cli, "_smoke_test", lambda url, token: False)
    res = _run()
    assert res.exit_code == 0, res.output  # fleet IS up; warn, don't abort
    assert "WARNING" in res.output
    assert "smoke-test job did not" in res.output


# ---------- small helpers used by `up` ----------


def test_roost_argv_prefers_path_binary(monkeypatch):
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda name: "/usr/bin/roost")
    assert roost_cli._roost_argv() == ["/usr/bin/roost"]
    monkeypatch.setattr(_sh, "which", lambda name: None)
    argv = roost_cli._roost_argv()
    assert argv[-2:] == ["-m", "roost.cli"]


def test_smoke_test_against_stubbed_cp(monkeypatch):
    states = iter(["queued", "running", "succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "j1", "state": "queued"})
        return httpx.Response(200, json={"id": "j1", "state": next(states)})

    monkeypatch.setattr(
        roost_cli, "_client",
        lambda url, token: httpx.Client(base_url=url,
                                        transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(roost_cli.time, "sleep", lambda s: None)
    assert roost_cli._smoke_test("http://cp:1", "t") is True


def test_smoke_test_failed_job_and_transport_error(monkeypatch):
    monkeypatch.setattr(
        roost_cli, "_client",
        lambda url, token: httpx.Client(
            base_url=url,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, json={"id": "j1", "state": "failed"}))))
    monkeypatch.setattr(roost_cli.time, "sleep", lambda s: None)
    assert roost_cli._smoke_test("http://cp:1", "t") is False

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(
        roost_cli, "_client",
        lambda url, token: httpx.Client(base_url=url,
                                        transport=httpx.MockTransport(boom)))
    assert roost_cli._smoke_test("http://cp:1", "t") is False
