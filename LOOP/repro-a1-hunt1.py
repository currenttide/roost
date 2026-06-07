"""A1 hunt (matcher/placement) — reproducing tests. Each must FAIL on
current master to qualify its finding for the backlog (PROTOCOL.md A1)."""
import sys, time
sys.path.insert(0, "/workspace/yang/roost-oss")
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roost import server
from roost.matcher import matches, placement_score

TOKEN = "t"


# --- Finding 1 (matcher): non-numeric cap satisfies numeric != ---

def test_non_numeric_cap_does_not_satisfy_numeric_neq():
    assert matches({"gpu_vram_gb": "N/A"}, {"gpu_vram_gb": "!=0"}) is False
    assert matches({"gpu_vram_gb": "none"}, {"gpu_vram_gb": "!=0"}) is False
    assert matches({"gpu_vram_gb": ""}, {"gpu_vram_gb": "!=0"}) is False


# --- Finding 2 (prefer-by-name): parity with target ---

def test_prefer_by_worker_name_scores_like_prefer_by_id():
    w = {"id": "abc123", "name": "gpu-node", "status": "idle",
         "capabilities": {"cpus": 8}, "capacity": 1}
    by_id = placement_score(w, {}, prefer={"worker": "abc123"}, running=0)
    by_name = placement_score(w, {}, prefer={"worker": "gpu-node"}, running=0)
    assert by_name == by_id  # name must get the same preference bonus


# --- shared scratch CP helpers ---

@pytest.fixture()
def client(tmp_path: Path):
    app = server.create_app(db_path=tmp_path / "r.db", token=TOKEN,
                            run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _worker(client, name):
    et = client.post("/enroll-tokens", json={"label": name}).json()["token"]
    r = client.post("/enroll", json={"token": et, "name": name,
                                     "capabilities": {"tools": ["python3"]}},
                    headers={"Authorization": ""}).json()
    return r["worker_id"], {"Authorization": f"Bearer {r['credential']}"}


# --- Finding 3 (grace bypass): decline+requeue must restart the window ---

def test_decline_requeue_restarts_placement_grace(client, tmp_path):
    wa, ha = _worker(client, "wa")
    job = client.post("/jobs", json={
        "kind": "auto", "task": "t", "requires": {"tools": ["python3"]},
        "prefer": {"worker": "someone-else"}}).json()
    # Age the job past PLACEMENT_GRACE, as a real queue does.
    import sqlite3
    conn = sqlite3.connect(tmp_path / "r.db")
    conn.execute("UPDATE jobs SET created_at = created_at - 10 WHERE id=?",
                 (job["id"],))
    conn.commit(); conn.close()
    a = client.get(f"/workers/{wa}/poll", params={"timeout": 0}, headers=ha).json()
    assert a["id"] == job["id"]  # anti-starvation gave it out — fine
    client.post(f"/workers/{wa}/jobs/{job['id']}/event",
                json={"type": "declined", "attempt": a["attempt"],
                      "reason": "poor fit"}, headers=ha)
    # After requeue the grace window must have RESTARTED: a fresh
    # poll inside the window must NOT bypass competitive placement.
    row = client.get(f"/jobs/{job['id']}").json()
    waited = time.time() - row["created_at"]
    assert waited < server.PLACEMENT_GRACE, (
        f"requeued job already {waited:.1f}s old — grace window permanently "
        "bypassed; placement quality abandoned after one decline")


# --- Finding 4 (attempt budget): declines must not consume retries ---

def test_declines_do_not_consume_the_attempt_budget(client):
    wa, ha = _worker(client, "wa")
    job = client.post("/jobs", json={
        "kind": "auto", "task": "t", "requires": {"tools": ["python3"]}}).json()
    a = client.get(f"/workers/{wa}/poll", params={"timeout": 0}, headers=ha).json()
    client.post(f"/workers/{wa}/jobs/{job['id']}/event",
                json={"type": "declined", "attempt": a["attempt"],
                      "reason": "poor fit"}, headers=ha)
    row = client.get(f"/jobs/{job['id']}").json()
    assert row["state"] == "queued"
    assert row["attempt"] == 0, (
        f"a decline consumed an attempt (attempt={row['attempt']}, "
        f"max={row['max_attempts']}) — real executions get fewer retries")
