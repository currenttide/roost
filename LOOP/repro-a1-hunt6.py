"""A1 hunt #6 — roost/worker.py under a concurrency/interleaving lens.

Each test must FAIL on current master for the stated reason to qualify its
finding (PROTOCOL.md A1). Deterministic: explicit interleaving control, no
sleep-and-hope. NOT collected by the default suite (LOOP/ is excluded).

Run:  python -m pytest LOOP/repro-a1-hunt6.py -q
"""
import asyncio
import os
import sys
from unittest.mock import patch

# Import the SAME `roost` package this checkout/worktree builds: prepend the repo
# root that contains this LOOP/ dir (two levels up), so `python -m pytest
# LOOP/repro-a1-hunt6.py` exercises THIS tree's roost/worker.py (not an editable
# install that may point elsewhere).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import roost.worker as wmod
from roost.worker import Worker


# --------------------------------------------------------------------------
# Shared fakes — drive the REAL run_job without spawning a real subprocess.
# --------------------------------------------------------------------------

class _EOFStream:
    """A stdout/stderr stand-in that signals EOF immediately, so each relay
    task finishes at once (no blocking the finally's gather)."""

    async def readline(self) -> bytes:
        return b""


class _QuickProc:
    """A process that has already exited cleanly: wait() returns 0 at once and
    its streams are at EOF. Drives run_job's normal-completion path fast."""

    pid = 111
    returncode = 0

    def __init__(self) -> None:
        self.stdout = _EOFStream()
        self.stderr = _EOFStream()

    async def wait(self) -> int:
        return 0


class _HangingProc:
    """A process that stays alive: wait() never returns, streams at EOF (so the
    relays finish but the job keeps holding its _job_tasks slot)."""

    pid = 222
    returncode = None

    def __init__(self) -> None:
        self.stdout = _EOFStream()
        self.stderr = _EOFStream()

    async def wait(self) -> int:
        await asyncio.sleep(9999)
        return 0


def _silent_worker() -> Worker:
    """A Worker whose network calls are no-ops, for driving run_job/the loop body."""
    w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)

    async def _noop_event(_jid, _ev):
        pass

    async def _noop_log(_jid, _stream, _data):
        pass

    w._post_event = _noop_event       # type: ignore[assignment]
    w._send_log = _noop_log           # type: ignore[assignment]
    return w


# --------------------------------------------------------------------------
# Finding 1 — _reap_stale_attempt's `old.done()` early-return races the OLD
# job task's pending done-callback, which then evicts the NEW attempt's
# _job_tasks entry.
#
# worker.py:1669  `if old is None or old.done(): return`  — early return, NO await
# worker.py:1688  `self._job_tasks.pop(_jid, None)`       — unconditional pop
#
# Sequence the real loop produces (loop body: poll -> _reap_stale_attempt ->
# _spawn_job, with no await between reap's early-return and _spawn_job):
#
#   1. A job's run_job task FINISHES during the poll await. Its done-callback
#      (_done) is SCHEDULED via call_soon but has not run yet — a normal asyncio
#      state ("task.done() is True, callback pending").
#   2. The same job_id is re-leased (CP outage -> sweep -> requeue -> we win the
#      new lease). The loop calls _reap_stale_attempt(job_id): old.done() is
#      True, so it returns IMMEDIATELY without awaiting.
#   3. _spawn_job installs the NEW task at _job_tasks[job_id].
#   4. The OLD task's pending _done callback now fires and does
#      _job_tasks.pop(job_id) — EVICTING THE NEW TASK'S ENTRY.
#
# Harm: len(_job_tasks) undercounts (capacity gate over-leases), _shutdown_jobs
# can't cancel the orphaned task, and a later re-lease's _reap_stale_attempt no
# longer sees the running task (so it won't reap it -> two concurrent attempts of
# the same job -> double execution).
#
# A correct _done must only evict an entry that still points at ITS OWN task.
# --------------------------------------------------------------------------

def test_reap_early_return_lets_stale_callback_evict_new_task_entry():
    async def go():
        w = _silent_worker()
        w._capacity = 4
        spec = {"id": "jX", "spec": {"kind": "command", "command": "x",
                                     "verify": False}}

        # Attempt 1 gets a quick (already-exited) process so its task finishes
        # fast; attempt 2 (the re-lease) gets a HANGING process so it stays in
        # _job_tasks — making an eviction unambiguously a bug (a live task lost
        # from tracking) rather than a normal self-removal.
        proc_seq = [_QuickProc, _HangingProc]

        async def _fake_create(*_a, **_k):
            return proc_seq.pop(0)()

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_create), \
             patch.object(wmod, "build_command",
                          side_effect=lambda spec, jid, **kw: (
                              ["/bin/true"], "/tmp", [])):
            # First attempt — run_job finishes quickly.
            w._spawn_job(spec)
            old = w._job_tasks["jX"]

            # Pump the loop just enough to reach the deterministic window:
            # old task DONE, but its _done callback NOT YET run (jX still maps
            # to the OLD task). This is exactly the state a poll await can leave.
            window = False
            for _ in range(2000):
                await asyncio.sleep(0)
                if old.done():
                    window = w._job_tasks.get("jX") is old
                    break
            assert old.done(), "old task should have finished"
            assert window, (
                "could not reach the done-but-callback-pending window "
                "(scheduling); rerun")

            # The real loop body for a re-lease of jX, with NO await between the
            # reap's early return and _spawn_job (matches worker.py:1628-1629).
            await w._reap_stale_attempt("jX")   # old.done() -> early return
            w._spawn_job(spec)                  # install NEW task (hanging proc)
            new = w._job_tasks.get("jX")
            assert new is not None and new is not old

            # Let the NEW attempt genuinely start (register in _active) so it is
            # provably a LIVE, still-running task — then let the OLD task's
            # pending callback fire.
            for _ in range(200):
                if "jX" in w._active and not new.done():
                    break
                await asyncio.sleep(0)
            assert not new.done() and "jX" in w._active, (
                "new attempt should be live and running")
            for _ in range(10):
                await asyncio.sleep(0)

            present = w._job_tasks.get("jX")
            new_still_running = not new.done()
            # Tidy up before asserting so a leak can't hang asyncio shutdown.
            for t in list(w._job_tasks.values()):
                t.cancel()
            if new is not None and not new.done():
                new.cancel()
            for _ in range(20):
                await asyncio.sleep(0)

            assert present is new and new_still_running, (
                "the NEW attempt's _job_tasks entry was EVICTED by the OLD "
                f"task's stale done-callback while the NEW task was still "
                f"running (present={present!r}, new_running={new_still_running}); "
                "the worker now under-counts in-flight jobs (capacity gate "
                "over-leases) and the running task is orphaned from _job_tasks "
                "(shutdown/reap miss it -> double execution on the next re-lease)")
        await w.close()

    asyncio.run(go())


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
