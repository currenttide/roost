"""Server-side V1 flow tests.

Exercise the control plane through its HTTP surface (TestClient) for the
enrollment + job-lifecycle path, and through the internal helpers for the
hierarchy guardrails (depth, tree budget, subtree cancel) that roost-mcp
relies on.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roost import server


TOKEN = "test-shared-token"


@pytest.fixture()
def client(tmp_path: Path):
    db = tmp_path / "roost.db"
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _enroll_worker(client: TestClient, capabilities: dict) -> tuple[str, str]:
    """Mint a token, enroll, return (worker_id, credential)."""
    r = client.post("/enroll-tokens", json={"label": "test"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    # Enrollment itself is unauthenticated (the token IS the auth).
    r = client.post(
        "/enroll",
        json={"token": token, "name": "w1", "capabilities": capabilities},
        headers={"Authorization": ""},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["worker_id"], body["credential"]


def _enroll_named(
    client: TestClient, name: str, capabilities: dict | None = None
) -> tuple[str, str]:
    """Enroll a worker with an explicit name; return (worker_id, credential)."""
    r = client.post("/enroll-tokens", json={"label": "test"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r = client.post(
        "/enroll",
        json={"token": token, "name": name, "capabilities": capabilities or {}},
        headers={"Authorization": ""},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["worker_id"], body["credential"]


# ---------- Enrollment + lifecycle (V1 criterion 1) ----------


def test_enroll_token_single_use(client: TestClient):
    r = client.post("/enroll-tokens", json={"label": "once"})
    token = r.json()["token"]
    ok = client.post("/enroll", json={"token": token, "name": "a", "capabilities": {}})
    assert ok.status_code == 200
    again = client.post("/enroll", json={"token": token, "name": "b", "capabilities": {}})
    assert again.status_code == 403  # already used


def test_enroll_requires_admin_to_mint(client: TestClient):
    # Worker credential is not admin → cannot mint enroll tokens.
    _, cred = _enroll_worker(client, {"tools": ["python3"]})
    r = client.post(
        "/enroll-tokens", json={"label": "x"},
        headers={"Authorization": f"Bearer {cred}"},
    )
    assert r.status_code == 403


def test_command_job_full_lifecycle(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"], "cpus": 4})
    wh = {"Authorization": f"Bearer {cred}"}

    # Submit a plain command job.
    r = client.post("/jobs", json={"command": "echo hi", "requires": {"tools": ["python3"]}})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    assert r.json()["state"] == "queued"
    assert r.json()["root_job_id"] == job_id  # self-rooted

    # Worker polls and gets it.
    r = client.get(f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh)
    assert r.status_code == 200, r.text
    assigned = r.json()
    assert assigned["id"] == job_id
    assert assigned["state"] == "assigned"
    attempt = assigned["attempt"]
    assert attempt == 1

    # Worker reports started → running.
    r = client.post(
        f"/workers/{worker_id}/jobs/{job_id}/event",
        json={"type": "started", "attempt": attempt}, headers=wh,
    )
    assert r.status_code == 200
    assert r.json()["state"] == "running"

    # Logs.
    client.post(
        f"/workers/{worker_id}/jobs/{job_id}/logs",
        json={"stream": "stdout", "data": "hi"}, headers=wh,
    )

    # Worker reports success with token usage.
    r = client.post(
        f"/workers/{worker_id}/jobs/{job_id}/event",
        json={"type": "succeeded", "attempt": attempt, "exit_code": 0, "tokens_used": 123},
        headers=wh,
    )
    assert r.status_code == 200
    final = r.json()
    assert final["state"] == "succeeded"
    assert final["exit_code"] == 0
    assert final["tokens_used"] == 123

    # Logs endpoint reflects the stdout line.
    r = client.get(f"/jobs/{job_id}/logs")
    streams = {log["stream"] for log in r.json()["logs"]}
    assert "stdout" in streams


def test_capability_mismatch_stays_queued(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    client.post("/jobs", json={"command": "true", "requires": {"gpu_vram_gb": ">=99999"}})
    r = client.get(f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh)
    assert r.status_code == 204  # no match → nothing assigned
    # A 204 must carry no body — a JSON-serialised None would overrun Content-Length
    # and make uvicorn raise on every idle poll.
    assert r.content == b""


def test_revoke_blocks_worker(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    # Works before revoke.
    assert client.post(f"/workers/{worker_id}/heartbeat", json={}, headers=wh).status_code == 200
    # Admin revokes.
    assert client.delete(f"/workers/{worker_id}").status_code == 200
    # Credential no longer authenticates.
    assert client.post(f"/workers/{worker_id}/heartbeat", json={}, headers=wh).status_code == 401


# ---------- Hierarchy guardrails (V1 criteria 3 & 4) ----------


def test_depth_limit_enforced(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    root = server._insert_job(db, {"command": "true", "hierarchy": {"max_depth": 1}})
    child = server._insert_job(db, {"command": "true", "hierarchy": {"can_dispatch": True}}, parent=root)
    assert child["depth"] == 1
    # Grandchild would be depth 2 > max_depth 1 → refused.
    with pytest.raises(ValueError, match="max_depth"):
        server._insert_job(db, {"command": "true"}, parent=child)


def test_tree_budget_enforced(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    root = server._insert_job(db, {"command": "true", "budget": {"tree_max_tokens": 1000}})
    assert root["tree_budget_tokens"] == 1000
    # Child requesting within budget is fine.
    server._insert_job(db, {"command": "true", "budget": {"max_tokens": 600}}, parent=root)
    # Spend some of the root budget, then a child that would breach it is refused.
    with server._connect(db) as conn:
        conn.execute("UPDATE jobs SET tree_budget_spent=900 WHERE id=?", (root["id"],))
    with pytest.raises(ValueError, match="tree budget"):
        server._insert_job(db, {"command": "true", "budget": {"max_tokens": 200}}, parent=root)


def test_captain_root_anchors_tree_and_budget(client: TestClient):
    # roost dispatch creates a captain-root: running, can_dispatch, tree budget.
    r = client.post("/jobs", json={
        "intent": "do a multi-step plan", "kind": "captain", "captain_root": True,
        "hierarchy": {"can_dispatch": True, "max_depth": 3},
        "budget": {"max_tokens": 1000},
    })
    assert r.status_code == 200, r.text
    root = r.json()
    assert root["state"] == "running"          # never queued; no worker pulls it
    assert root["worker_id"] is None
    assert root["tree_budget_tokens"] == 1000
    assert root["root_job_id"] == root["id"]

    # A sub-job dispatched under it shares the tree and draws on the budget.
    r = client.post("/jobs", json={
        "command": "true", "parent_job_id": root["id"], "budget": {"max_tokens": 600},
    })
    assert r.status_code == 200, r.text
    child = r.json()
    assert child["root_job_id"] == root["id"]
    assert child["depth"] == 1

    # A second sub-job that would breach the remaining budget is refused (409).
    with server._connect(client.app.state.db_path) as conn:
        conn.execute("UPDATE jobs SET tree_budget_spent=900 WHERE id=?", (root["id"],))
    r = client.post("/jobs", json={
        "command": "true", "parent_job_id": root["id"], "budget": {"max_tokens": 200},
    })
    assert r.status_code == 409
    assert "tree budget" in r.text

    # The tree view shows root + child.
    ids = {j["id"] for j in client.get(f"/jobs/{root['id']}/tree").json()}
    assert root["id"] in ids and child["id"] in ids


def test_captain_plan_reason_recorded_on_child_spec(client: TestClient):
    """R33: a sub-job's `reason` (the captain's 'why') persists in the child spec
    and surfaces verbatim in the tree, so `roost tree` can render plan intent."""
    root = client.post("/jobs", json={
        "intent": "split a goal", "captain_root": True,
        "hierarchy": {"can_dispatch": True, "max_depth": 3},
    }).json()
    # A child WITH a reason — note it is stripped/collapsed to one line on store.
    child = client.post("/jobs", json={
        "command": "pytest -q", "parent_job_id": root["id"],
        "reason": "  run the suite first\n(everything else depends on it)  ",
    }).json()
    assert child["spec"]["reason"] == "run the suite first (everything else depends on it)"

    # The reason is durable: it round-trips through the tree endpoint (what
    # `roost tree` reads), not just the immediate submit response.
    tree = {j["id"]: j for j in client.get(f"/jobs/{root['id']}/tree").json()}
    assert tree[child["id"]]["spec"]["reason"] == \
        "run the suite first (everything else depends on it)"


def test_job_without_reason_carries_no_plan_annotation(client: TestClient):
    """Graceful absence: a job submitted without a reason (all older/non-captain
    jobs) stores no `reason` key — the tree renderer then shows it exactly as today."""
    job = client.post("/jobs", json={"command": "true"}).json()
    assert "reason" not in job["spec"]
    # A blank/whitespace reason is treated as absent too (no empty annotation).
    blank = client.post("/jobs", json={"command": "true", "reason": "   \n  "}).json()
    assert "reason" not in blank["spec"]


def test_plan_reason_is_clamped(client: TestClient):
    """The plan reason is a one-liner, not prose: an oversize reason is clamped so
    it can never bloat the stored spec blob."""
    job = client.post("/jobs", json={
        "command": "true", "reason": "x" * (server.PLAN_REASON_MAX_CHARS + 200),
    }).json()
    assert len(job["spec"]["reason"]) == server.PLAN_REASON_MAX_CHARS


def test_finalize_captain_root(client: TestClient):
    root = client.post("/jobs", json={
        "intent": "plan", "captain_root": True,
        "hierarchy": {"can_dispatch": True},
    }).json()
    assert root["state"] == "running"
    r = client.post(f"/jobs/{root['id']}/finalize", json={"state": "succeeded"})
    assert r.status_code == 200
    assert r.json()["state"] == "succeeded"
    # Finalizing again (now terminal) is refused.
    assert client.post(f"/jobs/{root['id']}/finalize", json={"state": "failed"}).status_code == 409


def test_finalize_refuses_worker_owned_job(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    client.post("/jobs", json={"command": "true", "requires": {"tools": ["python3"]}})
    assigned = client.get(f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh).json()
    # A worker-owned job cannot be finalized out from under the worker.
    r = client.post(f"/jobs/{assigned['id']}/finalize", json={"state": "succeeded"})
    assert r.status_code == 409


def test_subtree_cancel(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    root = server._insert_job(db, {"command": "true", "hierarchy": {"can_dispatch": True, "max_depth": 5}})
    child = server._insert_job(db, {"command": "true", "hierarchy": {"can_dispatch": True}}, parent=root)
    grandchild = server._insert_job(db, {"command": "true"}, parent=child)
    n = server._cancel_job(db, root["id"], cascade=True)
    assert n == 3
    for jid in (root["id"], child["id"], grandchild["id"]):
        assert server._get_job(db, jid)["state"] == "cancelled"


# ---------- Placement ranking (V2-2 / V2-4) ----------


def test_load_aware_picks_lower_loaded_worker(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    busy = server._register_worker(
        db, "busy", {"tools": ["python3"], "load": {"running": 0, "loadavg1": 8.0}}
    )
    free = server._register_worker(
        db, "free", {"tools": ["python3"], "load": {"running": 0, "loadavg1": 0.1}}
    )
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    # The higher-loaded worker polls first but should defer to the better fit.
    assert server._try_assign_one(db, busy["id"]) is None
    got = server._try_assign_one(db, free["id"])
    assert got is not None and got["worker_id"] == free["id"]


def test_prefer_hint_routes_to_named_worker(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    w1 = server._register_worker(db, "w1", {"tools": ["python3"]})
    w2 = server._register_worker(db, "w2", {"tools": ["python3"]})
    server._insert_job(
        db,
        {"command": "true", "requires": {"tools": ["python3"]}, "prefer": {"worker": w2["id"]}},
    )
    # Non-preferred worker polls first → defers within the grace window.
    assert server._try_assign_one(db, w1["id"]) is None
    got = server._try_assign_one(db, w2["id"])
    assert got is not None and got["worker_id"] == w2["id"]


def test_grace_window_prevents_starvation(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    w1 = server._register_worker(db, "w1", {"tools": ["python3"]})
    server._register_worker(db, "w2", {"tools": ["python3"]})
    job = server._insert_job(
        db,
        {"command": "true", "requires": {"tools": ["python3"]}, "prefer": {"worker": "nonexistent"}},
    )
    # Age the job past the grace window; any capable worker may now take it.
    with server._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET created_at=? WHERE id=?",
            (job["created_at"] - server.PLACEMENT_GRACE - 1, job["id"]),
        )
    got = server._try_assign_one(db, w1["id"])
    assert got is not None and got["worker_id"] == w1["id"]


def test_independent_jobs_spread_across_idle_workers(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    w1 = server._register_worker(db, "w1", {"tools": ["python3"]})
    w2 = server._register_worker(db, "w2", {"tools": ["python3"]})
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    a = server._try_assign_one(db, w1["id"])  # w1 takes one, becomes busy
    b = server._try_assign_one(db, w2["id"])  # w2 takes the other (w1 now busy)
    assert a is not None and b is not None
    assert {a["worker_id"], b["worker_id"]} == {w1["id"], w2["id"]}


def test_lease_expiry_requeues_then_fails(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    worker = server._register_worker(db, "w", {"tools": ["python3"]})
    job = server._insert_job(db, {"command": "true"})
    # Assign + force an expired lease.
    assigned = server._try_assign_one(db, worker["id"])
    assert assigned["id"] == job["id"]
    with server._connect(db) as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=1 WHERE id=?", (job["id"],))
    counts = server._sweep(db)
    assert counts["requeued"] == 1
    assert server._get_job(db, job["id"])["state"] == "queued"
    # Second assignment + expiry exhausts max_attempts (default 2) → failed.
    server._try_assign_one(db, worker["id"])
    with server._connect(db) as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=1 WHERE id=?", (job["id"],))
    counts = server._sweep(db)
    assert counts["failed_attempts"] == 1
    failed = server._get_job(db, job["id"])
    assert failed["state"] == "failed"
    assert failed["error"] == "lease_expired"


def test_liveness_capable_workers_fact(client: TestClient):
    # A queued job that no online worker can satisfy reports capable_workers=0 —
    # the mechanical fact behind a silently-unplaceable plan.
    _enroll_worker(client, {"tools": ["python3"], "hostname": "boxA"})
    r = client.post("/jobs", json={"command": "true", "requires": {"hostname": "==nope"}})
    assert r.status_code == 200
    jid = r.json()["id"]
    j = client.get(f"/jobs/{jid}").json()
    assert j["state"] == "queued"
    assert j["capable_workers"] == 0
    assert "queued_sec" in j
    # A pin that matches an online worker is placeable.
    r2 = client.post("/jobs", json={"command": "true", "requires": {"hostname": "==boxA"}})
    j2 = client.get(f"/jobs/{r2.json()['id']}").json()
    assert j2["capable_workers"] == 1


# ---------- [R3] lease reconciliation: owned-jobs heartbeat signal ----------


def test_owned_job_ids_tracks_lease(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    w = server._register_worker(db, "w", {"tools": ["python3"]})["id"]
    job = server._insert_job(db, {"command": "true"})
    assert server._owned_job_ids(db, w) == []
    assigned = server._try_assign_one(db, w)
    assert assigned["id"] == job["id"]
    assert server._owned_job_ids(db, w) == [job["id"]]
    # Lease expires during a CP outage; the sweeper requeues → no longer ours.
    with server._connect(db) as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=1 WHERE id=?", (job["id"],))
    server._sweep(db)
    assert server._owned_job_ids(db, w) == []


def test_heartbeat_endpoint_returns_owned(client: TestClient):
    wid, cred = _enroll_worker(client, {"tools": ["python3"]})
    r = client.post("/jobs", json={"command": "true"})
    jid = r.json()["id"]
    # Lease it to the worker, then heartbeat: response must list it as owned.
    r = client.get(f"/workers/{wid}/poll", params={"timeout": 0.0},
                   headers={"Authorization": f"Bearer {cred}"})
    assert r.status_code == 200 and r.json()["id"] == jid
    r = client.post(f"/workers/{wid}/heartbeat", json={},
                    headers={"Authorization": f"Bearer {cred}"})
    assert r.status_code == 200
    body = r.json()
    assert body["owned"] == [jid]
    assert body["cancel"] == []


def test_outage_past_ttl_requeues_and_rejects_stale_reporter(tmp_path: Path):
    """Simulates a CP outage past LEASE_TTL: w1's lease expires (no heartbeats
    land), the sweeper requeues, w2 wins the new lease. On reconnect w1 must see
    an empty owned set, and its stale terminal report must be rejected."""
    db = tmp_path / "roost.db"
    server._init_db(db)
    w1 = server._register_worker(db, "w1", {"tools": ["python3"]})["id"]
    w2 = server._register_worker(db, "w2", {"tools": ["python3"]})["id"]
    job = server._insert_job(db, {"command": "sleep 99", "max_attempts": 3})
    a1 = server._try_assign_one(db, w1)
    assert a1["id"] == job["id"] and a1["attempt"] == 1
    # Outage: w1 unreachable — its lease lapses and its liveness goes stale →
    # offline; the sweeper requeues the job.
    with server._connect(db) as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=1 WHERE id=?", (job["id"],))
        conn.execute("UPDATE workers SET last_seen=1 WHERE id=?", (w1,))
    assert server._sweep(db)["requeued"] == 1
    # w2 polls first and wins the new lease (attempt 2).
    a2 = server._try_assign_one(db, w2)
    assert a2 is not None and a2["id"] == job["id"] and a2["attempt"] == 2
    # w1 reconnects: heartbeat owned-set no longer lists the job → it aborts.
    assert server._owned_job_ids(db, w1) == []
    assert server._owned_job_ids(db, w2) == [job["id"]]
    # And if w1's orphaned attempt still tried to report, it is rejected.
    with pytest.raises(PermissionError):
        server._apply_event(db, job["id"], w1,
                            {"type": "succeeded", "attempt": 1, "exit_code": 0})
    assert server._get_job(db, job["id"])["state"] == "assigned"  # w2's, untouched


def test_stale_worker_recovers_to_idle_on_heartbeat(tmp_path: Path):
    # Regression: a worker the sweeper marked 'stale' must return to 'idle'
    # once it heartbeats again — not stay stale forever.
    db = tmp_path / "r.db"
    server._init_db(db)
    wid = server._register_worker(db, "wkr", {"tools": ["python3"]})["id"]
    with server._connect(db) as conn:
        conn.execute("UPDATE workers SET status='stale' WHERE id=?", (wid,))
    assert server._heartbeat_worker(db, wid, None) is True
    w = next(x for x in server._list_workers(db) if x["id"] == wid)
    assert w["status"] == "idle"


def test_enroll_provisions_claude_auth(client: TestClient, monkeypatch):
    # With provisioning on (default) and host creds present, the enroll response
    # carries onboarding: install hint + copied credentials.
    monkeypatch.setattr(server, "_read_host_claude_creds", lambda: '{"claudeAiOauth":"x"}')
    r = client.post("/enroll-tokens", json={"label": "p"})
    token = r.json()["token"]
    r = client.post("/enroll", json={"token": token, "name": "w", "capabilities": {}})
    ob = r.json()["onboarding"]
    assert ob["install_claude"] is True and ob["install_cmd"]
    assert ob["auth"]["method"] == "copy"
    assert ob["auth"]["credentials_json"] == '{"claudeAiOauth":"x"}'


def test_enroll_token_can_opt_out_of_provisioning(client: TestClient, monkeypatch):
    monkeypatch.setattr(server, "_read_host_claude_creds", lambda: '{"claudeAiOauth":"x"}')
    r = client.post("/enroll-tokens", json={"label": "np", "policy": {"provision_claude": False}})
    token = r.json()["token"]
    r = client.post("/enroll", json={"token": token, "name": "w2", "capabilities": {}})
    assert "onboarding" not in r.json()


def test_revoked_worker_does_not_recover_on_heartbeat(tmp_path: Path):
    # A revoked worker must stay offline even if it keeps heartbeating.
    db = tmp_path / "r.db"
    server._init_db(db)
    wid = server._register_worker(db, "w", {"tools": ["python3"]}, None, "credhash")["id"]
    assert server._revoke_worker(db, wid) is True
    server._heartbeat_worker(db, wid, None)
    w = next(x for x in server._list_workers(db) if x["id"] == wid)
    assert w["status"] == "offline"


def test_cancel_backchannel_and_terminal_guard(client: TestClient):
    # Cancel reaches a running job via the heartbeat back-channel, and a late
    # worker event can't relabel the cancelled job.
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    r = client.post("/jobs", json={"command": "sleep 1", "requires": {"tools": ["python3"]}})
    jid = r.json()["id"]
    client.get(f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh)
    client.post(f"/workers/{worker_id}/jobs/{jid}/event",
                json={"type": "started", "attempt": 1}, headers=wh)
    # Cancel it.
    assert client.delete(f"/jobs/{jid}").status_code == 200
    # Heartbeat tells the worker to kill it.
    hb = client.post(f"/workers/{worker_id}/heartbeat", json={"capabilities": {}}, headers=wh)
    assert jid in hb.json().get("cancel", [])
    # A late terminal event is ignored; the job stays cancelled.
    ev = client.post(f"/workers/{worker_id}/jobs/{jid}/event",
                     json={"type": "succeeded", "attempt": 1, "exit_code": 0}, headers=wh)
    assert ev.status_code == 409
    assert client.get(f"/jobs/{jid}").json()["state"] == "cancelled"


def test_claude_creds_refresh_endpoint(client: TestClient, monkeypatch):
    # A worker can pull current creds to refresh its stale local copy.
    monkeypatch.setattr(server, "_read_host_claude_creds", lambda: '{"claudeAiOauth":"fresh"}')
    _, cred = _enroll_worker(client, {"tools": ["claude"]})
    wh = {"Authorization": f"Bearer {cred}"}
    r = client.get("/claude-creds", headers=wh)
    assert r.status_code == 200
    assert r.json()["credentials_json"] == '{"claudeAiOauth":"fresh"}'
    # Unauthenticated (no worker cred) is rejected.
    assert client.get("/claude-creds", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_cancel_does_not_free_worker_that_moved_on(tmp_path: Path):
    # Cancelling an already-finished job must NOT idle the worker that has since
    # moved on to a different running job (would allow double-assignment).
    db = tmp_path / "r.db"
    server._init_db(db)
    wid = server._register_worker(db, "w", {"tools": ["python3"]})["id"]
    now = 1000.0
    with server._connect(db) as conn:
        conn.execute("INSERT INTO jobs(id,spec,created_at,state,worker_id,finished_at) "
                     "VALUES('j1','{}',?, 'succeeded',?,?)", (now, wid, now))
        conn.execute("INSERT INTO jobs(id,spec,created_at,state,worker_id) "
                     "VALUES('j2','{}',?, 'running',?)", (now, wid))
        conn.execute("UPDATE workers SET status='busy' WHERE id=?", (wid,))
    server._cancel_job(db, "j1", False)
    w = next(x for x in server._list_workers(db) if x["id"] == wid)
    assert w["status"] == "busy"   # still running j2


def test_heartbeat_keeps_busy_worker_busy_after_stale(tmp_path: Path):
    # A busy worker the sweeper marked 'stale' must recover to 'busy' (it still
    # owns an in-flight job), not 'idle'.
    db = tmp_path / "r.db"
    server._init_db(db)
    wid = server._register_worker(db, "w", {"tools": ["python3"]})["id"]
    with server._connect(db) as conn:
        conn.execute("INSERT INTO jobs(id,spec,created_at,state,worker_id) "
                     "VALUES('j1','{}',1.0,'running',?)", (wid,))
        conn.execute("UPDATE workers SET status='stale' WHERE id=?", (wid,))
    server._heartbeat_worker(db, wid, None)
    w = next(x for x in server._list_workers(db) if x["id"] == wid)
    assert w["status"] == "busy"


# ---------- Bare-worker (kind: auto) self-selection — plan.md Phase 1 ----------


def _age_job(db: Path, job_id: str, *, seconds: float) -> None:
    """Backdate a job's created_at so it's past the placement grace window — makes
    the *poller* take it regardless of fit, isolating decline/requeue from the
    (separately-tested) grace-window scoring."""
    import sqlite3
    import time as _time
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE jobs SET created_at=? WHERE id=?",
                     (_time.time() - seconds, job_id))
        conn.commit()


def test_auto_job_decline_requeues_and_skips_decliner(tmp_path: Path):
    """A worker that self-declines a kind:auto task requeues it, and the decliner is
    skipped on its next poll (within the backoff) so a better-fit node can take it."""
    db = tmp_path / "roost.db"
    server._init_db(db)
    cpu = server._register_worker(db, "cpu", {"tools": ["claude"], "cpus": 4})
    gpu = server._register_worker(
        db, "gpu", {"tools": ["claude"], "cpus": 32, "gpu_count": 1, "gpu_vram_gb": 80})
    jid = server._insert_job(
        db, {"kind": "auto", "task": "train a model on a GPU", "requires": {}})["id"]
    # Aged past the grace window but within the decline backoff (30s).
    _age_job(db, jid, seconds=5)

    # CPU box happens to be free and grabs it (empty requires → matches anyone).
    got = server._try_assign_one(db, cpu["id"])
    assert got is not None and got["worker_id"] == cpu["id"]

    # It judges itself a poor fit and declines.
    job, accepted = server._apply_event(
        db, jid, cpu["id"],
        {"type": "declined", "attempt": got["attempt"], "reason": "no GPU on this node"},
    )
    assert accepted
    assert job["state"] == "queued"               # requeued, not failed
    assert job["worker_id"] is None

    # The decliner is skipped on its next poll; the GPU box can take it.
    assert server._try_assign_one(db, cpu["id"]) is None
    got2 = server._try_assign_one(db, gpu["id"])
    assert got2 is not None and got2["worker_id"] == gpu["id"]


def test_auto_job_escalates_after_max_declines(tmp_path: Path):
    """An impossible kind:auto task that every node declines escalates to failed
    rather than bouncing forever."""
    db = tmp_path / "roost.db"
    server._init_db(db)
    workers = [server._register_worker(db, f"w{i}", {"tools": ["claude"]}) for i in range(3)]
    jid = server._insert_job(
        db, {"kind": "auto", "task": "do the impossible", "requires": {}})["id"]
    _age_job(db, jid, seconds=5)

    final = None
    for w in workers:  # 3 distinct nodes each decline once → hits MAX_DECLINES
        got = server._try_assign_one(db, w["id"])
        assert got is not None, f"{w['id']} should be able to grab the requeued task"
        final, _ = server._apply_event(
            db, jid, w["id"],
            {"type": "declined", "attempt": got["attempt"], "reason": "nope"},
        )
    assert final["state"] == "failed"
    assert "declined" in (final["error"] or "")


def test_auto_job_does_not_escalate_while_capable_node_is_busy(tmp_path: Path):
    """The Phase-1 bug: a kind:auto task must NOT be failed just because the node that
    grabbed it declined — if another capable node exists (even busy), it stays queued."""
    import sqlite3
    db = tmp_path / "roost.db"
    server._init_db(db)
    cpu = server._register_worker(db, "cpu", {"tools": ["claude"], "cpus": 4})
    gpu = server._register_worker(
        db, "gpu", {"tools": ["claude"], "cpus": 32, "gpu_count": 1, "gpu_vram_gb": 80})
    # gpu is busy on something else (capable but not free) — must still block escalation.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workers SET status='busy' WHERE id=?", (gpu["id"],))
        conn.commit()
    jid = server._insert_job(db, {"kind": "auto", "task": "needs a GPU", "requires": {}})["id"]
    _age_job(db, jid, seconds=5)

    got = server._try_assign_one(db, cpu["id"])
    assert got is not None and got["worker_id"] == cpu["id"]
    job, _ = server._apply_event(
        db, jid, cpu["id"], {"type": "declined", "attempt": got["attempt"], "reason": "no GPU"})
    # NOT failed — the busy GPU node hasn't declined, so the task waits for it.
    assert job["state"] == "queued"
    assert job["decline_count"] == 1
    # cpu (the decliner) is now permanently skipped for this job.
    assert server._try_assign_one(db, cpu["id"]) is None


def test_triage_prompt_endpoint(client: TestClient):
    """The control plane serves a per-worker triage prompt carrying the decline marker."""
    worker_id, cred = _enroll_worker(client, {"tools": ["claude"], "cpus": 8})
    r = client.get("/triage-prompt", headers={"Authorization": f"Bearer {cred}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decline_marker"] in body["system"]
    assert "decline" in body["system"].lower()


# ---------- Derived observability model (ease-of-use-plan Part II, D0) ----------


def test_job_phase_derivation():
    assert server._job_phase({"state": "succeeded"}) == "succeeded"
    assert server._job_phase({"state": "running", "last_activity": "🔎 verifying result"}) == "verifying"
    assert server._job_phase({"state": "running", "last_activity": "🔧 self-healing (1)"}) == "self-healing"
    assert server._job_phase({"state": "running", "last_activity": "→ Bash"}) == "running"


def test_job_health_verdicts():
    assert server._job_health({"state": "failed", "error": "boom"})["status"] == "failed"
    assert server._job_health({"state": "succeeded", "result": {"verified": True}})["status"] == "verified"
    assert server._job_health({"state": "succeeded", "result": {"verified": False}})["status"] == "unverified"
    assert server._job_health({"state": "queued", "capable_workers": 0})["status"] == "unplaceable"
    assert server._job_health({"state": "queued", "capable_workers": 2, "queued_sec": 5})["status"] == "queued"
    assert server._job_health({"state": "running", "idle_sec": 999})["status"] == "stuck?"
    assert server._job_health({"state": "running", "idle_sec": 3})["status"] == "running"


def test_job_cost_and_budget():
    c = server._job_cost({"tokens_used": 500_000, "tree_budget_tokens": 1_000_000, "tree_budget_spent": 250_000})
    assert c["tokens_used"] == 500_000 and c["cost_est_usd"] > 0 and c["budget_pct"] == 25.0


def test_fleet_verdict_flags_problems_first():
    workers = [{"status": "idle"}, {"status": "busy"}]
    bad = server._derive_run({"id": "x", "state": "queued", "capable_workers": 0,
                              "spec": {"task": "need a GPU"}})
    good = server._derive_run({"id": "y", "state": "running", "spec": {"task": "ok"}})
    assert server._fleet_verdict(workers, [bad, good])["level"] == "alert"
    assert server._fleet_verdict(workers, [good])["level"] == "ok"


def test_derive_run_shape():
    r = server._derive_run({"id": "j1", "state": "succeeded", "worker_id": "w1",
                            "spec": {"task": "write a file"},
                            "result": {"verified": True, "output": "done", "evidence": "confirmed"}})
    assert r["run_id"] == "j1" and r["goal"] == "write a file"
    assert r["phase"] == "succeeded" and r["health"]["status"] == "verified"
    assert "cost" in r and "tokens_used" in r["cost"]


# ---------- Audit fixes (C2/H6/M1/M6/L7) ----------


def test_single_decline_with_no_remaining_capable_requeues_not_fails(tmp_path: Path):
    """[H6] A single decline where the capable set is momentarily empty (the only
    other capable node briefly stale/offline) must REQUEUE, not permanently fail —
    a transient must never destroy a placeable job."""
    import sqlite3
    import time as _time
    db = tmp_path / "roost.db"
    server._init_db(db)
    cpu = server._register_worker(db, "cpu", {"tools": ["claude"], "cpus": 4})
    gpu = server._register_worker(
        db, "gpu", {"tools": ["claude"], "gpu_count": 1, "gpu_vram_gb": 80})
    # The GPU box is the only other capable node but is momentarily stale/offline
    # (last_seen far in the past) → _online_capable_ids excludes it this instant.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workers SET last_seen=?, status='offline' WHERE id=?",
                     (_time.time() - 10_000, gpu["id"]))
        conn.commit()
    jid = server._insert_job(db, {"kind": "auto", "task": "needs a GPU", "requires": {}})["id"]
    _age_job(db, jid, seconds=5)

    got = server._try_assign_one(db, cpu["id"])
    assert got is not None and got["worker_id"] == cpu["id"]
    job, accepted = server._apply_event(
        db, jid, cpu["id"],
        {"type": "declined", "attempt": got["attempt"], "reason": "no GPU"})
    assert accepted
    # Only ONE distinct decliner and empty remaining → transient: must requeue.
    assert job["state"] == "queued", "single decline must not permanently fail"
    assert job["decline_count"] == 1


def test_two_distinct_decliners_none_remaining_fails_fast(tmp_path: Path):
    """[H6] Two DISTINCT capable nodes both decline and none remain → genuinely
    impossible, fail fast (don't bounce forever)."""
    db = tmp_path / "roost.db"
    server._init_db(db)
    a = server._register_worker(db, "a", {"tools": ["claude"]})
    b = server._register_worker(db, "b", {"tools": ["claude"]})
    jid = server._insert_job(db, {"kind": "auto", "task": "impossible", "requires": {}})["id"]
    _age_job(db, jid, seconds=5)

    got_a = server._try_assign_one(db, a["id"])
    assert got_a is not None
    job, _ = server._apply_event(
        db, jid, a["id"], {"type": "declined", "attempt": got_a["attempt"], "reason": "no"})
    # First decline (only b remains capable) → still queued.
    assert job["state"] == "queued"

    got_b = server._try_assign_one(db, b["id"])
    assert got_b is not None
    final, _ = server._apply_event(
        db, jid, b["id"], {"type": "declined", "attempt": got_b["attempt"], "reason": "no"})
    # Now 2 distinct decliners and none remaining → fail fast.
    assert final["state"] == "failed"
    assert "declined" in (final["error"] or "")


def test_finalize_requires_admin(client: TestClient):
    """[M6] finalize must reject a worker credential and only accept admin."""
    root = client.post("/jobs", json={
        "intent": "captain", "captain_root": True,
        "hierarchy": {"can_dispatch": True}}).json()
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    # Worker credential is forbidden from finalizing.
    r = client.post(f"/jobs/{root['id']}/finalize", json={"state": "succeeded"}, headers=wh)
    assert r.status_code == 403, r.text
    # Admin (shared token, default client headers) still works.
    ok = client.post(f"/jobs/{root['id']}/finalize", json={"state": "succeeded"})
    assert ok.status_code == 200, ok.text


def test_readyz_endpoint(client: TestClient):
    """[L7] /readyz is unauthenticated and reports a real DB read."""
    r = client.get("/readyz", headers={})  # no auth header
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is True
    assert "workers" in body


def test_run_refuses_unauthenticated_non_loopback(monkeypatch):
    """[C2] run() must refuse to serve unauthenticated on a non-loopback bind."""
    monkeypatch.delenv("ROOST_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        server.run(host="0.0.0.0", token="", provision_claude_auth=False)
    # Explicit insecure opt-in should NOT raise SystemExit before serving; we
    # stop it right at uvicorn.run so the test never actually binds a socket.
    import roost.server as _srv
    monkeypatch.setattr(_srv, "create_app", lambda **kw: object())

    class _Served(Exception):
        pass

    def _fake_uvicorn_run(*a, **k):
        raise _Served()

    import sys
    import types
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = _fake_uvicorn_run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    with pytest.raises(_Served):
        server.run(host="0.0.0.0", token="", insecure=True, provision_claude_auth=False)


def test_job_health_does_not_flag_stuck_during_verify_or_heal():
    # A job in verify/self-heal is legitimately quiet on its own activity line.
    j = {"state": "running", "idle_sec": 999, "last_activity": "🔎 verifying result"}
    assert server._job_health(j)["status"] == "verifying"
    j2 = {"state": "running", "idle_sec": 999, "last_activity": "🔧 self-healing (attempt 1)"}
    assert server._job_health(j2)["status"] == "self-healing"
    # but a genuinely idle running job (no verify marker) is still flagged
    assert server._job_health({"state": "running", "idle_sec": 999})["status"] == "stuck?"


# ---------- Hardening fixes (final audit) ----------


def test_worker_cannot_act_on_another_workers_path(client: TestClient):
    # Two enrolled workers; B's credential must not act on A's path (no impersonation).
    a_id, _ = _enroll_worker(client, {"tools": ["python3"]})
    r = client.post("/enroll-tokens", json={"label": "b"})
    btok = r.json()["token"]
    rb = client.post("/enroll", json={"token": btok, "name": "b", "capabilities": {}},
                     headers={"Authorization": ""})
    b_cred = rb.json()["credential"]
    bh = {"Authorization": f"Bearer {b_cred}"}
    # B heartbeating its own path is fine; B on A's path is forbidden.
    assert client.post(f"/workers/{rb.json()['worker_id']}/heartbeat", json={}, headers=bh).status_code == 200
    assert client.post(f"/workers/{a_id}/heartbeat", json={}, headers=bh).status_code == 403


def test_child_cannot_raise_max_depth_above_parent(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    root = server._insert_job(db, {"command": "true", "hierarchy": {"max_depth": 2, "can_dispatch": True}})
    assert root["max_depth"] == 2
    # A child declaring a huge max_depth is clamped down to the parent's ceiling.
    child = server._insert_job(
        db, {"command": "true", "hierarchy": {"max_depth": 1000, "can_dispatch": True}}, parent=root)
    assert child["max_depth"] == 2


def test_fleet_verdict_ignores_ancient_failures_but_flags_recent(monkeypatch):
    import time as _t
    now = 1_000_000.0
    workers = [{"status": "idle"}]
    old_fail = server._derive_run({"id": "o", "state": "failed", "error": "x",
                                   "spec": {"task": "old"}, "finished_at": now - 99999})
    recent_fail = server._derive_run({"id": "r", "state": "failed", "error": "x",
                                      "spec": {"task": "recent"}, "finished_at": now - 60})
    # Ancient failure alone → ok (history, not an alert).
    assert server._fleet_verdict(workers, [old_fail], now=now)["level"] == "ok"
    # A recent failure → alert.
    assert server._fleet_verdict(workers, [old_fail, recent_fail], now=now)["level"] == "alert"
    # An active unplaceable is always an alert regardless of age.
    unplace = server._derive_run({"id": "u", "state": "queued", "capable_workers": 0,
                                  "spec": {"task": "needs gpu"}})
    assert server._fleet_verdict(workers, [unplace], now=now)["level"] == "alert"


# ---------- Capacity-based concurrency (V8) ----------


def _set_capacity(db: Path, worker_id: str, capacity: int) -> None:
    with server._connect(db) as conn:
        conn.execute("UPDATE workers SET capacity=? WHERE id=?", (capacity, worker_id))


def test_capacity_2_worker_takes_two_jobs_then_saturates(tmp_path: Path):
    # A worker reporting capacity=2 must take a SECOND job while one is in flight,
    # then refuse a third (in-flight == capacity → saturated).
    db = tmp_path / "roost.db"
    server._init_db(db)
    w = server._register_worker(db, "w", {"tools": ["python3"]})
    _set_capacity(db, w["id"], 2)
    for _ in range(3):
        server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    a = server._try_assign_one(db, w["id"])
    assert a is not None
    # One in flight, capacity 2 → still assignable, stays display-idle.
    wrow = next(x for x in server._list_workers(db) if x["id"] == w["id"])
    assert wrow["status"] == "idle" and wrow["running"] == 1 and wrow["capacity"] == 2
    b = server._try_assign_one(db, w["id"])
    assert b is not None and b["id"] != a["id"]
    # Now saturated (2/2) → display-busy and refuses a third.
    wrow = next(x for x in server._list_workers(db) if x["id"] == w["id"])
    assert wrow["status"] == "busy" and wrow["running"] == 2
    assert server._try_assign_one(db, w["id"]) is None


def test_capacity_1_worker_saturates_after_one(tmp_path: Path):
    # Default capacity 1 keeps the old binary behavior: one job → busy → no more.
    db = tmp_path / "roost.db"
    server._init_db(db)
    w = server._register_worker(db, "w", {"tools": ["python3"]})
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    got = server._try_assign_one(db, w["id"])
    assert got is not None
    wrow = next(x for x in server._list_workers(db) if x["id"] == w["id"])
    assert wrow["status"] == "busy" and wrow["capacity"] == 1
    assert server._try_assign_one(db, w["id"]) is None


def test_finishing_a_job_reopens_a_slot(tmp_path: Path):
    # When a job finishes, a freed slot flips a saturated worker back to idle.
    db = tmp_path / "roost.db"
    server._init_db(db)
    w = server._register_worker(db, "w", {"tools": ["python3"]})
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    got = server._try_assign_one(db, w["id"])
    assert next(x for x in server._list_workers(db) if x["id"] == w["id"])["status"] == "busy"
    server._apply_event(db, got["id"], w["id"],
                        {"type": "succeeded", "attempt": got["attempt"], "exit_code": 0})
    wrow = next(x for x in server._list_workers(db) if x["id"] == w["id"])
    assert wrow["status"] == "idle" and wrow["running"] == 0


def test_heartbeat_persists_capacity_from_load(tmp_path: Path):
    # The pinned wire contract: heartbeat's load.capacity is persisted.
    db = tmp_path / "roost.db"
    server._init_db(db)
    wid = server._register_worker(db, "w", {"tools": ["python3"]})["id"]
    server._heartbeat_worker(db, wid, {"tools": ["python3"], "load": {"running": 0, "capacity": 4}})
    wrow = next(x for x in server._list_workers(db) if x["id"] == wid)
    assert wrow["capacity"] == 4


def test_heartbeat_capacity_is_sticky_when_load_absent(tmp_path: Path):
    # A heartbeat that carries capabilities but omits load/load.capacity must
    # PRESERVE a previously-pinned capacity, not clobber it back to 1.
    db = tmp_path / "roost.db"
    server._init_db(db)
    wid = server._register_worker(db, "w", {"tools": ["python3"]})["id"]
    server._heartbeat_worker(db, wid, {"tools": ["python3"], "load": {"running": 0, "capacity": 4}})
    # Heartbeat with capabilities but no load at all.
    server._heartbeat_worker(db, wid, {"tools": ["python3"]})
    assert next(x for x in server._list_workers(db) if x["id"] == wid)["capacity"] == 4
    # Heartbeat with a load dict that omits capacity.
    server._heartbeat_worker(db, wid, {"tools": ["python3"], "load": {"running": 1}})
    assert next(x for x in server._list_workers(db) if x["id"] == wid)["capacity"] == 4
    # An invalid capacity (bool / <1) is also ignored, preserving the stored value.
    server._heartbeat_worker(db, wid, {"tools": ["python3"], "load": {"capacity": 0}})
    assert next(x for x in server._list_workers(db) if x["id"] == wid)["capacity"] == 4


def test_capacity_worker_competes_while_partially_loaded(tmp_path: Path):
    # A capacity-2 worker with 1 job in flight still competes for placement; a
    # second worker doesn't get the job purely because the first is "busy".
    db = tmp_path / "roost.db"
    server._init_db(db)
    big = server._register_worker(db, "big", {"tools": ["python3"], "load": {"running": 0}})
    _set_capacity(db, big["id"], 2)
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    first = server._try_assign_one(db, big["id"])
    assert first is not None
    # Another queued job + the partially-loaded big worker polls again: it is not
    # saturated, so it can take the second job.
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    second = server._try_assign_one(db, big["id"])
    assert second is not None and second["id"] != first["id"]


def test_failed_event_persists_diagnosis(tmp_path: Path):
    # The pinned wire contract: a FAILED event's diagnosis is stored and surfaced
    # in /derived; a failure without one stores NULL.
    db = tmp_path / "roost.db"
    server._init_db(db)
    w = server._register_worker(db, "w", {"tools": ["python3"]})
    server._insert_job(db, {"command": "false", "requires": {"tools": ["python3"]}})
    got = server._try_assign_one(db, w["id"])
    server._apply_event(
        db, got["id"], w["id"],
        {"type": "failed", "attempt": got["attempt"], "exit_code": 1,
         "error": "boom", "diagnosis": "OOM: model too large for 8GB VRAM"},
    )
    job = server._get_job(db, got["id"])
    assert job["diagnosis"] == "OOM: model too large for 8GB VRAM"
    assert server._derive_run(job)["diagnosis"] == "OOM: model too large for 8GB VRAM"


def test_failed_event_without_diagnosis_stores_null(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    w = server._register_worker(db, "w", {"tools": ["python3"]})
    server._insert_job(db, {"command": "false", "requires": {"tools": ["python3"]}})
    got = server._try_assign_one(db, w["id"])
    server._apply_event(db, got["id"], w["id"],
                        {"type": "failed", "attempt": got["attempt"], "exit_code": 1})
    assert server._get_job(db, got["id"])["diagnosis"] is None


def test_sweep_prunes_dead_orphan_worker_rows(tmp_path: Path):
    # An offline worker unseen past the prune TTL with no in-flight job is deleted.
    db = tmp_path / "roost.db"
    server._init_db(db)
    dead = server._register_worker(db, "dead", {"tools": ["python3"]})["id"]
    live = server._register_worker(db, "live", {"tools": ["python3"]})["id"]
    import time as _t
    old = _t.time() - server.WORKER_PRUNE_TTL - 60
    with server._connect(db) as conn:
        conn.execute("UPDATE workers SET status='offline', last_seen=? WHERE id=?", (old, dead))
    counts = server._sweep(db)
    assert counts["pruned"] == 1
    ids = {w["id"] for w in server._list_workers(db)}
    assert dead not in ids and live in ids


def test_sweep_never_prunes_offline_worker_owning_inflight_job(tmp_path: Path):
    # Conservative: an offline+old worker that still OWNS an in-flight job is kept.
    db = tmp_path / "roost.db"
    server._init_db(db)
    wid = server._register_worker(db, "w", {"tools": ["python3"]})["id"]
    import time as _t
    old = _t.time() - server.WORKER_PRUNE_TTL - 60
    with server._connect(db) as conn:
        conn.execute("INSERT INTO jobs(id,spec,created_at,state,worker_id) "
                     "VALUES('j1','{}',1.0,'running',?)", (wid,))
        conn.execute("UPDATE workers SET status='offline', last_seen=? WHERE id=?", (old, wid))
    counts = server._sweep(db)
    assert counts["pruned"] == 0
    assert wid in {w["id"] for w in server._list_workers(db)}


def test_sweep_does_not_prune_recently_seen_or_online(tmp_path: Path):
    # A worker seen recently (even if labeled offline) and an online worker are kept.
    db = tmp_path / "roost.db"
    server._init_db(db)
    recent = server._register_worker(db, "recent", {"tools": ["python3"]})["id"]
    online = server._register_worker(db, "online", {"tools": ["python3"]})["id"]
    with server._connect(db) as conn:
        # 'offline' label but last_seen is fresh → not past the prune TTL.
        conn.execute("UPDATE workers SET status='offline' WHERE id=?", (recent,))
    counts = server._sweep(db)
    assert counts["pruned"] == 0
    ids = {w["id"] for w in server._list_workers(db)}
    assert recent in ids and online in ids


def test_sweep_never_prunes_enrolled_credentialed_worker(tmp_path: Path):
    # A per-worker-credentialed (cred_hash set) node that's merely powered off
    # overnight must KEEP its row — deleting cred_hash would lock it out (401 →
    # forced re-enroll). Only credential-less orphans are pruned.
    db = tmp_path / "roost.db"
    server._init_db(db)
    enrolled = server._register_worker(db, "enrolled", {"tools": ["python3"]})["id"]
    orphan = server._register_worker(db, "orphan", {"tools": ["python3"]})["id"]
    import time as _t
    old = _t.time() - server.WORKER_PRUNE_TTL - 60
    with server._connect(db) as conn:
        conn.execute("UPDATE workers SET status='offline', last_seen=?, cred_hash=? WHERE id=?",
                     (old, "deadbeef", enrolled))
        conn.execute("UPDATE workers SET status='offline', last_seen=? WHERE id=?", (old, orphan))
    counts = server._sweep(db)
    assert counts["pruned"] == 1  # only the credential-less orphan
    ids = {w["id"] for w in server._list_workers(db)}
    assert enrolled in ids and orphan not in ids


def test_sweep_never_prunes_revoked_worker(tmp_path: Path):
    # A revoked row is preserved as an audit record even when old + offline.
    db = tmp_path / "roost.db"
    server._init_db(db)
    revoked = server._register_worker(db, "revoked", {"tools": ["python3"]})["id"]
    import time as _t
    old = _t.time() - server.WORKER_PRUNE_TTL - 60
    server._revoke_worker(db, revoked)  # clears cred_hash, sets revoked=1, offline
    with server._connect(db) as conn:
        conn.execute("UPDATE workers SET last_seen=? WHERE id=?", (old, revoked))
    counts = server._sweep(db)
    assert counts["pruned"] == 0
    assert revoked in {w["id"] for w in server._list_workers(db)}


def test_heartbeat_renews_all_inflight_leases(tmp_path: Path):
    # A worker running multiple jobs concurrently: one heartbeat must renew the
    # lease on EVERY in-flight job it owns, not just one.
    db = tmp_path / "roost.db"
    server._init_db(db)
    big = server._register_worker(db, "big", {"tools": ["python3"], "load": {"running": 0}})["id"]
    _set_capacity(db, big, 2)
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    j1 = server._try_assign_one(db, big)
    j2 = server._try_assign_one(db, big)
    assert j1 is not None and j2 is not None and j1["id"] != j2["id"]
    # Force both leases into the past so we can observe renewal moving them ahead.
    import time as _t
    past = _t.time() - 1000.0
    with server._connect(db) as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=? WHERE worker_id=?", (past, big))
    server._heartbeat_worker(db, big, {"tools": ["python3"], "load": {"running": 2, "capacity": 2}})
    now = _t.time()
    with server._connect(db) as conn:
        rows = conn.execute(
            "SELECT id, lease_expires_at FROM jobs WHERE worker_id=? AND state IN ('assigned','running')",
            (big,),
        ).fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["lease_expires_at"] > now  # both leases renewed into the future


# ---------- worker prune (explicit admin cleanup of ghost rows) ----------

def _set_last_seen(db: Path, worker_id: str, ts: float) -> None:
    with server._connect(db) as conn:
        conn.execute("UPDATE workers SET last_seen=? WHERE id=?", (ts, worker_id))


def test_prune_workers_deletes_stale_keeps_recent(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    old = server._register_worker(db, "ghost", {"tools": ["python3"]})
    fresh = server._register_worker(db, "live", {"tools": ["python3"]})
    now = time.time()
    _set_last_seen(db, old["id"], now - 5 * 86400)   # 5 days ago
    _set_last_seen(db, fresh["id"], now - 10)        # seen 10s ago
    res = server._prune_workers(db, older_than_sec=86400)  # older than 1 day
    assert res["pruned"] == 1 and res["names"] == ["ghost"]
    remaining = {w["id"] for w in server._list_workers(db)}
    assert remaining == {fresh["id"]}


def test_prune_workers_spares_inflight_owner(tmp_path: Path):
    db = tmp_path / "roost.db"
    server._init_db(db)
    w = server._register_worker(db, "busy-old", {"tools": ["python3"]})
    server._insert_job(db, {"command": "true", "requires": {"tools": ["python3"]}})
    assigned = server._try_assign_one(db, w["id"])   # job now 'assigned' to w
    assert assigned is not None
    _set_last_seen(db, w["id"], time.time() - 30 * 86400)  # ancient, but in-flight
    res = server._prune_workers(db, older_than_sec=86400)
    assert res["pruned"] == 0
    assert {x["id"] for x in server._list_workers(db)} == {w["id"]}


def test_prune_endpoint_requires_admin(client: TestClient):
    # A worker credential is not admin → 403.
    wid, cred = _enroll_worker(client, {"tools": ["python3"]})
    r = client.post("/workers/prune", params={"older_than_days": 1},
                    headers={"Authorization": f"Bearer {cred}"})
    assert r.status_code == 403
    # The shared admin token works.
    r = client.post("/workers/prune", params={"older_than_days": 1})
    assert r.status_code == 200, r.text
    assert "pruned" in r.json()


# ---------- Mobile pairing (scoped client tokens, `roost pair`) ----------


def _pair_mobile(client: TestClient, label: str = "test-phone") -> tuple[str, str]:
    """Mint a mobile token as admin. Returns (token_id, raw_token)."""
    r = client.post("/pair-tokens", json={"label": label})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith(server.MOBILE_TOKEN_PREFIX)
    assert body["scope"] == "mobile"
    return body["id"], body["token"]


def test_pair_token_mint_requires_admin(client: TestClient):
    _, cred = _enroll_worker(client, {"tools": ["python3"]})
    r = client.post("/pair-tokens", json={"label": "x"},
                    headers={"Authorization": f"Bearer {cred}"})
    assert r.status_code == 403


def test_mobile_token_reads_submits_and_cancels(client: TestClient):
    _enroll_worker(client, {"tools": ["python3"]})
    _, tok = _pair_mobile(client)
    mh = {"Authorization": f"Bearer {tok}"}

    # Reads: the dashboard surface.
    assert client.get("/jobs", headers=mh).status_code == 200
    assert client.get("/derived", headers=mh).status_code == 200
    assert client.get("/workers", headers=mh).status_code == 200

    # Submit a job, read it back, read its logs, cancel it.
    r = client.post("/jobs", headers=mh,
                    json={"command": "echo hi", "requires": {"tools": ["python3"]}})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    assert client.get(f"/jobs/{job_id}", headers=mh).status_code == 200
    assert client.get(f"/jobs/{job_id}/logs", headers=mh).status_code == 200
    r = client.delete(f"/jobs/{job_id}", headers=mh)
    assert r.status_code == 200, r.text


def test_mobile_token_denied_admin_and_worker_plane(client: TestClient):
    wid, _ = _enroll_worker(client, {"tools": ["python3"]})
    _, tok = _pair_mobile(client)
    mh = {"Authorization": f"Bearer {tok}"}

    # Admin plane: minting, pairing management, worker lifecycle.
    assert client.post("/enroll-tokens", json={"label": "x"}, headers=mh).status_code == 403
    assert client.post("/pair-tokens", json={"label": "x"}, headers=mh).status_code == 403
    assert client.get("/pair-tokens", headers=mh).status_code == 403
    assert client.delete(f"/workers/{wid}", headers=mh).status_code == 403
    assert client.post("/workers/prune", params={"older_than_days": 1},
                       headers=mh).status_code == 403

    # Worker plane: creds provisioning + lease loop are off-limits.
    assert client.get("/claude-creds", headers=mh).status_code == 403
    assert client.get(f"/workers/{wid}/poll", params={"timeout": 0},
                      headers=mh).status_code == 403
    assert client.post(f"/workers/{wid}/heartbeat", json={},
                       headers=mh).status_code == 403

    # Legacy register path only accepts the shared token.
    assert client.post("/workers/register", json={"name": "evil", "capabilities": {}},
                       headers=mh).status_code == 403


def test_mobile_token_cannot_finalize_jobs(client: TestClient):
    _enroll_worker(client, {"tools": ["python3"]})
    _, tok = _pair_mobile(client)
    mh = {"Authorization": f"Bearer {tok}"}
    r = client.post("/jobs", headers=mh,
                    json={"command": "echo hi", "requires": {"tools": ["python3"]}})
    job_id = r.json()["id"]
    r = client.post(f"/jobs/{job_id}/finalize", headers=mh,
                    json={"state": "succeeded"})
    assert r.status_code == 403


def test_revoked_mobile_token_is_rejected(client: TestClient):
    token_id, tok = _pair_mobile(client)
    mh = {"Authorization": f"Bearer {tok}"}
    assert client.get("/jobs", headers=mh).status_code == 200
    r = client.delete(f"/pair-tokens/{token_id}")
    assert r.status_code == 200 and r.json()["revoked"] is True
    # Token is dead immediately.
    assert client.get("/jobs", headers=mh).status_code == 401
    # Double-revoke 404s.
    assert client.delete(f"/pair-tokens/{token_id}").status_code == 404


def test_pair_token_list_never_leaks_secrets(client: TestClient):
    token_id, tok = _pair_mobile(client, label="audit-me")
    rows = client.get("/pair-tokens").json()
    assert any(r["id"] == token_id and r["label"] == "audit-me" for r in rows)
    for row in rows:
        assert "token_hash" not in row
        assert tok not in str(row.values())


# ---------- Hard `target` worker-pin ----------


def test_target_by_id_pins_to_one_worker(client: TestClient):
    """A job with target=<worker id> is assignable ONLY to that worker; every
    other capable worker leaves it queued (no fall-through)."""
    a_id, a_cred = _enroll_named(client, "a", {"tools": ["python3"]})
    b_id, b_cred = _enroll_named(client, "b", {"tools": ["python3"]})
    ah = {"Authorization": f"Bearer {a_cred}"}
    bh = {"Authorization": f"Bearer {b_cred}"}

    r = client.post("/jobs", json={
        "command": "true", "requires": {"tools": ["python3"]}, "target": b_id,
    })
    job_id = r.json()["id"]

    # Worker a is fully capable but NOT the target → nothing for it.
    assert client.get(f"/workers/{a_id}/poll", params={"timeout": 0},
                      headers=ah).status_code == 204
    # Job is still queued, untouched.
    assert client.get(f"/jobs/{job_id}").json()["state"] == "queued"

    # The pinned worker b gets it.
    r = client.get(f"/workers/{b_id}/poll", params={"timeout": 0}, headers=bh)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == job_id
    assert r.json()["state"] == "assigned"


def test_target_by_name_pins_to_one_worker(client: TestClient):
    a_id, a_cred = _enroll_named(client, "alpha", {"tools": ["python3"]})
    b_id, b_cred = _enroll_named(client, "beta", {"tools": ["python3"]})
    ah = {"Authorization": f"Bearer {a_cred}"}
    bh = {"Authorization": f"Bearer {b_cred}"}

    client.post("/jobs", json={
        "command": "true", "requires": {"tools": ["python3"]}, "target": "beta",
    })
    # Non-target by name gets nothing.
    assert client.get(f"/workers/{a_id}/poll", params={"timeout": 0},
                      headers=ah).status_code == 204
    # Target by name gets it.
    r = client.get(f"/workers/{b_id}/poll", params={"timeout": 0}, headers=bh)
    assert r.status_code == 200 and r.json()["state"] == "assigned"


def test_target_nonexistent_or_offline_stays_queued(client: TestClient):
    """A target naming a worker that doesn't exist (or is offline) is no error —
    the job just stays queued, and no other worker may grab it."""
    a_id, a_cred = _enroll_named(client, "a", {"tools": ["python3"]})
    ah = {"Authorization": f"Bearer {a_cred}"}

    r = client.post("/jobs", json={
        "command": "true", "requires": {"tools": ["python3"]},
        "target": "ghost-worker",
    })
    job_id = r.json()["id"]
    assert client.get(f"/workers/{a_id}/poll", params={"timeout": 0},
                      headers=ah).status_code == 204
    assert client.get(f"/jobs/{job_id}").json()["state"] == "queued"


def test_normal_job_unaffected_by_target_feature(client: TestClient):
    """A job with no target routes normally to a capable worker."""
    a_id, a_cred = _enroll_named(client, "a", {"tools": ["python3"]})
    ah = {"Authorization": f"Bearer {a_cred}"}
    client.post("/jobs", json={"command": "true", "requires": {"tools": ["python3"]}})
    r = client.get(f"/workers/{a_id}/poll", params={"timeout": 0}, headers=ah)
    assert r.status_code == 200 and r.json()["state"] == "assigned"


# ---------- Enroll dedup ----------


def test_reenroll_same_name_and_host_retires_old_row(client: TestClient):
    """Re-enrolling the same machine (same name + hostname) cleanly replaces it:
    exactly one non-offline 'w1' row remains (the newest); the old one is
    revoked + offline."""
    caps = {"hostname": "h", "tools": ["python3"]}
    old_id, _ = _enroll_named(client, "w1", caps)
    new_id, _ = _enroll_named(client, "w1", caps)
    assert old_id != new_id

    with server._connect(client.app.state.db_path) as conn:
        rows = conn.execute(
            "SELECT id, status, revoked FROM workers WHERE name='w1'"
        ).fetchall()
    by_id = {r["id"]: r for r in rows}
    # Old row retired: revoked + offline.
    assert by_id[old_id]["revoked"] == 1
    assert by_id[old_id]["status"] == "offline"
    # New row is live and not revoked.
    assert by_id[new_id]["revoked"] == 0
    assert by_id[new_id]["status"] != "offline"
    # Exactly one non-offline 'w1' row.
    live = [r for r in rows if r["status"] != "offline"]
    assert len(live) == 1 and live[0]["id"] == new_id


def test_reenroll_different_host_does_not_retire(client: TestClient):
    """A different machine reusing a name (different hostname) is NOT retired —
    only a clearly-superseded same-host row is."""
    old_id, _ = _enroll_named(client, "w1", {"hostname": "h1"})
    new_id, _ = _enroll_named(client, "w1", {"hostname": "h2"})
    with server._connect(client.app.state.db_path) as conn:
        old = conn.execute(
            "SELECT revoked FROM workers WHERE id=?", (old_id,)
        ).fetchone()
    assert old["revoked"] == 0


# ---------- install.sh hardening ----------


def test_install_script_pins_python_and_real_source(client: TestClient):
    body = client.get("/install.sh").text
    # Python is pinned to 3.12 (newer interpreters break the async HTTP client).
    assert "--python 3.12" in body
    # The default source is NOT the bare unrelated `roost` PyPI name.
    assert 'ROOST_SOURCE="${ROOST_SOURCE:-roost}"' not in body
    # It defaults to a real (git) source for this project.
    assert "git+https://github.com" in body
    # Opt-in claude install flag is documented/handled.
    assert "--with-claude" in body


# ---------- write-time log bounds (R11) ----------


def _running_job(client: TestClient) -> tuple[str, str, dict, int]:
    """Submit + lease + start a job; returns (worker_id, job_id, wh, attempt)."""
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    r = client.post("/jobs", json={"command": "echo hi",
                                   "requires": {"tools": ["python3"]}})
    job_id = r.json()["id"]
    a = client.get(f"/workers/{worker_id}/poll", params={"timeout": 0},
                   headers=wh).json()
    client.post(f"/workers/{worker_id}/jobs/{job_id}/event",
                json={"type": "started", "attempt": a["attempt"]}, headers=wh)
    return worker_id, job_id, wh, a["attempt"]


def test_log_append_oversize_rejected_413(client: TestClient):
    worker_id, job_id, wh, _ = _running_job(client)
    big = "x" * (server.LOG_APPEND_MAX_BYTES + 1)
    r = client.post(f"/workers/{worker_id}/jobs/{job_id}/logs",
                    json={"stream": "stdout", "data": big}, headers=wh)
    assert r.status_code == 413
    assert "split or truncate" in r.json()["detail"]
    # Nothing was stored.
    assert client.get(f"/jobs/{job_id}/logs").json()["logs"][-1]["stream"] == "event"
    # A line exactly AT the cap is fine.
    r = client.post(f"/workers/{worker_id}/jobs/{job_id}/logs",
                    json={"stream": "stdout",
                          "data": "x" * server.LOG_APPEND_MAX_BYTES},
                    headers=wh)
    assert r.status_code == 200


def test_log_append_size_cap_counts_bytes_not_chars(client: TestClient):
    # Multibyte text: char count under the cap, byte count over → rejected.
    worker_id, job_id, wh, _ = _running_job(client)
    snowmen = "☃" * (server.LOG_APPEND_MAX_BYTES // 3 + 100)  # 3 bytes each
    r = client.post(f"/workers/{worker_id}/jobs/{job_id}/logs",
                    json={"stream": "stdout", "data": snowmen}, headers=wh)
    assert r.status_code == 413


def test_log_append_row_ceiling_429(client: TestClient, monkeypatch):
    monkeypatch.setattr(server, "LOG_MAX_ROWS_PER_JOB", 5)
    worker_id, job_id, wh, attempt = _running_job(client)
    # The `started` event row is exempt from the ceiling count's effects on
    # events but still occupies a row; fill up to the ceiling with stdout.
    url = f"/workers/{worker_id}/jobs/{job_id}/logs"
    codes = [client.post(url, json={"stream": "stdout", "data": f"l{i}"},
                         headers=wh).status_code for i in range(6)]
    # 1 event row exists; stdout appends accepted until COUNT hits 5.
    assert codes[:4] == [200, 200, 200, 200]
    assert codes[4:] == [429, 429]
    r = client.post(url, json={"stream": "stdout", "data": "one more"},
                    headers=wh)
    assert r.status_code == 429
    assert "row ceiling" in r.json()["detail"]

    # Liveness still bumped by the rejected append (capped ≠ stuck).
    with server._connect(client.app.state.db_path) as conn:
        row = conn.execute("SELECT last_activity_at FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
    assert row["last_activity_at"] == pytest.approx(time.time(), abs=5)

    # Lifecycle EVENT rows still land at the ceiling (exempt stream) — the
    # terminal state divider must not be lost to stdout spam.
    r = client.post(f"/workers/{worker_id}/jobs/{job_id}/event",
                    json={"type": "succeeded", "attempt": attempt,
                          "exit_code": 0}, headers=wh)
    assert r.status_code == 200
    logs = client.get(f"/jobs/{job_id}/logs").json()["logs"]
    assert logs[-1]["stream"] == "event"
    assert "succeeded" in logs[-1]["data"]


def test_oversize_event_slimmed_not_rejected(client: TestClient):
    # A terminal event with a huge result must not fail the state change OR
    # the log row — the row is slimmed to parseable {"type", "truncated"}.
    worker_id, job_id, wh, attempt = _running_job(client)
    huge = "y" * (server.LOG_APPEND_MAX_BYTES + 100)
    r = client.post(f"/workers/{worker_id}/jobs/{job_id}/event",
                    json={"type": "succeeded", "attempt": attempt,
                          "exit_code": 0, "result": {"output": huge}},
                    headers=wh)
    assert r.status_code == 200
    assert r.json()["state"] == "succeeded"
    last = client.get(f"/jobs/{job_id}/logs").json()["logs"][-1]
    assert last["stream"] == "event"
    parsed = json.loads(last["data"])
    assert parsed == {"type": "succeeded", "truncated": True}


# ---------- decline/requeue bookkeeping (R19) ----------


def _auto_job_and_worker(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    job = client.post("/jobs", json={
        "kind": "auto", "task": "t",
        "requires": {"tools": ["python3"]}}).json()
    return worker_id, wh, job


def test_decline_requeue_restarts_placement_grace(client: TestClient):
    # [R19a] A job already older than PLACEMENT_GRACE is handed out via
    # anti-starvation; after a decline the grace window must RESTART so the
    # next placement round is competitive again.
    worker_id, wh, job = _auto_job_and_worker(client)
    with server._connect(client.app.state.db_path) as conn:
        conn.execute("UPDATE jobs SET created_at = created_at - 10 WHERE id=?",
                     (job["id"],))
    a = client.get(f"/workers/{worker_id}/poll", params={"timeout": 0},
                   headers=wh).json()
    assert a["id"] == job["id"]
    client.post(f"/workers/{worker_id}/jobs/{job['id']}/event",
                json={"type": "declined", "attempt": a["attempt"],
                      "reason": "poor fit"}, headers=wh)
    with server._connect(client.app.state.db_path) as conn:
        row = conn.execute("SELECT requeued_at, created_at FROM jobs WHERE id=?",
                           (job["id"],)).fetchone()
    assert row["requeued_at"] is not None
    # The grace clock now runs from requeued_at — effectively zero age...
    assert time.time() - row["requeued_at"] < server.PLACEMENT_GRACE
    # ...while created_at stays truthful for display/queued_sec.
    assert time.time() - row["created_at"] >= 10


def test_declines_do_not_consume_the_attempt_budget(client: TestClient):
    # [R19b] Each assignment increments `attempt`; a decline must refund it,
    # or two declines at default max_attempts=2 leave a REAL execution with
    # zero retries (sweeper kills at attempt >= max_attempts).
    worker_id, wh, job = _auto_job_and_worker(client)
    a = client.get(f"/workers/{worker_id}/poll", params={"timeout": 0},
                   headers=wh).json()
    assert a["attempt"] == 1
    client.post(f"/workers/{worker_id}/jobs/{job['id']}/event",
                json={"type": "declined", "attempt": a["attempt"],
                      "reason": "poor fit"}, headers=wh)
    row = client.get(f"/jobs/{job['id']}").json()
    assert row["state"] == "queued"
    assert row["attempt"] == 0  # refunded
    assert row["decline_count"] == 1  # decline bookkeeping intact

    # A second capable worker takes it as a FRESH attempt 1 with the full
    # retry budget; the decliner is still skipped.
    w2, cred2 = _enroll_worker(client, {"tools": ["python3"]})
    h2 = {"Authorization": f"Bearer {cred2}"}
    a2 = client.get(f"/workers/{w2}/poll", params={"timeout": 0},
                    headers=h2).json()
    assert a2["id"] == job["id"]
    assert a2["attempt"] == 1


def test_decline_escalation_and_skip_still_hold(client: TestClient):
    # Regression guard: the refund must not break decliner-skip or the
    # declined-by-all escalation.
    wa, ha, job = _auto_job_and_worker(client)
    a = client.get(f"/workers/{wa}/poll", params={"timeout": 0}, headers=ha).json()
    client.post(f"/workers/{wa}/jobs/{job['id']}/event",
                json={"type": "declined", "attempt": a["attempt"],
                      "reason": "no gpu"}, headers=ha)
    # The decliner never re-grabs it.
    r = client.get(f"/workers/{wa}/poll", params={"timeout": 0}, headers=ha)
    assert r.status_code == 204
    # Second (last capable) worker declines → declined-by-all escalation.
    wb, credb = _enroll_worker(client, {"tools": ["python3"]})
    hb = {"Authorization": f"Bearer {credb}"}
    a2 = client.get(f"/workers/{wb}/poll", params={"timeout": 0}, headers=hb).json()
    client.post(f"/workers/{wb}/jobs/{job['id']}/event",
                json={"type": "declined", "attempt": a2["attempt"],
                      "reason": "no gpu"}, headers=hb)
    row = client.get(f"/jobs/{job['id']}").json()
    assert row["state"] == "failed"
    assert "declined by all 2" in row["error"]


def test_prefer_by_name_routes_inside_grace_window(client: TestClient):
    # [R20] end-to-end: with the job inside the placement-grace window, a
    # non-preferred poller defers to the worker preferred BY NAME.
    wa, creda = _enroll_worker(client, {"tools": ["python3"]})
    ha = {"Authorization": f"Bearer {creda}"}
    # Second worker named distinctly; prefer it by NAME.
    r = client.post("/enroll-tokens", json={"label": "t2"})
    tok = r.json()["token"]
    r = client.post("/enroll", json={"token": tok, "name": "the-chosen-one",
                                     "capabilities": {"tools": ["python3"]}},
                    headers={"Authorization": ""})
    wb, credb = r.json()["worker_id"], r.json()["credential"]
    hb = {"Authorization": f"Bearer {credb}"}

    job = client.post("/jobs", json={
        "command": "echo hi", "requires": {"tools": ["python3"]},
        "prefer": {"worker": "the-chosen-one"}}).json()

    # Non-preferred worker polls first, inside the grace window → defers.
    r = client.get(f"/workers/{wa}/poll", params={"timeout": 0}, headers=ha)
    assert r.status_code == 204
    # The preferred worker (by NAME) takes it.
    got = client.get(f"/workers/{wb}/poll", params={"timeout": 0},
                     headers=hb).json()
    assert got["id"] == job["id"]


def test_publish_middleware_passthrough_and_routing(tmp_path: Path):
    # Pure-ASGI publish router: LAN/API traffic passes straight through (so 204/SSE
    # responses are never re-streamed), publish-domain hosts get site content only.
    db = tmp_path / "roost.db"
    app = server.create_app(db_path=db, token="t", run_sweeper=False, publish_domain="roost.pub")
    with TestClient(app) as c:
        # LAN/API traffic (normal host) is untouched.
        assert c.get("/healthz").status_code == 200
        # Apex host → landing page, never the API.
        r = c.get("/", headers={"host": "roost.pub"})
        assert r.status_code == 200 and "roost.pub" in r.text
        # Apex non-root → 404 (no API leak under the public domain).
        assert c.get("/workers", headers={"host": "roost.pub"}).status_code == 404
        # Unknown slug subdomain → 404.
        assert c.get("/", headers={"host": "nope.roost.pub"}).status_code == 404
        # A non-publish host still reaches the API (auth-gated, not routed to publish).
        assert c.get("/healthz", headers={"host": "192.168.1.193:8787"}).status_code == 200


# ---------- /metrics (Prometheus text exposition, R35) ----------


def _parse_prometheus(text: str) -> dict[str, dict]:
    """Tiny exposition parser for assertions: returns
    {metric_name: {"help": str, "type": str, "samples": {labels_str: float}}}.
    Validates that every sample line belongs to a series that already declared
    HELP and TYPE, and that no metric declares HELP/TYPE more than once."""
    import re

    metrics: dict[str, dict] = {}
    sample_re = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{.*\})? (?P<value>.+)$")
    for line in text.split("\n"):
        if line == "":
            continue
        if line.startswith("# HELP "):
            _, _, rest = line.partition("# HELP ")
            name, _, help_text = rest.partition(" ")
            assert name not in metrics, f"duplicate HELP for {name}"
            metrics[name] = {"help": help_text, "type": None, "samples": {}}
        elif line.startswith("# TYPE "):
            _, _, rest = line.partition("# TYPE ")
            name, _, mtype = rest.partition(" ")
            assert name in metrics, f"TYPE before HELP for {name}"
            assert metrics[name]["type"] is None, f"duplicate TYPE for {name}"
            metrics[name]["type"] = mtype
        elif line.startswith("#"):
            raise AssertionError(f"unexpected comment line: {line!r}")
        else:
            m = sample_re.match(line)
            assert m, f"unparseable sample line: {line!r}"
            name = m.group("name")
            # The series (possibly a labelled child of a family) must trace back
            # to a declared family name.
            assert name in metrics, f"sample for undeclared metric: {name!r}"
            assert metrics[name]["type"] is not None, f"sample before TYPE for {name}"
            labels = m.group("labels") or ""
            metrics[name]["samples"][labels] = float(m.group("value"))
    return metrics


def test_metrics_format_valid(client: TestClient):
    """[R35] /metrics returns valid Prometheus 0.0.4 text exposition: correct
    content type, HELP+TYPE per series, a trailing newline, and >=8 series."""
    r = client.get("/metrics")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in r.headers["content-type"]
    body = r.text
    assert body.endswith("\n"), "exposition must end with a trailing newline"

    metrics = _parse_prometheus(body)
    # Every declared metric has both HELP and TYPE.
    for name, meta in metrics.items():
        assert meta["help"], f"{name} missing HELP text"
        assert meta["type"] in ("gauge", "counter"), f"{name} bad TYPE {meta['type']!r}"
    # The core series we promised are present.
    expected = {
        "roost_jobs", "roost_queue_depth", "roost_workers_online",
        "roost_workers_total", "roost_blobs_count", "roost_blobs_bytes",
        "roost_sites_count", "roost_lease_expirations_total",
        "roost_schedule_beats_total",
    }
    assert expected <= set(metrics), f"missing series: {expected - set(metrics)}"
    assert len(metrics) >= 8
    # roost_jobs is a labelled family with one sample per state.
    job_samples = metrics["roost_jobs"]["samples"]
    assert any('state="queued"' in lbl for lbl in job_samples)
    assert len(job_samples) == 6  # all six states always emitted


def test_metrics_values_match_seeded_db(client: TestClient):
    """[R35] seed jobs/workers/blobs/sites/schedules in known states and assert
    /metrics reports the exact numbers (DB-derived)."""
    now = time.time()
    with server._connect(client.app.state.db_path) as conn:
        for st, n in [("queued", 3), ("running", 2), ("succeeded", 5),
                      ("failed", 1)]:
            for i in range(n):
                conn.execute(
                    "INSERT INTO jobs(id, spec, requires, state, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (f"{st}-{i}", "{}", "{}", st, now),
                )
        # 2 online, 1 stale (old last_seen), 1 revoked → only 2 count online,
        # but all 4 count toward total.
        conn.execute("INSERT INTO workers(id,name,capabilities,registered_at,"
                     "last_seen,status) VALUES ('w1','w1','{}',?,?,'idle')", (now, now))
        conn.execute("INSERT INTO workers(id,name,capabilities,registered_at,"
                     "last_seen,status) VALUES ('w2','w2','{}',?,?,'busy')", (now, now))
        conn.execute("INSERT INTO workers(id,name,capabilities,registered_at,"
                     "last_seen,status) VALUES ('w3','w3','{}',?,?,'idle')",
                     (now, now - 999))
        conn.execute("INSERT INTO workers(id,name,capabilities,registered_at,"
                     "last_seen,status,revoked) VALUES ('w4','w4','{}',?,?,'idle',1)",
                     (now, now))
        conn.execute("INSERT INTO blobs(id,name,size,created_at,expires_at) "
                     "VALUES ('b1','f',100,?,?)", (now, now + 999))
        conn.execute("INSERT INTO blobs(id,name,size,created_at,expires_at) "
                     "VALUES ('b2','g',250,?,?)", (now, now + 999))
        conn.execute("INSERT INTO sites(slug,created_at,updated_at) "
                     "VALUES ('s1',?,?)", (now, now))
        conn.execute("INSERT INTO schedules(id,spec,interval_sec,enabled,"
                     "next_run_at,created_at) VALUES ('sc1','{}',60,1,?,?)", (now, now))
        conn.execute("INSERT INTO schedules(id,spec,interval_sec,enabled,"
                     "next_run_at,created_at) VALUES ('sc2','{}',60,0,?,?)", (now, now))
        # Two lease-expired lifecycle events (what the sweeper writes).
        conn.execute("INSERT INTO job_logs(job_id,seq,stream,data,ts) VALUES "
                     "('queued-0',1,'event',?,?)",
                     (json.dumps({"type": "lease_expired", "attempt": 0}), now))
        conn.execute("INSERT INTO job_logs(job_id,seq,stream,data,ts) VALUES "
                     "('queued-0',2,'event',?,?)",
                     (json.dumps({"type": "lease_expired", "attempt": 1}), now))

    metrics = _parse_prometheus(client.get("/metrics").text)

    def sample(name, labels=""):
        key = "{" + labels + "}" if labels else ""
        return metrics[name]["samples"][key]

    assert sample("roost_jobs", 'state="queued"') == 3
    assert sample("roost_jobs", 'state="running"') == 2
    assert sample("roost_jobs", 'state="succeeded"') == 5
    assert sample("roost_jobs", 'state="failed"') == 1
    assert sample("roost_jobs", 'state="cancelled"') == 0
    assert sample("roost_queue_depth") == 3
    assert sample("roost_workers_total") == 4
    assert sample("roost_workers_online") == 2
    assert sample("roost_blobs_count") == 2
    assert sample("roost_blobs_bytes") == 350
    assert sample("roost_sites_count") == 1
    assert sample("roost_schedules_count") == 2
    assert sample("roost_schedules_enabled") == 1
    assert sample("roost_lease_expirations_total") == 2


def test_metrics_requires_admin(client: TestClient):
    """[R35] /metrics is admin-only: a worker credential is rejected (403) and a
    missing/invalid bearer is rejected (401)."""
    _, cred = _enroll_worker(client, {"tools": ["python3"]})
    # Worker credential → 403 (matches require_admin on other janitorial routes).
    r = client.get("/metrics", headers={"Authorization": f"Bearer {cred}"})
    assert r.status_code == 403, r.text
    # Missing bearer → 401.
    r = client.get("/metrics", headers={"Authorization": ""})
    assert r.status_code == 401, r.text
    # Invalid bearer → 401.
    r = client.get("/metrics", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401, r.text
    # Admin (default fixture headers) → 200.
    assert client.get("/metrics").status_code == 200


# ---------- Online backup (`roost backup` / GET /admin/backup, R39) ----------


def _job_count(db: Path) -> int:
    """Open a snapshot/DB file read-only-ish and count rows in `jobs`."""
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()


def test_backup_requires_admin(client: TestClient):
    """[R39] /admin/backup is admin-only, like the other janitorial routes."""
    _, cred = _enroll_worker(client, {"tools": ["python3"]})
    # Worker credential → 403.
    r = client.get("/admin/backup", headers={"Authorization": f"Bearer {cred}"})
    assert r.status_code == 403, r.text
    # Missing bearer → 401.
    r = client.get("/admin/backup", headers={"Authorization": ""})
    assert r.status_code == 401, r.text
    # Invalid bearer → 401.
    r = client.get("/admin/backup", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401, r.text
    # Admin (default fixture headers) → 200 with a non-empty body.
    r = client.get("/admin/backup")
    assert r.status_code == 200, r.text
    assert len(r.content) > 0


def test_backup_roundtrip_readable(client: TestClient, tmp_path: Path):
    """[R39] A backup is a self-contained, valid SQLite DB that round-trips: it
    passes integrity_check and a restored copy is readable with the same fleet
    state (worker + jobs the running CP holds)."""
    worker_id, _ = _enroll_worker(client, {"tools": ["python3"]})
    for i in range(5):
        assert client.post("/jobs", json={"command": f"echo {i}"}).status_code == 200

    r = client.get("/admin/backup")
    assert r.status_code == 200, r.text
    # A sensible download name (version + .db) so archives are self-describing.
    cd = r.headers.get("content-disposition", "")
    assert "roost-backup-" in cd and ".db" in cd

    restored = tmp_path / "restored.db"
    restored.write_bytes(r.content)

    conn = sqlite3.connect(restored)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # State the running CP holds is present in the snapshot.
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5
        row = conn.execute(
            "SELECT name FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_backup_consistent_under_concurrent_writes(tmp_path: Path):
    """[R39] The core guarantee: a snapshot taken while writes are in flight is
    internally consistent (integrity_check passes), and its row count is sane —
    between the count before the writes started and a final quiesced copy. A naive
    file copy under WAL could capture a torn state; the online backup API cannot.
    """
    db = tmp_path / "roost.db"
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})

        # Seed a baseline so the backup is never racing an empty DB.
        for i in range(10):
            assert c.post("/jobs", json={"command": f"seed {i}"}).status_code == 200
        baseline = _job_count(db)
        assert baseline == 10

        stop = threading.Event()
        errors: list[str] = []

        def hammer() -> None:
            n = 0
            while not stop.is_set() and n < 200:
                resp = c.post("/jobs", json={"command": f"w {n}"})
                if resp.status_code != 200:
                    errors.append(resp.text)
                    return
                n += 1

        writer = threading.Thread(target=hammer)
        writer.start()
        try:
            # Take several backups while writes are actively in flight.
            snapshots: list[Path] = []
            for k in range(3):
                r = c.get("/admin/backup")
                assert r.status_code == 200, r.text
                snap = tmp_path / f"snap-{k}.db"
                snap.write_bytes(r.content)
                snapshots.append(snap)
        finally:
            stop.set()
            writer.join(timeout=10)
        assert not errors, f"writer hit errors: {errors[:3]}"
        assert not writer.is_alive()

        final = _job_count(db)
        assert final > baseline  # writes actually happened during the window

        for snap in snapshots:
            conn = sqlite3.connect(snap)
            try:
                assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            finally:
                conn.close()
            # A consistent point-in-time snapshot: at least the baseline, never
            # more than the fully-quiesced final count.
            assert baseline <= count <= final, (
                f"snapshot row count {count} outside [{baseline}, {final}]"
            )


def test_backup_leaves_no_temp_file_behind(client: TestClient, tmp_path: Path):
    """[R39] The server-side temp snapshot is cleaned up after the response is
    streamed — repeated backups must not accumulate files in the temp dir."""
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("roost-backup-*.db"))
    for _ in range(3):
        assert client.get("/admin/backup").status_code == 200
    after = set(Path(tempfile.gettempdir()).glob("roost-backup-*.db"))
    # No new roost-backup temp files linger once the responses have been sent.
    assert after <= before


def test_backup_temp_file_cleaned_up_on_client_disconnect(client: TestClient):
    """[R39] The snapshot is streamed from a generator whose `finally` deletes the
    temp file, so even a client that disconnects mid-download leaves nothing behind
    (a leaked file would be a full copy of the fleet DB in the temp dir)."""
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("roost-backup-*.db"))
    # Open the stream but abandon it without reading the body — a client disconnect.
    with client.stream("GET", "/admin/backup") as resp:
        assert resp.status_code == 200
        # leave the context without iter_bytes(): closing the response tears down
        # the server-side generator (GeneratorExit → finally → unlink).
    after = set(Path(tempfile.gettempdir()).glob("roost-backup-*.db"))
    assert after <= before
