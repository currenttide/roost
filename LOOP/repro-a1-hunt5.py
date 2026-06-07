"""A1 hunt #5 — server lifecycle / concurrency reproductions.

Lens: the event-ingestion + lifecycle seams in roost/server.py under
CONCURRENCY/INTERLEAVING. Each test below FAILS on current master for the
claimed reason; they are survey evidence (PROTOCOL.md A1), not the eventual
regression tests for the fix.

Not collected by the default suite (filename is `repro-*`, not `test_*`).
Run explicitly:  python -m pytest LOOP/repro-a1-hunt5.py -q

CONFIRMED BUG — orphaned interactive input on terminal transition
-----------------------------------------------------------------
R38's contract (README "Interactive follow-up", and the _queue_job_input /
worker _deliver_inputs docstrings) is explicit:

    "Every input ends in one of three states, never silently lost."
    queued -> delivered  (worker wrote it to the live stdin), or
    queued -> dropped    (undeliverable: wrong kind, OR the job went terminal
                          before pickup).

But a `queued` input is stranded in `queued` FOREVER whenever its job reaches
ANY terminal state before the owning worker pulls it on its (≤15s) heartbeat:

  * server-side, NOTHING reconciles job_inputs on a terminal transition —
    _apply_event (succeeded/failed), _cancel_job, _finalize_job, and _sweep
    (lease-expiry fail) all leave queued rows untouched; and
  * the worker is only ever TOLD to deliver via the heartbeat `inputs` list,
    which _pending_input_job_ids restricts to state IN (assigned,running) —
    so once the job is terminal the worker is never asked, and it has already
    dropped the job from its _active map.

The input is therefore "silently lost": never delivered, never dropped, no
actor will ever resolve it. This is both a lifecycle gap (every terminal path)
AND a genuine race — cancelling a job at the moment a client sends it input
leaves that input orphaned (the cancel-vs-queue interleaving below).

Note: the lease-expiry REQUEUE path (vs. fail) is deliberately NOT a bug — a
requeued job is still active, so its queued input correctly waits for the next
worker to deliver. Only TERMINAL transitions strand it.

CLEARED hypotheses (interleavings investigated and found SAFE — recorded here
because cleared output is valuable per PROTOCOL.md A1):

  * Sweeper requeue vs. a live heartbeat's lease renewal at the TTL boundary —
    SAFE. Every writer (_sweep, _heartbeat_worker, _apply_event) opens with
    `BEGIN IMMEDIATE`, which takes a RESERVED lock; a second writer BLOCKS until
    the first commits (verified empirically), and each re-reads job state inside
    its own transaction. No torn read, no double-assignment, no lease
    resurrection: if the sweep requeues first (worker_id=NULL), a later progress
    event fails the worker-ownership guard (server.py:1519); if the heartbeat
    renews first, the sweep's in-txn SELECT no longer sees the lease as expired.

  * Two attempts' stale events crossing — SAFE. After requeue+reassign,
    _apply_event rejects the old attempt via worker_id mismatch (403,
    server.py:1519) and/or the attempt-number guard (server.py:1522).

  * Cancel racing finalize on a captain root — SAFE. Both BEGIN IMMEDIATE;
    exactly one wins, the loser sees a terminal state and returns 409. No
    double-notify: _finalize_job requires worker_id IS NULL (server.py:963) and
    _apply_event requires worker ownership — disjoint, so only the winner fires.

  * Schedule tick racing job lifecycle / its own previous beat — SAFE.
    _tick_schedules runs ONLY from the single _sweep_loop (server.py:3734); there
    is no manual-run endpoint, so it never overlaps itself — no double-enqueue.

  * job_logs seq (SELECT MAX(seq)+1) collision across concurrent writers — SAFE.
    All five INSERT-INTO-job_logs sites run inside BEGIN IMMEDIATE, so the
    read-max-then-insert is serialized; concurrent appenders cannot collide.

  * Notify task racing app shutdown — by design. The lifespan cancels only the
    sweeper; in-flight notify tasks are fire-and-forget (server.py:2553) and a
    notification lost on shutdown can never affect job state — acceptable.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roost import server

TOKEN = "hunt5-token"

TERMINAL = ("succeeded", "failed", "cancelled")


@pytest.fixture()
def client(tmp_path: Path):
    db = tmp_path / "roost.db"
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


# ---------- helpers ----------


def _enroll(client: TestClient, name: str = "w1") -> tuple[str, str]:
    token = client.post("/enroll-tokens", json={"label": name}).json()["token"]
    body = client.post(
        "/enroll",
        json={"token": token, "name": name, "capabilities": {"tools": ["python3"]}},
        headers={"Authorization": ""},
    ).json()
    return body["worker_id"], body["credential"]


def _running_job(client: TestClient, worker_id: str, cred: str, spec: dict) -> tuple[str, int]:
    wh = {"Authorization": f"Bearer {cred}"}
    job_id = client.post("/jobs", json=spec).json()["id"]
    attempt = client.get(
        f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh
    ).json()["attempt"]
    client.post(
        f"/workers/{worker_id}/jobs/{job_id}/event",
        json={"type": "started", "attempt": attempt}, headers=wh,
    )
    return job_id, attempt


def _input_state(client: TestClient, job_id: str, input_id: str) -> str:
    for row in client.get(f"/jobs/{job_id}/inputs").json()["inputs"]:
        if row["id"] == input_id:
            return row["state"]
    raise AssertionError("input row vanished")


# ---------- Finding 1a: orphaned input — worker-reported terminal (succeeded/failed) ----------


@pytest.mark.parametrize("terminal", ["succeeded", "failed"])
def test_queued_input_is_resolved_when_job_finishes_before_pickup(
    client: TestClient, terminal: str
):
    """A client queues input on a RUNNING job; the worker reports the job
    terminal BEFORE its next heartbeat pulls the input. The input must NOT be
    left stranded in `queued` — the contract resolves it to `dropped` (the job
    went terminal before pickup). On master it stays `queued` forever."""
    wid, cred = _enroll(client)
    wh = {"Authorization": f"Bearer {cred}"}
    job_id, attempt = _running_job(
        client, wid, cred, {"command": "cat", "requires": {"tools": ["python3"]}}
    )
    input_id = client.post(f"/jobs/{job_id}/input", json={"text": "steer"}).json()["input_id"]

    # The job goes terminal without the worker ever pulling the input.
    client.post(
        f"/workers/{wid}/jobs/{job_id}/event",
        json={"type": terminal, "attempt": attempt,
              "result": {"output": "ok"} if terminal == "succeeded" else None,
              "error": None if terminal == "succeeded" else "boom"},
        headers=wh,
    )
    assert client.get(f"/jobs/{job_id}").json()["state"] == terminal

    # The worker is never asked to deliver it again (heartbeat excludes terminal).
    hb = client.post(f"/workers/{wid}/heartbeat", json={}, headers=wh).json()
    assert job_id not in hb["inputs"]

    state = _input_state(client, job_id, input_id)
    assert state != "queued", (
        f"input stranded in `queued` on a {terminal} job — never delivered, "
        f"never dropped, no worker will ever pull it (heartbeat inputs={hb['inputs']}); "
        "violates R38's 'never silently lost' contract")
    assert state == "dropped"


# ---------- Finding 1b: orphaned input — sweeper lease-expiry failure ----------


def test_queued_input_is_resolved_when_lease_expiry_fails_the_job(
    client: TestClient, tmp_path: Path
):
    """Same stranding via the OTHER terminal path: a leased worker goes silent,
    the sweeper expires the lease and (last attempt) FAILS the job. A queued
    input must be resolved, not left `queued`."""
    wid, cred = _enroll(client)
    db = tmp_path / "roost.db"
    job_id, attempt = _running_job(
        client, wid, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}, "max_attempts": 1},
    )
    input_id = client.post(f"/jobs/{job_id}/input", json={"text": "steer"}).json()["input_id"]

    # Force the lease past its TTL, then sweep: attempt(1) >= max_attempts(1) -> failed.
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", (time.time() - 1, job_id))
    conn.commit()
    conn.close()
    server._sweep(db)
    assert client.get(f"/jobs/{job_id}").json()["state"] == "failed"

    state = _input_state(client, job_id, input_id)
    assert state != "queued", (
        "input stranded in `queued` after the job FAILED on lease expiry — "
        "the sweeper terminated the job but never resolved its pending input")
    assert state == "dropped"


# ---------- Finding 1c: orphaned input — the cancel-vs-queue RACE ----------


def test_input_queued_concurrently_with_cancel_is_never_left_queued(client: TestClient):
    """The concurrency manifestation. A client POSTs input to a running job at
    the same instant the operator cancels it. The two transactions are correctly
    serialized — the input POST either loses (409 'job is terminal') or wins
    (200 'queued'). But when the POST WINS the race, the freshly-queued input is
    orphaned: the job is cancelled microseconds later and nothing ever resolves
    the input. Across trials at least one POST wins-then-is-orphaned; on master
    that orphan stays `queued`."""
    wins_then_orphaned = 0
    trials = 12
    for _ in range(trials):
        wid, cred = _enroll(client)
        job_id, _ = _running_job(
            client, wid, cred, {"command": "cat", "requires": {"tools": ["python3"]}}
        )
        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def _cancel():
            barrier.wait()
            results["cancel"] = client.delete(f"/jobs/{job_id}").status_code

        def _send():
            barrier.wait()
            results["input"] = client.post(f"/jobs/{job_id}/input", json={"text": "m"})

        t1 = threading.Thread(target=_cancel)
        t2 = threading.Thread(target=_send)
        t1.start(); t2.start(); t1.join(); t2.join()

        resp = results["input"]
        if resp.status_code == 200:  # POST won the race; input was accepted
            input_id = resp.json()["input_id"]
            assert client.get(f"/jobs/{job_id}").json()["state"] == "cancelled"
            if _input_state(client, job_id, input_id) == "queued":
                wins_then_orphaned += 1
        else:
            assert resp.status_code == 409  # cancel won — correctly rejected

    assert wins_then_orphaned == 0, (
        f"{wins_then_orphaned}/{trials} inputs were accepted (200 queued) and then "
        "orphaned by a concurrent cancel — a legitimate steering message returns "
        "success to the client and is then silently lost (left `queued` forever). "
        "Cancelling a job as someone steers it must resolve, not strand, the input.")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
