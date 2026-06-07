"""A1 hunt #4 reproductions — captain / steward / verify / watcher seams.

Each test asserts the CORRECT behavior and FAILS on current master because the
bug exists (not a tautology). These are survey evidence, not the eventual
regression tests for the fixes. Run with:

    python -m pytest -q LOOP/repro-a1-hunt4.py

Bugs reproduced (both in the server-side observability seam that the
roost-oversee skill, the web panel, and the MCP inbox all consume via
`/derived` + `/jobs/{id}/derived`):

  C1  `_job_phase` infers the worker's verify/self-heal PHASE from a bare
      substring ("verifying" / "self-healing") of the job's own activity text.
      A genuinely-stuck job whose activity legitimately contains that word
      (e.g. "verifying build artifacts") is mislabeled as phase "verifying",
      which short-circuits `_job_health` BEFORE the idle/stuck check — so the
      overseer never sees "stuck?" and can't intervene.

  C2  `_annotate_liveness` computes `capable_workers` from `requires` matching
      only, ignoring a HARD `target` worker-pin. A job pinned to a worker that
      does not exist can never be placed, yet reports capable_workers >= 1 and
      health "queued"/"waiting" instead of "unplaceable" — the mechanical fact
      the overseer relies on to flag a silently-unplaceable plan.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roost import server

TOKEN = "replenish-hunt4-token"


@pytest.fixture()
def client(tmp_path: Path):
    app = server.create_app(db_path=tmp_path / "roost.db", token=TOKEN, run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _enroll(client: TestClient, name: str, capabilities: dict) -> tuple[str, str]:
    token = client.post("/enroll-tokens", json={"label": "t"}).json()["token"]
    body = client.post(
        "/enroll",
        json={"token": token, "name": name, "capabilities": capabilities},
        headers={"Authorization": ""},
    ).json()
    return body["worker_id"], body["credential"]


# ---------------------------------------------------------------------------
# C1: a stuck job whose activity text contains "verifying" is masked, so the
#     deterministic health verdict never says "stuck?".
# ---------------------------------------------------------------------------


def test_stuck_job_with_verifying_in_activity_is_still_flagged_stuck(client: TestClient):
    worker_id, cred = _enroll(client, "w-exec", {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}

    # An ordinary agent job. Submit, let the worker lease it, mark it running.
    job = client.post(
        "/jobs", json={"intent": "build and verify the release", "verify": False}
    ).json()
    job_id = job["id"]
    assigned = client.get(
        f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh
    ).json()
    assert assigned["id"] == job_id
    client.post(
        f"/jobs/{job_id}/events",
        json={"type": "started", "attempt": assigned["attempt"]},
        headers=wh,
    )

    # The job's LAST sign of life is long ago AND its activity line legitimately
    # mentions "verifying" (a normal step in the job's own work, NOT the worker's
    # trust-loop verify phase). Stamp both directly so idle_sec is well past the
    # STUCK_AFTER threshold deterministically.
    stale = time.time() - (server.STUCK_AFTER + 600)
    with server._connect(client.app.state.db_path) as conn:
        conn.execute(
            "UPDATE jobs SET last_activity_at=?, last_activity=? WHERE id=?",
            (stale, "verifying build artifacts", job_id),
        )

    derived = client.get(f"/jobs/{job_id}/derived").json()
    # The job has had NO activity for >STUCK_AFTER seconds: the deterministic D0
    # verdict MUST flag it so the overseer can intervene. It is masked as
    # "verifying" because _job_phase matched the bare word in the activity text.
    assert derived["health"]["status"] == "stuck?", (
        f"a stuck job was masked as {derived['health']['status']!r} because its "
        f"activity text contains 'verifying' (phase={derived['phase']!r})"
    )


# ---------------------------------------------------------------------------
# C2: a job hard-pinned to a non-existent worker can never run, but is not
#     reported unplaceable because capable_workers ignores the target pin.
# ---------------------------------------------------------------------------


def test_job_pinned_to_nonexistent_target_is_unplaceable(client: TestClient):
    # One real, online worker exists — but the job is hard-pinned to a DIFFERENT,
    # non-existent worker name. Per the placement contract (_try_assign_one), only
    # the named target may ever take it, so this job can NEVER be placed.
    _enroll(client, "alpha", {"tools": ["python3"]})
    job = client.post(
        "/jobs", json={"command": "true", "target": "ghost-worker-does-not-exist"}
    ).json()

    derived = client.get(f"/jobs/{job['id']}/derived").json()
    assert derived["state"] == "queued"
    # The mechanical fact behind a silently-unplaceable plan: no worker can run
    # this job. The overseer reads health == "unplaceable" to flag it. A pin to a
    # worker that does not exist must NOT report a placeable, merely-"queued" job.
    assert derived["health"]["status"] == "unplaceable", (
        f"a job pinned to a non-existent target reported "
        f"{derived['health']['status']!r} with capable_workers="
        f"{derived['capable_workers']!r}; the target pin is ignored when counting "
        f"capable workers"
    )
