"""Recovery-path tests (R118).

Two production-critical scenarios that previously had no coverage:

1. Control-plane RESTART over stale leases: a CP process that comes back up
   over an existing SQLite/WAL database must find the leases that expired
   while it was down and requeue them — with R19 attempt accounting intact
   (a decline refunds its attempt; a lease expiry consumes one).

2. Concurrent assignment: N workers long-poll the same control plane at the
   same instant while exactly one job is queued. Exactly one poll may win the
   job, and the bookkeeping (attempt counter, worker_id, owned-set, worker
   statuses) must come out consistent. Real async requests via httpx's
   ASGITransport + asyncio.gather — no sleeps-as-sync.

Everything is scoped to tmp_path (xdist-safe, R45 precedent) and avoids
monkeypatching module globals so the tests are deterministic under
repetition.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from roost import server


TOKEN = "test-shared-token"


def _admin_client(app) -> TestClient:
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return c


def _enroll(client: TestClient, name: str) -> tuple[str, str]:
    """Enroll a worker over HTTP; return (worker_id, credential).

    The credential round-trips through the DB (cred_hash), so it must keep
    authenticating against a FRESH app instance booted over the same file.
    """
    r = client.post("/enroll-tokens", json={"label": name})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r = client.post(
        "/enroll",
        json={"token": token, "name": name, "capabilities": {"tools": ["python3"]}},
        headers={"Authorization": ""},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["worker_id"], body["credential"]


def _rewind_lease(db: Path, job_id: str, seconds: float) -> None:
    """Simulate ``seconds`` of wall clock passing while the CP is down by
    shifting the job's lease deadline into the past (the suite's established
    alternative to monkeypatching time.time across threads)."""
    with server._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = lease_expires_at - ? WHERE id=?",
            (seconds, job_id),
        )


def _wait_for_state(client: TestClient, job_id: str, state: str,
                    deadline: float = 30.0) -> dict:
    """Bounded wait for a BACKGROUND state transition (the fresh instance's
    sweeper task). Not sleeps-as-sync — the assertion is the converged state,
    observed through the HTTP surface, with a hard deadline."""
    end = time.monotonic() + deadline
    while True:
        j = client.get(f"/jobs/{job_id}").json()
        if j["state"] == state:
            return j
        if time.monotonic() >= end:
            raise AssertionError(
                f"job {job_id} never reached {state!r}; last seen: {j['state']!r} "
                f"(attempt={j.get('attempt')}, error={j.get('error')!r})"
            )
        time.sleep(0.02)


# ---------- (a) CP restart finds + requeues stale leases ----------


def test_cp_restart_sweeps_stale_lease_with_r19_attempt_accounting(tmp_path: Path):
    """Full outage drill over one DB file, two app instances:

    instance 1: w1 is assigned (attempt 1) and DECLINES → requeue refunds the
    attempt (R19: declines never eat the retry budget); w2 takes it (attempt 1,
    again) and reports started, then the CP "crashes" mid-run.

    [outage]: more than LEASE_TTL passes; nothing heartbeats.

    instance 2 (FRESH app over the SAME file): its background sweeper must
    find the stale lease and requeue WITHOUT refunding (a lease expiry is a
    real failure, R52); the decliner set and worker credentials must have
    survived the restart; the retry then exhausts max_attempts → failed.
    """
    db = tmp_path / "roost.db"
    job_id = None

    # ---- instance 1: assign + decline + re-assign + start, then tear down.
    app1 = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with _admin_client(app1) as c1:
        w1, cred1 = _enroll(c1, "w1")
        w2, cred2 = _enroll(c1, "w2")
        r = c1.post("/jobs", json={"command": "sleep 999",
                                   "requires": {"tools": ["python3"]},
                                   "max_attempts": 2})
        assert r.status_code == 200, r.text
        job_id = r.json()["id"]

        # w1 polls first and wins the tie (identical workers → first poller).
        r = c1.get(f"/workers/{w1}/poll", params={"timeout": 0},
                   headers={"Authorization": f"Bearer {cred1}"})
        assert r.status_code == 200, r.text
        assert r.json()["attempt"] == 1

        # w1 declines: requeued AND the attempt slot is refunded (R19).
        r = c1.post(f"/workers/{w1}/jobs/{job_id}/event",
                    json={"type": "declined", "attempt": 1, "reason": "poor fit"},
                    headers={"Authorization": f"Bearer {cred1}"})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "queued"
        assert r.json()["attempt"] == 0  # decline did NOT consume an attempt

        # w2 takes the requeued job — first REAL attempt is still number 1.
        r = c1.get(f"/workers/{w2}/poll", params={"timeout": 0},
                   headers={"Authorization": f"Bearer {cred2}"})
        assert r.status_code == 200, r.text
        assert r.json()["id"] == job_id
        assert r.json()["attempt"] == 1

        r = c1.post(f"/workers/{w2}/jobs/{job_id}/event",
                    json={"type": "started", "attempt": 1},
                    headers={"Authorization": f"Bearer {cred2}"})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "running"
    # CP down. The job is 'running' in the DB with a live-looking lease.

    # ---- outage: > LEASE_TTL elapses while no process owns the DB.
    _rewind_lease(db, job_id, server.LEASE_TTL + 1)

    # ---- instance 2: a FRESH app over the SAME file, real background sweeper.
    app2 = server.create_app(db_path=db, token=TOKEN, run_sweeper=True)
    with _admin_client(app2) as c2:
        # The new instance's sweeper finds the stale lease and requeues it.
        j = _wait_for_state(c2, job_id, "queued")
        # Lease expiry CONSUMES the attempt (no refund — contrast the decline).
        assert j["attempt"] == 1
        assert j["worker_id"] is None

        # State survived the restart: w1's credential still authenticates, and
        # w1 is still a recorded decliner, so it must NOT be offered the job.
        r = c2.get(f"/workers/{w1}/poll", params={"timeout": 0},
                   headers={"Authorization": f"Bearer {cred1}"})
        assert r.status_code == 204

        # w2's credential also survived; it re-grabs the job as attempt 2.
        r = c2.get(f"/workers/{w2}/poll", params={"timeout": 0},
                   headers={"Authorization": f"Bearer {cred2}"})
        assert r.status_code == 200, r.text
        assert r.json()["id"] == job_id
        assert r.json()["attempt"] == 2

        # A second expiry at attempt == max_attempts is terminal: requeue would
        # exceed the budget, so the sweep fails the job instead. Nudge a sweep
        # directly for determinism (the background task may legitimately win
        # the race, so we assert the converged END STATE, never sweep counts).
        _rewind_lease(db, job_id, server.LEASE_TTL + 1)
        server._sweep(db)
        j = _wait_for_state(c2, job_id, "failed")
        assert j["error"] == "lease_expired"
        # Final ledger: 1 refunded decline + 2 consumed real attempts.
        assert j["attempt"] == 2


def test_cp_restart_with_lease_still_live_does_not_requeue(tmp_path: Path):
    """Control case for the drill above: a restart alone must NOT disturb a
    healthy lease — only the passage of LEASE_TTL does. Guards against a
    sweeper regression that treats every in-flight job found at boot as stale.
    """
    db = tmp_path / "roost.db"
    app1 = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with _admin_client(app1) as c1:
        w, cred = _enroll(c1, "w")
        r = c1.post("/jobs", json={"command": "sleep 999",
                                   "requires": {"tools": ["python3"]}})
        job_id = r.json()["id"]
        r = c1.get(f"/workers/{w}/poll", params={"timeout": 0},
                   headers={"Authorization": f"Bearer {cred}"})
        assert r.status_code == 200

    app2 = server.create_app(db_path=db, token=TOKEN, run_sweeper=True)
    with _admin_client(app2) as c2:
        # Force at least one full sweep on the fresh instance, then check the
        # lease was respected. (_sweep is the exact function the loop runs.)
        server._sweep(db)
        j = c2.get(f"/jobs/{job_id}").json()
        assert j["state"] == "assigned"
        assert j["worker_id"] == w
        assert j["attempt"] == 1


# ---------- (b) concurrent polls: exactly-one assignment ----------


def test_concurrent_polls_assign_exactly_once(tmp_path: Path):
    """N simultaneous polls compete for ONE queued job against the real app
    (httpx ASGITransport + asyncio.gather; poll timeout=0 so a loser answers
    204 immediately — no sleeps-as-sync). Exactly one wins; bookkeeping is
    consistent: attempt incremented exactly once, a single owner, losers own
    nothing, and only the winner's status flips to busy."""
    N = 8
    db = tmp_path / "race.db"
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    worker_ids = [
        server._register_worker(db, f"w{i}", {"tools": ["python3"]})["id"]
        for i in range(N)
    ]
    job = server._insert_job(
        db, {"command": "true", "requires": {"tools": ["python3"]}}
    )

    async def race() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://cp",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=30.0,
        ) as client:
            return await asyncio.gather(
                *(
                    client.get(f"/workers/{wid}/poll", params={"timeout": 0})
                    for wid in worker_ids
                )
            )

    responses = asyncio.run(race())

    # Exactly one 200 (the assignment); every other simultaneous poll gets 204.
    by_status = {r.status_code for r in responses}
    assert by_status <= {200, 204}, [r.status_code for r in responses]
    winners = [
        (wid, r) for wid, r in zip(worker_ids, responses) if r.status_code == 200
    ]
    assert len(winners) == 1, (
        f"expected exactly one assignment, got {len(winners)} "
        f"({[r.status_code for r in responses]})"
    )
    winner_id, win = winners[0]
    body = win.json()
    assert body["id"] == job["id"]
    assert body["state"] == "assigned"
    assert body["worker_id"] == winner_id
    # The attempt counter moved exactly once — a double-assignment race would
    # leave attempt == 2 (or a worker_id that doesn't match the 200 winner).
    assert body["attempt"] == 1

    # Durable bookkeeping agrees with the wire responses.
    j = server._get_job(db, job["id"])
    assert j["state"] == "assigned"
    assert j["worker_id"] == winner_id
    assert j["attempt"] == 1
    assert j["lease_expires_at"] is not None

    # Ownership: the job is attributed to exactly the winner.
    for wid in worker_ids:
        owned = server._owned_job_ids(db, wid)
        assert owned == ([job["id"]] if wid == winner_id else [])

    # Status: the winner saturated (default capacity 1) → busy; losers idle.
    statuses = {w["id"]: w["status"] for w in server._list_workers(db)}
    assert statuses[winner_id] == "busy"
    for wid in worker_ids:
        if wid != winner_id:
            assert statuses[wid] == "idle"


