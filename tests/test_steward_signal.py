"""[R117] Steward timeout/failure is a structured, visible signal — not a silent None.

`_run_steward_agent` previously collapsed timeout / spawn-failure / bad-output into
`None`; callers fell back to the heuristic and the failure disappeared. These tests
pin the new contract:

  * each attempt resolves to a distinguishable STEWARD_* outcome,
  * every failure emits one loud STEWARD_AGENT_FAILED log line,
  * consecutive failures are counted and advertised in heartbeat capabilities
    (`steward_failures` + `steward_last_error`, absent when healthy — the R41
    `gpu_detection` seam),
  * the callers' heuristic fallback behavior itself is unchanged.

The real `claude` binary is never invoked: the subprocess is faked.
"""
from __future__ import annotations

import asyncio

from roost import steward
from roost.worker import (
    STEWARD_BAD_OUTPUT,
    STEWARD_NO_BINARY,
    STEWARD_OK,
    STEWARD_SPAWN_FAILURE,
    STEWARD_TIMEOUT,
    Worker,
)


def _mk_worker():
    return Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0, hang: bool = False):
        self._stdout = stdout
        self.returncode = returncode
        self._hang = hang
        self.pid = -1  # os.getpgid(-1) raises; the kill path must swallow that

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(60)
        return (self._stdout, b"")

    async def wait(self):
        return self.returncode


def _patch_proc(monkeypatch, proc: _FakeProc):
    monkeypatch.setattr("roost.worker.shutil.which", lambda _n: "/usr/bin/claude")

    async def fake_exec(*argv, **kwargs):
        return proc

    monkeypatch.setattr("roost.worker.asyncio.create_subprocess_exec", fake_exec)


def _run(w: Worker, timeout_s: float = 5.0):
    try:
        return asyncio.run(
            w._run_steward_agent("prompt", label="capacity", timeout_s=timeout_s))
    finally:
        asyncio.run(w.close())


# ---------- distinguishable outcomes + per-occurrence loud log ----------


def test_success_returns_text_and_resets_counter(monkeypatch, capsys):
    w = _mk_worker()
    w._steward_failures = 3            # pretend the node was flaky earlier
    w._steward_last_outcome = STEWARD_TIMEOUT
    _patch_proc(monkeypatch, _FakeProc(stdout=b'{"type":"result","result":"ok"}'))
    assert _run(w) == "ok"
    assert w._steward_failures == 0
    assert w._steward_last_outcome == STEWARD_OK
    assert "STEWARD_AGENT_FAILED" not in capsys.readouterr().out


def test_missing_binary_is_distinguishable_and_loud(monkeypatch, capsys):
    w = _mk_worker()
    monkeypatch.setattr("roost.worker.shutil.which", lambda _n: None)
    assert _run(w) is None
    assert w._steward_failures == 1
    assert w._steward_last_outcome == STEWARD_NO_BINARY
    out = capsys.readouterr().out
    assert "STEWARD_AGENT_FAILED" in out
    assert "outcome=no-binary" in out
    assert "label=capacity" in out


def test_timeout_is_distinguishable_and_loud(monkeypatch, capsys):
    w = _mk_worker()
    _patch_proc(monkeypatch, _FakeProc(hang=True))
    assert _run(w, timeout_s=0.01) is None
    assert w._steward_failures == 1
    assert w._steward_last_outcome == STEWARD_TIMEOUT
    out = capsys.readouterr().out
    assert "STEWARD_AGENT_FAILED" in out
    assert "outcome=timeout" in out


def test_spawn_failure_is_distinguishable_and_loud(monkeypatch, capsys):
    w = _mk_worker()
    monkeypatch.setattr("roost.worker.shutil.which", lambda _n: "/usr/bin/claude")

    async def boom(*argv, **kwargs):
        raise PermissionError("no exec for you")

    monkeypatch.setattr("roost.worker.asyncio.create_subprocess_exec", boom)
    assert _run(w) is None
    assert w._steward_last_outcome == STEWARD_SPAWN_FAILURE
    out = capsys.readouterr().out
    assert "outcome=spawn-failure" in out
    assert "PermissionError" in out


