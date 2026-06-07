"""A1 hunt #7 (the long-idle gate) — server↔mobile CONTRACT-correctness repros.

Lens: can the SERVER emit responses that violate mobile-app/API.md? The client
decode layers are pure + golden-tested; this hunt walks the SERVER serializers
(_derive_run / _goal_text and friends) against unusual/degenerate rows.

Each test asserts the CONTRACT-COMPLIANT outcome and must FAIL on current master
for the stated reason (PROTOCOL.md A1). These are survey evidence, not the
eventual regression tests for the fixes.

Root cause (shared family): the §2 run row documents `goal`/`result` as STRINGS,
but `_derive_run` reaches them via `" ".join(...)` / `[:N]` without proving the
underlying field is a `str`. A field that is a non-string (a `list[int]` command
from a mobile submit; a structured `result.output` from a non-conformant worker)
makes the slice/join raise, so the mobile DASHBOARD endpoints `GET /derived`
(polled every 2 s, §2) and `GET /jobs/{id}/derived` (§4 session header) return
**500** instead of the documented run shape — and because `/derived` iterates the
whole page, ONE poisoned job breaks the dashboard for EVERY job.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "/workspace/yang/roost-oss")

from roost import server

TOKEN = "replenish-hunt7-token"


@pytest.fixture()
def client(tmp_path: Path):
    app = server.create_app(db_path=tmp_path / "roost.db", token=TOKEN,
                            run_sweeper=False)
    # raise_server_exceptions=False so a handler 500 surfaces as a 500 response
    # (what a mobile client actually sees) instead of re-raising into the test.
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _enroll_worker(client: TestClient, name: str = "w1") -> tuple[str, dict]:
    token = client.post("/enroll-tokens", json={"label": name}).json()["token"]
    r = client.post(
        "/enroll",
        json={"token": token, "name": name, "capabilities": {"tools": ["python3"]}},
        headers={"Authorization": ""},
    ).json()
    return r["worker_id"], {"Authorization": f"Bearer {r['credential']}"}


# --- Finding 1 (mobile-reachable): a non-string `command` 500s the dashboard ---
#
# JobSubmit.command is typed `Optional[Any]` (to accept "str | list[str]"), so a
# mobile/client `POST /jobs` carrying `command: [1, 2, 3]` is ACCEPTED (200) and
# stored verbatim. _goal_text then does `" ".join([1, 2, 3])` → TypeError, so the
# very next dashboard poll 500s. This is squarely server↔mobile contract: a
# mobile submit the server accepts makes the server's own mobile dashboard
# un-renderable. (`intent`/`task` are typed `Optional[str]` and reject this with
# 422 — `command` is the one text field that flows into _goal_text untyped.)

def test_non_string_command_does_not_500_the_dashboard(client: TestClient):
    r = client.post("/jobs", json={"kind": "command", "command": [1, 2, 3]})
    assert r.status_code == 200, r.text  # server accepts the submit
    job_id = r.json()["id"]

    d = client.get("/derived")
    assert d.status_code == 200, (
        "GET /derived (the mobile dashboard, §2, polled every 2 s) 500s because "
        "_goal_text does \" \".join() over a non-string command — a single such "
        "job makes the whole dashboard un-renderable for every job"
    )
    # §2 run shape: `goal` must be a string (display title ≤140 chars).
    run = next(r for r in d.json()["runs"] if r["run_id"] == job_id)
    assert isinstance(run["goal"], str)

    sd = client.get(f"/jobs/{job_id}/derived")
    assert sd.status_code == 200, (
        "GET /jobs/{id}/derived (§4 session header) 500s on the same job"
    )


# --- Finding 2 (worker-induced): a structured result.output 500s the dashboard --
#
# JobEvent.result is typed `Any`. A worker reporting `succeeded` with
# `result: {"output": {...}}` (a structured, non-string output) is ACCEPTED (200)
# and stored. _derive_run then evaluates `(res.get("output") or ...)[:240]` and the
# dict slice raises, so GET /derived 500s. The honest worker always coerces output
# to str, so this needs a NON-CONFORMANT worker — but it is reachable through the
# documented worker event API, and the contract-violation surface (a 500 from the
# mobile dashboard instead of the §2 run array) is mobile-facing. GET /jobs/{id}
# (raw detail) survives because it does not call _derive_run.

def test_structured_worker_result_output_does_not_500_the_dashboard(client: TestClient):
    wid, wh = _enroll_worker(client)
    job = client.post(
        "/jobs",
        json={"kind": "command", "command": "cat", "requires": {"tools": ["python3"]}},
    ).json()
    job_id = job["id"]
    lease = client.get(f"/workers/{wid}/poll", params={"timeout": 0}, headers=wh)
    assert lease.status_code == 200, lease.text
    attempt = lease.json()["attempt"]

    ev = client.post(
        f"/workers/{wid}/jobs/{job_id}/event",
        json={"type": "succeeded", "attempt": attempt,
              "result": {"output": {"summary": "done", "files": 3}, "verified": True}},
        headers=wh,
    )
    assert ev.status_code == 200, ev.text  # server accepts the worker report

    # Raw detail survives (no _derive_run); the dashboard must too.
    assert client.get(f"/jobs/{job_id}").status_code == 200
    d = client.get("/derived")
    assert d.status_code == 200, (
        "GET /derived 500s because _derive_run slices a non-string result.output "
        "(`(res.get('output') or ...)[:240]`) — the §2 run row documents `result` "
        "as a string, so a structured output must be coerced, not crash the page"
    )
    run = next(r for r in d.json()["runs"] if r["run_id"] == job_id)
    assert isinstance(run["result"], str)
