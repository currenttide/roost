"""Tests for the `schedule` verb (R8): interval jobs enqueued by the CP tick.

Covers the schedule CRUD surface, the tick semantics (due / overdue-no-backfill /
disabled / no-pile-up / broken-spec), permissions, and interval parsing.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roost import server
from roost.server import _tick_schedules, parse_every

TOKEN = "test-admin-token"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "roost.db"


@pytest.fixture()
def client(db: Path):
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _mk(client: TestClient, **kw) -> dict:
    body = {"spec": {"kind": "command", "command": "echo tick"},
            "every": "30m", **kw}
    r = client.post("/schedules", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _force_due(client: TestClient, db: Path, sched_id: str,
               next_run_at: float) -> None:
    """Backdate next_run_at (tests can't wait out a real interval)."""
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE schedules SET next_run_at=? WHERE id=?",
                 (next_run_at, sched_id))
    conn.commit()
    conn.close()


# ---------- parse_every ----------


def test_parse_every_units_and_numbers():
    assert parse_every("30s") == 30
    assert parse_every("5m") == 300
    assert parse_every("2h") == 7200
    assert parse_every("1d") == 86400
    assert parse_every("90") == 90.0
    assert parse_every(120) == 120.0
    assert parse_every(45.5) == 45.5
    assert parse_every("1.5h") == 5400


def test_parse_every_garbage():
    assert parse_every("soon") is None
    assert parse_every("5 fortnights") is None
    assert parse_every(None) is None
    assert parse_every(True) is None
    assert parse_every({"every": 5}) is None


# ---------- CRUD surface ----------


def test_create_and_list(client: TestClient):
    s = _mk(client, name="ticker")
    assert s["interval_sec"] == 1800
    assert s["enabled"] is True
    assert s["name"] == "ticker"
    assert s["last_job_id"] is None
    # First run is one interval out.
    assert s["next_run_at"] == pytest.approx(s["created_at"] + 1800, abs=5)

    rows = client.get("/schedules").json()
    assert [r["id"] for r in rows] == [s["id"]]
    assert rows[0]["spec"] == {"kind": "command", "command": "echo tick"}


def test_create_validates_interval(client: TestClient):
    r = client.post("/schedules", json={
        "spec": {"command": "echo x"}, "every": "soon"})
    assert r.status_code == 400
    r = client.post("/schedules", json={
        "spec": {"command": "echo x"}, "every": "5s"})  # under the 30s floor
    assert r.status_code == 400
    assert ">=" in r.json()["detail"]


def test_create_validates_spec(client: TestClient):
    # Same shape rules as POST /jobs.
    r = client.post("/schedules", json={"spec": {}, "every": "30m"})
    assert r.status_code == 400
    r = client.post("/schedules", json={
        "spec": {"kind": "auto"}, "every": "30m"})
    assert r.status_code == 400
    # Schedules mint ROOT jobs only.
    r = client.post("/schedules", json={
        "spec": {"command": "x", "parent_job_id": "abc"}, "every": "30m"})
    assert r.status_code == 400
    assert "root" in r.json()["detail"]


def test_delete(client: TestClient):
    s = _mk(client)
    r = client.delete(f"/schedules/{s['id']}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/schedules").json() == []
    assert client.delete(f"/schedules/{s['id']}").status_code == 404