def test_bad_output_nonzero_exit_and_empty_stdout(monkeypatch, capsys):
    w = _mk_worker()
    _patch_proc(monkeypatch, _FakeProc(stdout=b"boom", returncode=2))
    assert asyncio.run(
        w._run_steward_agent("p", label="diagnosis", timeout_s=5.0)) is None
    assert w._steward_last_outcome == STEWARD_BAD_OUTPUT
    assert "exit_code=2" in capsys.readouterr().out

    _patch_proc(monkeypatch, _FakeProc(stdout=b"", returncode=0))
    assert _run(w) is None
    assert w._steward_failures == 2  # consecutive across both bad outputs
    assert w._steward_last_outcome == STEWARD_BAD_OUTPUT
    out = capsys.readouterr().out
    assert "outcome=bad-output" in out
    assert "empty stdout" in out
    assert "consecutive=2" in out


# ---------- aggregate signal in the heartbeat (R41 gpu_detection seam) ----------


def test_heartbeat_capabilities_omit_signal_when_healthy():
    w = _mk_worker()
    caps = w._heartbeat_capabilities()
    assert "steward_failures" not in caps
    assert "steward_last_error" not in caps
    assert "load" in caps  # existing payload shape intact
    asyncio.run(w.close())


def test_heartbeat_capabilities_carry_failure_count_and_label():
    w = _mk_worker()
    w._steward_failures = 4
    w._steward_last_outcome = STEWARD_TIMEOUT
    caps = w._heartbeat_capabilities()
    assert caps["steward_failures"] == 4
    assert caps["steward_last_error"] == "timeout"
    asyncio.run(w.close())


def test_heartbeat_signal_clears_after_success(monkeypatch):
    w = _mk_worker()
    monkeypatch.setattr("roost.worker.shutil.which", lambda _n: None)
    asyncio.run(w._run_steward_agent("p", label="capacity", timeout_s=1.0))
    assert "steward_failures" in w._heartbeat_capabilities()
    _patch_proc(monkeypatch, _FakeProc(stdout=b'{"type":"result","result":"4"}'))
    asyncio.run(w._run_steward_agent("p", label="capacity", timeout_s=1.0))
    assert "steward_failures" not in w._heartbeat_capabilities()
    asyncio.run(w.close())


# ---------- heuristic fallback behavior itself unchanged ----------


def test_capacity_falls_back_mechanically_on_timeout_and_counts_it(monkeypatch):
    """End-to-end through _judge_capacity: a steward timeout still degrades to the
    mechanical estimate exactly as before, but is now counted and labeled."""
    w = _mk_worker()
    _patch_proc(monkeypatch, _FakeProc(hang=True))
    monkeypatch.setattr("roost.worker.CAPACITY_AGENT_TIMEOUT", 0.01)
    cap = asyncio.run(w._judge_capacity())
    assert cap >= steward.FALLBACK_CAPACITY  # mechanical fallback, floor 1
    assert w._capacity == cap
    assert w._steward_failures == 1
    assert w._steward_last_outcome == STEWARD_TIMEOUT
    asyncio.run(w.close())


def test_diagnosis_falls_back_deterministically_on_missing_binary(monkeypatch):
    w = _mk_worker()
    monkeypatch.setattr("roost.worker.shutil.which", lambda _n: None)
    d = asyncio.run(w._diagnose_failure(
        {"kind": "command", "command": "pytest"}, exit_code=1,
        stdout_tail="", stderr_tail="AssertionError: 2 != 3"))
    assert d == "exit_code=1 — AssertionError: 2 != 3"
    assert w._steward_failures == 1
    assert w._steward_last_outcome == STEWARD_NO_BINARY
    asyncio.run(w.close())
