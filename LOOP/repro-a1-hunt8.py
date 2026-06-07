"""A1 hunt #8 (docker executor) reproductions.

Lens: the `kind: docker` executor in roost/worker.py — container lifecycle,
kill-target selection, exit-code propagation, GPU plumbing, relay, teardown when
the docker daemon is slow/wedged. SKIP security-surface findings.

Each test must FAIL on current master for the claimed reason to qualify a finding
under LOOP/PROTOCOL.md (A1). These are survey evidence, not the eventual
regression tests for the fixes. Docker is not run here: the subprocess seam is
stubbed (FakeProcess pattern, like tests/test_worker.py).

Run only these:  python -m pytest -q LOOP/repro-a1-hunt8.py

------------------------------------------------------------------------------
CONFIRMED #1 — Teardown of a docker job's container uses an UNBOUNDED `await`:
  `_kill_active_job` spawns `docker kill <name>` / `docker rm -f <name>` and waits
  with a bare `await k.wait()` (roost/worker.py ~L1226-1234) — the ONLY subprocess
  wait in worker.py with no timeout (the detection probe `docker info` uses
  timeout=20; every other proc.wait is wrapped in asyncio.wait_for). If the docker
  daemon is wedged (a real failure mode on a busy GPU box: dockerd hung on an
  unkillable D-state container, an overloaded host, a stuck NVIDIA runtime), the
  `docker kill` subprocess never returns, so:
    * `_kill_active_job` hangs forever (test_kill_active_job_*),
    * the wallclock-timeout teardown path in run_job hangs forever, posting NO
      terminal event and leaking the capacity slot / _running counter — the R25
      `finally` (the single cleanup point) is INSIDE the hang and never runs
      (test_run_job_docker_timeout_*),
    * graceful shutdown (`_shutdown_jobs` -> `_kill_active_job`) hangs too, so the
      worker never exits on SIGTERM and the container is orphaned (burning GPU)
      when systemd hard-kills the stuck worker.
  This is exactly the lens question "does run_job's finally + the R25 cleanup hold
  when `docker kill` itself hangs?" — answer: it does NOT.
------------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio

import pytest

import roost.worker as wm
from roost.worker import Worker

# A generous-but-finite cap. On master the wedged `docker kill` never returns, so
# the operations below run unboundedly past this and asyncio.wait_for trips. A
# bounded teardown (a fix that wraps the wait in a timeout and kills the stuck CLI)
# finishes well under HANG_CAP regardless of the exact bound it picks. Master's
# hang is *infinite*, so HANG_CAP only needs to exceed a sane fix's bound.
HANG_CAP = 10.0


class _DeadClient:
    """The `docker run` client process, already exited (returncode set) — i.e. the
    SIGKILL of its process group has happened (or it died on its own). Teardown of
    the sibling CONTAINER (docker kill / docker rm -f) is what remains."""

    def __init__(self, rc: int = 137):
        self.returncode = rc
        self.pid = -1


class _WedgedProc:
    """A `docker kill` / `docker rm -f` subprocess against a WEDGED daemon: it is
    spawned but its wait() never returns on its own (the CLI is blocked talking to
    the hung daemon). Faithful to a real asyncio subprocess: wait() unblocks only
    once the process is killed — so a fix that bounds the wait and then kills the
    stuck CLI will see wait() return, while master (which only ever `await k.wait()`)
    hangs forever."""

    def __init__(self):
        self.returncode = None
        self.pid = 999999
        self._killed = asyncio.Event()

    def kill(self):
        self.returncode = -9
        self._killed.set()

    def terminate(self):
        self.kill()

    async def wait(self):
        await self._killed.wait()  # blocks until kill()/terminate() — never on its own
        return self.returncode


@pytest.fixture()
def wedge_docker(monkeypatch):
    """Stub the subprocess seam so any `docker kill`/`docker rm -f` spawns a wedged
    process. Records the argv that were attempted. No daemon required."""
    attempted: list[list[str]] = []

    async def fake_exec(*argv, **kw):
        attempted.append(list(argv))
        return _WedgedProc()

    monkeypatch.setattr(wm.asyncio, "create_subprocess_exec", fake_exec)
    return attempted


def test_kill_active_job_does_not_hang_when_docker_kill_wedges(wedge_docker):
    """A docker job's teardown must not block forever on a wedged daemon.

    FAILS on master: `await k.wait()` in _kill_active_job is unbounded, so this
    hangs and asyncio.wait_for trips at HANG_CAP. A bounded teardown returns fast.
    """
    async def go():
        w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
        w._active = {
            "jD": {"process": _DeadClient(), "is_docker": True, "cancelled": None}
        }
        try:
            await asyncio.wait_for(
                w._kill_active_job("jD", "timeout"), timeout=HANG_CAP)
        except asyncio.TimeoutError:
            pytest.fail(
                "_kill_active_job hung > %.1fs on a wedged `docker kill` "
                "(unbounded `await k.wait()`); attempted=%r"
                % (HANG_CAP, wedge_docker))
        finally:
            await w.close()

    asyncio.run(go())


def test_run_job_docker_timeout_teardown_does_not_hang_when_daemon_wedged(
    wedge_docker, monkeypatch
):
    """The wallclock-timeout path for a docker job must still post a terminal event
    and unwind accounting even if container teardown wedges.

    FAILS on master: run_job's timeout branch awaits _kill_active_job (which hangs
    on the wedged `docker kill`) BEFORE the R25 finally, so no terminal event is
    posted and _running leaks. asyncio.wait_for trips at HANG_CAP.
    """
    async def go():
        w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
        w.policy = {}
        events: list[dict] = []

        async def fake_post_event(job_id, event):
            events.append(event)

        async def fake_send_log(job_id, stream, data):
            pass

        async def fake_diagnose(spec, **kw):
            return {"summary": "stub"}

        w._post_event = fake_post_event       # type: ignore[assignment]
        w._send_log = fake_send_log           # type: ignore[assignment]
        w._diagnose_failure = fake_diagnose   # type: ignore[assignment]

        # Real `docker run` stand-in: a long sleeper we own, so the wallclock cap
        # fires and the SIGKILL-of-client path runs; the wedged seam is hit only
        # by the subsequent `docker kill`/`docker rm -f` (CONTAINER teardown).
        real_exec = asyncio.subprocess.create_subprocess_exec

        async def routing_exec(*argv, **kw):
            if tuple(argv[:2]) == ("docker", "run"):
                return await real_exec("sleep", "30", **kw)
            # docker kill / docker rm -f -> wedged (recorded by the fixture too)
            wedge_docker.append(list(argv))
            return _WedgedProc()

        monkeypatch.setattr(wm.asyncio, "create_subprocess_exec", routing_exec)

        spec = {
            "kind": "docker",
            "image": "alpine",
            "command": "sleep 30",
            "budget": {"max_wallclock_sec": 0.5},  # fires fast
        }
        # run_job itself increments _running (after "started") and the R25 finally
        # decrements it. Start at 0: a clean run returns to 0; a hung teardown that
        # skips the finally leaves it at 1.
        w._running = 0
        try:
            await asyncio.wait_for(
                w.run_job({"id": "jD", "spec": spec}), timeout=HANG_CAP)
        except asyncio.TimeoutError:
            pytest.fail(
                "run_job hung > %.1fs in the docker wallclock-timeout teardown "
                "(unbounded `docker kill`); terminal events posted=%r, _running=%d "
                "(R25 finally never ran)"
                % (HANG_CAP, [e.get("type") for e in events], w._running))
        finally:
            await w.close()

        # If a fix lets teardown finish, the job must report a terminal outcome and
        # release its slot rather than vanishing mid-teardown.
        terminal = [e for e in events if e.get("type") in ("succeeded", "failed")]
        assert terminal, (
            "docker timeout produced no terminal event; events=%r"
            % [e.get("type") for e in events])
        assert w._running == 0, "capacity slot leaked (R25 cleanup did not run)"

    asyncio.run(go())
