"""Server-side V1 flow tests.

Exercise the control plane through its HTTP surface (TestClient) for the
enrollment + job-lifecycle path, and through the internal helpers for the
hierarchy guardrails (depth, tree budget, subtree cancel) that roost-mcp
relies on.
"""

from __future__ import annotations

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