def test_concurrent_polls_repeated_rounds_stay_exactly_once(tmp_path: Path):
    """Hammer the same race for several rounds over one app/DB: each round
    queues ONE fresh job, all N workers poll simultaneously, exactly one wins,
    and the winner completes (freeing its slot) before the next round. Catches
    races that only show up once workers carry history (last_assigned_at,
    finished jobs, status churn)."""
    N = 6
    ROUNDS = 4
    db = tmp_path / "race.db"
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    worker_ids = [
        server._register_worker(db, f"w{i}", {"tools": ["python3"]})["id"]
        for i in range(N)
    ]

    async def run_rounds() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://cp",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=30.0,
        ) as client:
            for rnd in range(ROUNDS):
                job = server._insert_job(
                    db, {"command": "true", "requires": {"tools": ["python3"]}}
                )
                responses = await asyncio.gather(
                    *(
                        client.get(f"/workers/{wid}/poll", params={"timeout": 0})
                        for wid in worker_ids
                    )
                )
                winners = [
                    (wid, r)
                    for wid, r in zip(worker_ids, responses)
                    if r.status_code == 200
                ]
                assert len(winners) == 1, (
                    f"round {rnd}: expected exactly one assignment, got "
                    f"{[r.status_code for r in responses]}"
                )
                assert all(
                    r.status_code == 204
                    for _, r in zip(worker_ids, responses)
                    if r.status_code != 200
                )
                winner_id, win = winners[0]
                assert win.json()["id"] == job["id"]
                assert win.json()["attempt"] == 1
                # Winner finishes the job, freeing its slot for the next round.
                r = await client.post(
                    f"/workers/{winner_id}/jobs/{job['id']}/event",
                    json={"type": "succeeded", "attempt": 1, "exit_code": 0},
                )
                assert r.status_code == 200, r.text
                assert r.json()["state"] == "succeeded"

    asyncio.run(run_rounds())

    # After all rounds: every job terminal, nothing double-booked.
    with server._connect(db) as conn:
        rows = conn.execute("SELECT state, attempt FROM jobs").fetchall()
    assert len(rows) == ROUNDS
    assert all(r["state"] == "succeeded" and r["attempt"] == 1 for r in rows)
