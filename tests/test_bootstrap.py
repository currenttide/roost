"""Tests for roost/bootstrap.py — the `roost up` zero-to-fleet on-ramp (R9).

The pure helpers are tested directly; the network pollers (ping_ok,
wait_for_health, wait_for_worker) run against a stubbed CP via
httpx.MockTransport so no process or socket is involved. Failure paths
(down CP, 401s, wrong worker, timeouts) are covered explicitly.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import httpx
import pytest

from roost import bootstrap


# ---------- pure helpers ----------


def test_build_url_rewrites_wildcard_bind():
    # 0.0.0.0 listens everywhere but is not a connectable address.
    assert bootstrap.build_url("0.0.0.0", 8787) == "http://127.0.0.1:8787"


def test_build_url_other_hosts_verbatim():
    assert bootstrap.build_url("127.0.0.1", 8787) == "http://127.0.0.1:8787"
    assert bootstrap.build_url("localhost", 9000) == "http://localhost:9000"
    assert bootstrap.build_url("192.168.1.50", 8787) == "http://192.168.1.50:8787"


def test_panel_url():
    assert (bootstrap.panel_url("http://127.0.0.1:8787", "tok")
            == "http://127.0.0.1:8787/panel?token=tok")
    # Trailing slash is normalized; empty token → bare /panel.
    assert (bootstrap.panel_url("http://127.0.0.1:8787/", "")
            == "http://127.0.0.1:8787/panel")


def test_is_loopback():
    for host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        assert bootstrap.is_loopback(host), host
    for host in ("192.168.1.50", "100.97.12.7", "example.com", ""):
        assert not bootstrap.is_loopback(host), host


def test_config_payload_full_and_minimal():
    full = bootstrap.config_payload(
        "http://cp:8787", "tok", worker_id="w1", name="box")
    # `credential` is the key config.resolve_url_token reads as the bearer.
    assert full == {"url": "http://cp:8787", "credential": "tok",
                    "worker_id": "w1", "name": "box"}
    assert bootstrap.config_payload("http://cp:8787", "") == {"url": "http://cp:8787"}


def test_env_file_text():
    assert bootstrap.env_file_text("http://cp:8787", "tok") == (
        "export ROOST_URL=http://cp:8787\n"
        "export ROOST_TOKEN=tok\n"
    )


def test_write_env_file_explicit_path(tmp_path: Path):
    path = tmp_path / "deep" / "env"  # parent does not exist yet
    out = bootstrap.write_env_file("http://cp:8787", "tok", path=path)
    assert out == path
    assert path.read_text() == bootstrap.env_file_text("http://cp:8787", "tok")
    # Token-bearing file must be private.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_env_file_honors_roost_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROOST_HOME", str(tmp_path / "rhome"))
    out = bootstrap.write_env_file("http://cp:8787", "tok")
    assert out == tmp_path / "rhome" / "env"
    assert out.is_file()


def test_write_env_file_overwrites_and_keeps_mode(tmp_path: Path):
    path = tmp_path / "env"
    bootstrap.write_env_file("http://old:1", "old-tok", path=path)
    bootstrap.write_env_file("http://new:2", "new-tok", path=path)
    text = path.read_text()
    assert "new-tok" in text and "old-tok" not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_gen_admin_token():
    a, b = bootstrap.gen_admin_token(), bootstrap.gen_admin_token()
    assert a != b
    assert len(a) == 32
    int(a, 16)  # hex


def test_default_worker_name(monkeypatch):
    monkeypatch.setattr(bootstrap.socket, "gethostname", lambda: "my-box")
    assert bootstrap.default_worker_name() == "my-box"


# ---------- stubbed-CP pollers ----------


def _stub_cp(monkeypatch, handler) -> None:
    """Route every httpx.Client the module creates through a MockTransport."""
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(bootstrap.httpx, "Client", fake_client)


def test_ping_ok_up(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _stub_cp(monkeypatch, handler)
    assert bootstrap.ping_ok("http://cp:8787/", token="tok") is True
    assert seen["path"] == "/healthz"
    assert seen["auth"] == "Bearer tok"


def test_ping_ok_no_token_sends_no_header(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200)

    _stub_cp(monkeypatch, handler)
    assert bootstrap.ping_ok("http://cp:8787") is True
    assert seen["auth"] is None


def test_ping_ok_error_status(monkeypatch):
    _stub_cp(monkeypatch, lambda req: httpx.Response(500))
    assert bootstrap.ping_ok("http://cp:8787") is False


def test_ping_ok_connection_refused(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _stub_cp(monkeypatch, handler)
    assert bootstrap.ping_ok("http://cp:8787") is False


def test_wait_for_health_immediate(monkeypatch):
    _stub_cp(monkeypatch, lambda req: httpx.Response(200))
    assert bootstrap.wait_for_health("http://cp:8787", timeout=1.0,
                                     interval=0.01) is True


def test_wait_for_health_cp_comes_up_late(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:  # down for the first two polls
            raise httpx.ConnectError("not yet", request=request)
        return httpx.Response(200)

    _stub_cp(monkeypatch, handler)
    assert bootstrap.wait_for_health("http://cp:8787", timeout=5.0,
                                     interval=0.01) is True
    assert calls["n"] == 3


def test_wait_for_health_timeout(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dead", request=request)

    _stub_cp(monkeypatch, handler)
    assert bootstrap.wait_for_health("http://cp:8787", timeout=0.05,
                                     interval=0.01) is False


def _workers_handler(payloads):
    """Handler returning successive /workers payloads (last repeats)."""
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/workers"
        body = payloads[min(state["i"], len(payloads) - 1)]
        state["i"] += 1
        if isinstance(body, int):
            return httpx.Response(body, json={"detail": "nope"})
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, json=body)

    return handler


def test_wait_for_worker_found(monkeypatch):
    w = {"id": "w1", "status": "idle"}
    _stub_cp(monkeypatch, _workers_handler([[w]]))
    got = bootstrap.wait_for_worker("http://cp:8787", "tok",
                                    timeout=1.0, interval=0.01)
    assert got == w


def test_wait_for_worker_waits_for_online_status(monkeypatch):
    # Registered but offline → not accepted; flips to busy → returned.
    _stub_cp(monkeypatch, _workers_handler([
        [{"id": "w1", "status": "offline"}],
        [{"id": "w1", "status": "busy"}],
    ]))
    got = bootstrap.wait_for_worker("http://cp:8787", "tok",
                                    timeout=2.0, interval=0.01)
    assert got["status"] == "busy"


def test_wait_for_worker_filters_by_id(monkeypatch):
    # Another worker online must NOT satisfy a wait for w2.
    _stub_cp(monkeypatch, _workers_handler([
        [{"id": "w1", "status": "idle"}],
        [{"id": "w1", "status": "idle"}, {"id": "w2", "status": "idle"}],
    ]))
    got = bootstrap.wait_for_worker("http://cp:8787", "tok", worker_id="w2",
                                    timeout=2.0, interval=0.01)
    assert got["id"] == "w2"


def test_wait_for_worker_timeout_none(monkeypatch):
    _stub_cp(monkeypatch, _workers_handler([[]]))
    assert bootstrap.wait_for_worker("http://cp:8787", "tok",
                                     timeout=0.05, interval=0.01) is None


def test_wait_for_worker_tolerates_errors_then_succeeds(monkeypatch):
    # A 401 page and a transport error must not crash the poll loop.
    _stub_cp(monkeypatch, _workers_handler([
        401,
        httpx.ConnectError("flap"),
        [{"id": "w1", "status": "idle"}],
    ]))
    got = bootstrap.wait_for_worker("http://cp:8787", "tok",
                                    timeout=2.0, interval=0.01)
    assert got["id"] == "w1"


def test_wait_for_worker_all_errors_times_out(monkeypatch):
    _stub_cp(monkeypatch, _workers_handler([401]))
    assert bootstrap.wait_for_worker("http://cp:8787", "tok",
                                     timeout=0.05, interval=0.01) is None
