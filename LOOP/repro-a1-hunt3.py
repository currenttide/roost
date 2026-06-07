"""A1 hunt #3 (worker executors) reproductions for the two DEFERRED findings.

A1 hunt #3 confirmed 5 bugs in roost/worker.py; 3 shipped (R24–R26). The two
below were deferred to Proposed and need a reproducing test each before they can
be promoted (LOOP/PROTOCOL.md A1). Each test asserts CORRECT behaviour and must
FAIL on current master because the bug is present — survey evidence, not the
eventual regression test for the fix.

Both findings live in `Worker._oneshot_agent` (the verifier / self-heal one-shot
agent runner), roost/worker.py.

Bug A — _oneshot_agent corrupts the bwrap argv when inserting the system prompt.
  With worker policy `sandbox: "bwrap"` and a claude build lacking `--sandbox`,
  `_build_claude_argv` returns a bwrap-WRAPPED argv:
      ["bwrap", "--ro-bind", "/", "/", ..., "--", "claude", "-p", <intent>, ...]
  _oneshot_agent then does `argv[:3] + ["--append-system-prompt", sp] + argv[3:]`,
  splicing the flag into the MIDDLE of bwrap's own options (between "/" and the
  rest of "--ro-bind / /"), corrupting the sandbox invocation. The sibling
  `_build_auto_argv` does this correctly via `argv.index("claude")`.

Bug B — the stdout/stderr relay tasks are not cancelled on CancelledError.
  `t1`/`t2` are created, then `asyncio.gather(t1, t2, ...)` is awaited INSIDE the
  `try` (not `finally`). If the parent job is cancelled while _oneshot_agent is
  awaiting `asyncio.wait_for(proc.wait(), ...)`, CancelledError propagates and the
  two relay tasks are left floating (pending, unowned) — asyncio "task was
  destroyed but it is pending" warnings and cross-test interference.
"""

from __future__ import annotations

import asyncio

import pytest

from roost.worker import Worker


def _mk_worker() -> Worker:
    # self_test=False skips heavy capability detection (same as tests/test_steward.py).
    return Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)


# --------------------------------------------------------------------------- #
# Bug A — bwrap argv corruption when splicing --append-system-prompt          #
# --------------------------------------------------------------------------- #

def test_oneshot_agent_keeps_bwrap_argv_intact_with_system_prompt(monkeypatch):
    w = _mk_worker()
    # Opt the worker policy into the OS-level bwrap sandbox fallback so
    # _build_claude_argv returns a bwrap-wrapped argv.
    w.policy = {"sandbox": "bwrap"}
    # Force the bwrap branch: pretend the installed claude has no --sandbox flag
    # (true for claude 2.1.x) so the builder wraps in bwrap instead.
    monkeypatch.setattr("roost.worker._claude_supports_sandbox", lambda: False)
    monkeypatch.setattr("roost.worker.shutil.which", lambda name: f"/usr/bin/{name}")

    captured: dict = {}

    async def _capture(*argv, **kwargs):
        captured["argv"] = list(argv)
        # Bail out of _oneshot_agent cleanly without spawning anything: it catches
        # FileNotFoundError/PermissionError around the spawn and returns early.
        raise FileNotFoundError("stub — argv captured")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capture)

    asyncio.run(
        w._oneshot_agent(
            "job-A", "do the thing", system_prompt="SYS PROMPT", label="verify",
            timeout_s=1.0,
        )
    )
    asyncio.run(w.close())

    argv = captured["argv"]
    assert argv and argv[0] == "bwrap", f"expected a bwrap-wrapped argv, got {argv!r}"

    # bwrap's own leading options must be untouched: the whole-host read-only bind
    # must still read `--ro-bind / /`. The bug splices --append-system-prompt at
    # index 3, turning this into `--ro-bind / --append-system-prompt SYS PROMPT /`.
    assert argv[1:4] == ["--ro-bind", "/", "/"], (
        "bwrap options corrupted — --append-system-prompt was spliced into the "
        f"middle of bwrap's flags: {argv[1:6]!r}"
    )

    # The flag and its value must sit AFTER the `claude -p <intent>` triple, inside
    # the jailed command (i.e. after bwrap's `--` separator), so claude actually
    # receives the system prompt.
    sep = argv.index("--")
    ci = argv.index("claude")
    assert ci > sep, "claude must appear after bwrap's `--` separator"
    assert argv[ci:ci + 3] == ["claude", "-p", "do the thing"]
    assert argv[ci + 3:ci + 5] == ["--append-system-prompt", "SYS PROMPT"], (
        "--append-system-prompt must be inserted right after `claude -p <intent>`, "
        f"not elsewhere: {argv[ci:ci + 6]!r}"
    )