def test_enable_disable_roundtrip(client: TestClient, db: Path):
    s = _mk(client)
    r = client.patch(f"/schedules/{s['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    # Re-enable restarts the clock: next run ~one interval from now, so a
    # long-disabled schedule can't fire the moment it's re-enabled.
    _force_due(client, db, s["id"], time.time() - 9999)
    r = client.patch(f"/schedules/{s['id']}", json={"enabled": True})
    assert r.json()["enabled"] is True
    assert r.json()["next_run_at"] == pytest.approx(time.time() + 1800, abs=5)
    assert client.patch("/schedules/nope", json={"enabled": True}).status_code == 404


# ---------- permissions ----------


def test_client_token_can_manage_worker_cannot(client: TestClient):
    tok = client.post("/pair-tokens", json={"label": "phone"}).json()
    mh = {"Authorization": f"Bearer {tok['token']}"}
    r = client.post("/schedules", json={
        "spec": {"command": "echo hi"}, "every": "30m"}, headers=mh)
    assert r.status_code == 200, r.text
    assert client.get("/schedules", headers=mh).status_code == 200

    # A worker credential is the worker PLANE: it may not mint standing load.
    et = client.post("/enroll-tokens", json={"label": "t"}).json()["token"]
    cred = client.post("/enroll", json={
        "token": et, "name": "w1", "capabilities": {}},
        headers={"Authorization": ""}).json()["credential"]
    wh = {"Authorization": f"Bearer {cred}"}
    assert client.post("/schedules", json={
        "spec": {"command": "x"}, "every": "30m"}, headers=wh).status_code == 403
    assert client.get("/schedules", headers=wh).status_code == 403

    r = client.get("/schedules", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


# ---------- the tick ----------


def test_tick_due_schedule_enqueues_job(client: TestClient, db: Path):
    s = _mk(client, name="due")
    _force_due(client, db, s["id"], time.time() - 1)
    assert _tick_schedules(db) == 1

    jobs = client.get("/jobs").json()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["state"] == "queued"
    assert job["spec"]["command"] == "echo tick"
    assert job["spec"]["schedule_id"] == s["id"]  # provenance

    row = client.get("/schedules").json()[0]
    assert row["last_job_id"] == job["id"]
    assert row["last_run_at"] is not None
    assert row["next_run_at"] > time.time()


def test_tick_not_due_does_nothing(client: TestClient, db: Path):
    _mk(client)  # next_run_at is one interval out
    assert _tick_schedules(db) == 0
    assert client.get("/jobs").json() == []


def test_tick_overdue_no_backfill_preserves_cadence(client: TestClient, db: Path):
    # CP "down" for many intervals: exactly ONE job, and next_run_at advances
    # by whole intervals from the ORIGINAL grid (cadence preserved, no drift).
    s = _mk(client)  # 1800s interval
    origin = time.time() - 10_000  # ~5.5 intervals overdue
    _force_due(client, db, s["id"], origin)
    assert _tick_schedules(db) == 1
    assert len(client.get("/jobs").json()) == 1

    row = client.get("/schedules").json()[0]
    assert row["next_run_at"] > time.time()
    # On the original grid: next_run_at - origin is a whole multiple of 1800.
    assert (row["next_run_at"] - origin) % 1800 == pytest.approx(0, abs=1e-6)
    assert row["next_run_at"] - time.time() <= 1800 + 5


def test_tick_disabled_schedule_never_fires(client: TestClient, db: Path):
    s = _mk(client)
    client.patch(f"/schedules/{s['id']}", json={"enabled": False})
    _force_due(client, db, s["id"], time.time() - 1)
    assert _tick_schedules(db) == 0
    assert client.get("/jobs").json() == []


def test_tick_skips_beat_while_previous_run_in_flight(client: TestClient, db: Path):
    s = _mk(client)
    _force_due(client, db, s["id"], time.time() - 1)
    assert _tick_schedules(db) == 1  # first run enqueued (stays 'queued')

    _force_due(client, db, s["id"], time.time() - 1)
    assert _tick_schedules(db) == 0  # beat skipped: previous still in flight
    assert len(client.get("/jobs").json()) == 1
    row = client.get("/schedules").json()[0]
    assert row["next_run_at"] > time.time()  # clock advanced anyway

    # Previous run finished → the next due beat fires again.
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE jobs SET state='succeeded'")
    conn.commit()
    conn.close()
    _force_due(client, db, s["id"], time.time() - 1)
    assert _tick_schedules(db) == 1
    assert len(client.get("/jobs").json()) == 2


def test_tick_broken_spec_skips_and_advances(client: TestClient, db: Path):
    # A spec the insert path rejects must not break the tick (or the sweep
    # loop riding it); the clock advances so it doesn't hot-loop.
    s = _mk(client)
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE schedules SET spec='{\"kind\": \"command\"' WHERE id=?",
                 (s["id"],))  # truncated JSON
    conn.commit()
    conn.close()
    _force_due(client, db, s["id"], time.time() - 1)
    assert _tick_schedules(db) == 0
    assert client.get("/jobs").json() == []
    # The list endpoint stays usable (tolerant decode) and the clock advanced.
    row = client.get("/schedules").json()[0]
    assert row["spec"] == {}
    assert row["next_run_at"] > time.time()
