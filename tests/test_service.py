"""Tests for roost/service.py — the supervised-worker install layer (R10).

Every subprocess boundary (systemctl / launchctl / loginctl / journalctl /
tail) is mocked and its argv recorded; Path.home() is redirected via HOME so
unit/plist files land in a tmp dir. The renderers are asserted on real
content — the launchd plist is parsed with plistlib, not regexed.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from roost import service


# ---------- harness ----------


class FakeRun:
    """Records every subprocess.run argv; returns scripted results."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=self.stdout, stderr=self.stderr)

    def argv_starting(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@pytest.fixture()
def fake_run(monkeypatch) -> FakeRun:
    run = FakeRun()
    monkeypatch.setattr(service.subprocess, "run", run)
    return run


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _which(available: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def _on(monkeypatch, system: str, which: set[str]) -> None:
    monkeypatch.setattr(service.platform, "system", lambda: system)
    monkeypatch.setattr(service.shutil, "which", _which(which))


# ---------- _resolve_roost_bin ----------


def test_resolve_roost_bin_on_path(monkeypatch):
    monkeypatch.setattr(service.shutil, "which", _which({"roost"}))
    assert service._resolve_roost_bin() == "/usr/bin/roost"


def test_resolve_roost_bin_uv_fallback(home: Path, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", _which(set()))
    fallback = home / ".local" / "bin" / "roost"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("#!/bin/sh\n")
    assert service._resolve_roost_bin() == str(fallback)


def test_resolve_roost_bin_python_m_last_resort(home: Path, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", _which(set()))
    assert service._resolve_roost_bin() == f"{sys.executable} -m roost.cli"


# ---------- _service_env ----------


def test_service_env_propagates_only_allowlist(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/claude-iso")
    monkeypatch.setenv("DOCKER_CONFIG", "/tmp/docker")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-this")
    assert service._service_env() == {
        "CLAUDE_CONFIG_DIR": "/tmp/claude-iso",
        "DOCKER_CONFIG": "/tmp/docker",
    }


def test_service_env_skips_empty(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    assert service._service_env() == {}


# ---------- renderers ----------


def test_render_systemd_unit_core_directives(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    unit = service._render_systemd_unit("/usr/bin/roost")
    assert "ExecStart=/usr/bin/roost worker\n" in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit
    # The PATH line that makes uv-installed tools (claude) visible.
    assert "Environment=PATH=%h/.local/bin:" in unit


def test_render_systemd_unit_env_quoting(monkeypatch):
    # A value with spaces, a backslash and a double quote must survive
    # systemd's parsing: whole KEY=value quoted, internal \ and " escaped.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", 'C:\\weird "dir"/claude')
    unit = service._render_systemd_unit("/usr/bin/roost")
    assert ('Environment="CLAUDE_CONFIG_DIR='
            'C:\\\\weird \\"dir\\"/claude"\n') in unit


def test_render_launchd_plist_is_valid_and_complete(home: Path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    plist = plistlib.loads(
        service._render_launchd_plist("/usr/local/bin/roost").encode())
    assert plist["Label"] == "com.roost.worker"
    assert plist["ProgramArguments"] == ["/usr/local/bin/roost", "worker"]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["StandardOutPath"].endswith("Logs/roost-worker.log")
    assert str(home) in plist["EnvironmentVariables"]["PATH"]


def test_render_launchd_plist_multiword_bin_and_escaping(home, monkeypatch):
    # `python -m roost.cli` must become three argv entries (+ "worker"), and
    # XML-special env values must be escaped, not break the document.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", '/tmp/<a>&"b"')
    plist = plistlib.loads(
        service._render_launchd_plist(f"{sys.executable} -m roost.cli").encode())
    assert plist["ProgramArguments"] == [sys.executable, "-m", "roost.cli", "worker"]
    assert plist["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"] == '/tmp/<a>&"b"'


# ---------- install ----------


def test_install_linux_writes_unit_and_enables(home, fake_run, monkeypatch):
    _on(monkeypatch, "Linux", {"roost", "systemctl", "loginctl"})
    monkeypatch.setenv("USER", "yang")
    rc, msg = service.install(start=False)
    assert rc == 0
    unit = home / ".config" / "systemd" / "user" / "roost-worker.service"
    assert unit.is_file()
    assert "ExecStart=/usr/bin/roost worker" in unit.read_text()
    assert "systemctl --user start" in msg  # tells the operator how to start
    assert ["loginctl", "enable-linger", "yang"] in fake_run.calls
    assert ["systemctl", "--user", "daemon-reload"] in fake_run.calls
    assert ["systemctl", "--user", "enable", "roost-worker.service"] in fake_run.calls
    # start=False: no start dispatched
    assert fake_run.argv_starting("systemctl", "--user", "start") == []


def test_install_linux_start_true_dispatches_start(home, fake_run, monkeypatch):
    _on(monkeypatch, "Linux", {"roost", "systemctl"})
    rc, msg = service.install(start=True)
    assert rc == 0 and "started=True" in msg
    assert ["systemctl", "--user", "start", "roost-worker.service"] in fake_run.calls


def test_install_linux_propagates_start_rc(home, fake_run, monkeypatch):
    _on(monkeypatch, "Linux", {"roost", "systemctl"})
    fake_run.returncode = 4
    rc, _ = service.install(start=True)
    assert rc == 4


def test_install_linux_no_systemctl(home, fake_run, monkeypatch):
    _on(monkeypatch, "Linux", {"roost"})
    rc, msg = service.install()
    assert rc == 2
    assert "systemctl not found" in msg
    # The unit is still written so a later systemctl can use it.
    assert (home / ".config" / "systemd" / "user" / "roost-worker.service").is_file()
    assert fake_run.calls == []


def test_install_darwin_writes_plist_no_start(home, fake_run, monkeypatch):
    _on(monkeypatch, "Darwin", {"roost", "launchctl"})
    rc, msg = service.install(start=False)
    assert rc == 0 and "launchctl bootstrap" in msg
    plist_path = home / "Library" / "LaunchAgents" / "com.roost.worker.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == "com.roost.worker"
    assert fake_run.calls == []  # bootstrap is only run with start=True


def test_install_darwin_start_true_bootstraps(home, fake_run, monkeypatch):
    _on(monkeypatch, "Darwin", {"roost", "launchctl"})
    rc, msg = service.install(start=True)
    assert rc == 0 and "started" in msg
    uid = os.getuid()
    plist_path = home / "Library" / "LaunchAgents" / "com.roost.worker.plist"
    assert ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)] in fake_run.calls
    assert ["launchctl", "kickstart", "-k",
            f"gui/{uid}/com.roost.worker"] in fake_run.calls


def test_install_unsupported_platform(fake_run, monkeypatch):
    _on(monkeypatch, "Windows", {"roost"})
    rc, msg = service.install()
    assert rc == 2 and "unsupported platform" in msg
    assert fake_run.calls == []


# ---------- start / stop / status ----------


def test_start_stop_linux(fake_run, monkeypatch):
    _on(monkeypatch, "Linux", {"systemctl"})
    rc, _ = service.start()
    assert rc == 0
    assert ["systemctl", "--user", "start", "roost-worker.service"] in fake_run.calls
    rc, _ = service.stop()
    assert ["systemctl", "--user", "stop", "roost-worker.service"] in fake_run.calls


def test_start_propagates_rc(fake_run, monkeypatch):
    _on(monkeypatch, "Linux", {"systemctl"})
    fake_run.returncode = 3
    rc, _ = service.start()
    assert rc == 3


def test_start_stop_darwin(fake_run, monkeypatch):
    _on(monkeypatch, "Darwin", {"launchctl"})
    uid = os.getuid()
    service.start()
    assert ["launchctl", "kickstart", "-k",
            f"gui/{uid}/com.roost.worker"] in fake_run.calls
    service.stop()
    assert ["launchctl", "bootout", f"gui/{uid}/com.roost.worker"] in fake_run.calls


def test_start_stop_status_no_supervisor(fake_run, monkeypatch):
    _on(monkeypatch, "Linux", set())  # no systemctl
    for fn in (service.start, service.stop, service.status):
        rc, msg = fn()
        assert rc == 2 and "no supervisor" in msg
    assert fake_run.calls == []


def test_status_returns_output(fake_run, monkeypatch):
    _on(monkeypatch, "Linux", {"systemctl"})
    fake_run.stdout = "● roost-worker.service - active (running)"
    rc, msg = service.status()
    assert rc == 0 and "active (running)" in msg
    assert ["systemctl", "--user", "status", "roost-worker.service"] in fake_run.calls


def test_status_darwin(fake_run, monkeypatch):
    _on(monkeypatch, "Darwin", {"launchctl"})
    fake_run.stdout = "state = running"
    rc, msg = service.status()
    assert rc == 0 and "state = running" in msg
    assert fake_run.calls[0][:2] == ["launchctl", "print"]


# ---------- logs ----------


def test_logs_linux(fake_run, monkeypatch):
    _on(monkeypatch, "Linux", {"journalctl"})
    service.logs()
    assert ["journalctl", "--user", "-u", "roost-worker.service"] in fake_run.calls
    service.logs(follow=True)
    assert ["journalctl", "--user", "-u", "roost-worker.service", "-f"] in fake_run.calls


def test_logs_darwin_missing_file(home, fake_run, monkeypatch):
    _on(monkeypatch, "Darwin", {"launchctl"})
    rc, msg = service.logs()
    assert rc == 2 and "no log file" in msg
    assert fake_run.calls == []


def test_logs_darwin_tail(home, fake_run, monkeypatch):
    _on(monkeypatch, "Darwin", {"launchctl"})
    log = home / "Library" / "Logs" / "roost-worker.log"
    log.parent.mkdir(parents=True)
    log.write_text("hello\n")
    service.logs()
    assert ["tail", "-n", "200", str(log)] in fake_run.calls
    service.logs(follow=True)
    assert ["tail", "-f", str(log)] in fake_run.calls


def test_logs_unsupported(fake_run, monkeypatch):
    _on(monkeypatch, "Windows", set())
    rc, msg = service.logs()
    assert rc == 2 and "no log source" in msg
