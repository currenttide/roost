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


def test_parse_every_rejects_non_finite():
    # R100: a non-finite interval (inf/-inf/nan) is not usable and must be
    # rejected at the door — otherwise `inf < 30` / `nan < 30` are both False
    # so it slips past the >= floor guard and poisons the schedules table.
    # Covers both how it can arrive: string forms and a bare JSON number that
    # overflows to inf (e.g. 1e999 → float('inf')).
    assert parse_every("inf") is None
    assert parse_every("-inf") is None
    assert parse_every("nan") is None
    assert parse_every("1e400") is None        # string overflow → inf
    assert parse_every(float("inf")) is None   # bare JSON number 1e999
    assert parse_every(float("-inf")) is None
    assert parse_every(float("nan")) is None


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


def test_create_non_finite_interval_rejected_no_poison_row(client: TestClient):
    # R100 (A1 hunt #10, F1): `every: "inf"` (or any value parsing to a
    # non-finite float — "1e400", or a bare JSON number 1e999 that overflows
    # to inf) used to bypass the floor guard (`inf < 30` is False), commit a
    # poison row, then 500 on JSON render — wedging GET /schedules FOREVER for
    # every client. It must now be a clean 400 with NO row committed, and the
    # list endpoint must stay 200 throughout.
    for ev in ("inf", "-inf", "1e400"):
        r = client.post("/schedules", json={
            "spec": {"command": "echo x"}, "every": ev})
        assert r.status_code == 400, f"{ev!r} accepted: {r.status_code}: {r.text}"
        assert client.get("/schedules").status_code == 200

    # The bare JSON number path: 1e999 deserializes to float('inf') (not a
    # string), so it must be caught on the int/float branch too.
    r = client.post(
        "/schedules",
        content=b'{"spec": {"command": "echo x"}, "every": 1e999}',
        headers={"content-type": "application/json"})
    assert r.status_code == 400, f"1e999 accepted: {r.status_code}: {r.text}"

    # No poison row leaked: the list is empty and stays 200, and a later GOOD
    # schedule lists cleanly (proving the surface was never wedged).
    assert client.get("/schedules").json() == []
    good = client.post("/schedules", json={
        "spec": {"command": "ok"}, "every": "30m"})
    assert good.status_code == 200, good.text
    lr = client.get("/schedules")
    assert lr.status_code == 200
    assert [s["id"] for s in lr.json()] == [good.json()["id"]]


def test_create_nan_interval_rejected(client: TestClient):
    # R100 (A1 hunt #10, F2): `every: "nan"` returned nan; `nan < 30` is False
    # so the floor guard was bypassed; the INSERT then violated interval_sec
    # NOT NULL (SQLite coerces nan → NULL) → IntegrityError → 500 at create.
    # Now a clean 400, no row committed.
    r = client.post("/schedules", json={
        "spec": {"command": "echo x"}, "every": "nan"})
    assert r.status_code == 400, f"nan accepted: {r.status_code}: {r.text}"
    assert client.get("/schedules").status_code == 200
    assert client.get("/schedules").json() == []


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


def test_mobile_scope_manages_schedules_end_to_end(client: TestClient):
    # [R40] The mobile-app contract (mobile-app/API.md §7) pins this: a
    # mobile-scoped pair token manages schedules through the SAME client
    # permission set as agent scope — scope is an audit label, not a privilege
    # boundary (see the scope→verbs matrix in server.py; R6 settled the same
    # question for publish). Phone front doors schedule recurring work.
    tok = client.post("/pair-tokens", json={"label": "phone"}).json()
    assert tok["scope"] == "mobile"  # the default scope
    mh = {"Authorization": f"Bearer {tok['token']}"}

    # create AS the phone (not as admin)
    r = client.post("/schedules", json={
        "spec": {"intent": "tidy the repo", "kind": "claude",
                 "requires": {}, "hierarchy": {"can_dispatch": True}},
        "every": "30m", "name": "nightly-tidy"}, headers=mh)
    assert r.status_code == 200, r.text
    sched = r.json()
    assert sched["enabled"] is True
    assert sched["interval_sec"] == 1800
    assert sched["last_job_id"] is None
    sid = sched["id"]

    # list AS the phone — the schedule shows up
    r = client.get("/schedules", headers=mh)
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == [sid]

    # disable then re-enable AS the phone
    r = client.patch(f"/schedules/{sid}", json={"enabled": False}, headers=mh)
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = client.patch(f"/schedules/{sid}", json={"enabled": True}, headers=mh)
    assert r.status_code == 200 and r.json()["enabled"] is True

    # delete AS the phone
    r = client.delete(f"/schedules/{sid}", headers=mh)
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/schedules", headers=mh).json() == []


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