# --------------------------------------------------------------------------- #
# Bug B — relay tasks leak (left pending) when _oneshot_agent is cancelled     #
# --------------------------------------------------------------------------- #

class _NeverReader:
    """A stdout/stderr stand-in: the relay parks in readline() forever (never
    yields EOF), so the only thing that can finish a relay task is cancellation."""

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""


class _HangingProc:
    """Minimal asyncio.subprocess stand-in whose wait() never returns, so the
    parent can be cancelled precisely while inside `asyncio.wait_for(proc.wait())`.
    `wait()` sets `parked` once it is suspended, so the driver can cancel at the
    exact point CancelledError is claimed to leak the relays."""

    def __init__(self, parked: asyncio.Event) -> None:
        self.pid = 424242
        self.returncode = None
        self.stdout = _NeverReader()
        self.stderr = _NeverReader()
        self._parked = parked

    async def wait(self) -> int:
        self._parked.set()
        await asyncio.sleep(3600)
        return 0


def test_oneshot_agent_cancels_relay_tasks_on_cancellation(monkeypatch):
    w = _mk_worker()
    w.policy = {}  # plain (non-bwrap) path — argv building is irrelevant here

    # Avoid any real claude/bwrap dependency in argv construction.
    monkeypatch.setattr("roost.worker.shutil.which", lambda name: f"/usr/bin/{name}")

    # Capture the two relay tasks _oneshot_agent creates (the `rel` coroutines), so
    # we can inspect their state at the moment of cancellation.
    relays: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def _tracking_create_task(coro, *a, **k):
        t = real_create_task(coro, *a, **k)
        if getattr(coro, "__name__", None) == "rel":
            relays.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _tracking_create_task)

    async def _drive() -> int:
        parked = asyncio.Event()

        async def _fake_spawn(*argv, **kwargs):
            return _HangingProc(parked)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

        task = real_create_task(
            w._oneshot_agent("job-B", "do the thing", label="verify", timeout_s=3600.0)
        )
        # Wait until _oneshot_agent has spawned the proc, created both relay tasks,
        # and is genuinely suspended inside `asyncio.wait_for(proc.wait(), ...)` —
        # the exact await where CancelledError is claimed to strand the relays.
        await asyncio.wait_for(parked.wait(), timeout=5.0)

        # Cancel the parent the way a server cancel / job teardown does.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)  # let the parent's `finally` cleanup run

        # CRUCIAL: inspect WHILE the loop is still alive. asyncio.run() cancels any
        # leftover tasks at shutdown, which would mask the leak if we checked after
        # the loop closed. A correct _oneshot_agent cancels its relay tasks in a
        # `finally`; the buggy one leaves them pending here.
        leaked = [t for t in relays if not t.done()]
        for t in leaked:  # tidy up so asyncio.run shutdown stays quiet
            t.cancel()
        return len(leaked)

    leaked_count = asyncio.run(_drive())
    asyncio.run(w.close())

    assert len(relays) == 2, f"expected 2 relay tasks, tracked {len(relays)}"
    assert leaked_count == 0, (
        f"{leaked_count} relay task(s) left PENDING after _oneshot_agent was "
        "cancelled — the gather(t1, t2) is in `try`, not `finally`, so the relays "
        "are never cancelled on CancelledError and float as unowned tasks"
    )
