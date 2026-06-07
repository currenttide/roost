"""Record golden JSON fixtures for the mobile apps.

Spins up an in-process control plane (TestClient, no network), drives a tiny
fleet through enroll → submit → run → log → finish, and snapshots every
response shape the phone apps consume into mobile-app/fixtures/.

Re-run after any server change that touches these shapes:

    python mobile-app/record_fixtures.py

Both app test suites parse these files; if a shape drifts, regenerate and the
app-side decode tests tell you exactly what broke. The drift GUARD
(tests/test_fixture_drift.py) imports ``capture()`` below and compares a live
run's shapes against the committed goldens on every pytest run — additive-only
(API.md §8): the server may add fields, but a removed/renamed field fails CI.
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from roost import server  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOKEN = "fixture-admin-token"


def capture(db_path: Path) -> dict[str, Any]:
    """Drive the canonical fixture scenario; return {fixture_name: payload}.

    JSON fixtures map to their decoded objects; the SSE transcript maps to its
    raw text. This is the single source of the scenario — ``main()`` writes
    the goldens from it, the drift guard replays it for live comparison.
    """
    db_path.unlink(missing_ok=True)
    app = server.create_app(db_path=db_path, token=TOKEN, run_sweeper=False)
    out: dict[str, Any] = {}

    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})

        # -- pairing ------------------------------------------------------
        r = c.post("/pair-tokens", json={"label": "fixture-phone"})
        pair = r.json()
        out["pair_token_response.json"] = pair
        mh = {"Authorization": f"Bearer {pair['token']}"}

        # -- a worker -----------------------------------------------------
        r = c.post("/enroll-tokens", json={"label": "fixture"})
        et = r.json()["token"]
        r = c.post("/enroll", json={"token": et, "name": "fixture-node",
                                    "capabilities": {"tools": ["python3"], "cpus": 4}},
                   headers={"Authorization": ""})
        wid, cred = r.json()["worker_id"], r.json()["credential"]
        wh = {"Authorization": f"Bearer {cred}"}

        # -- jobs in three states (submitted AS the mobile token) ----------
        # 1. agent job driven to success with logs
        r = c.post("/jobs", headers=mh, json={
            "intent": "fix the flaky auth test in roost-oss",
            "kind": "claude",
            "requires": {"tools": ["python3"]},
            "hierarchy": {"can_dispatch": True},
        })
        job_submit_resp = r.json()
        done_id = job_submit_resp["id"]
        out["job_submit_response.json"] = job_submit_resp

        a = c.get(f"/workers/{wid}/poll", params={"timeout": 0}, headers=wh).json()
        c.post(f"/workers/{wid}/jobs/{done_id}/event",
               json={"type": "started", "attempt": a["attempt"]}, headers=wh)
        for line in ("running pytest -q ...", "2 failed, re-reading test_auth.py",
                     "editing tests/test_auth.py", "re-running: all green"):
            c.post(f"/workers/{wid}/jobs/{done_id}/logs",
                   json={"stream": "stdout", "data": line}, headers=wh)
        c.post(f"/workers/{wid}/jobs/{done_id}/event",
               json={"type": "succeeded", "attempt": a["attempt"], "exit_code": 0,
                     "tokens_used": 48213,
                     "result": {"output": "fixed: tests green", "verified": True,
                                "evidence": "pytest -q: 255 passed"}},
               headers=wh)

        # 2. running job (leased, one log line, never finished)
        r = c.post("/jobs", headers=mh, json={
            "intent": "bump deps and run the test suite",
            "kind": "claude", "requires": {"tools": ["python3"]},
            "hierarchy": {"can_dispatch": True},
        })
        running_id = r.json()["id"]
        a2 = c.get(f"/workers/{wid}/poll", params={"timeout": 0}, headers=wh).json()
        c.post(f"/workers/{wid}/jobs/{running_id}/event",
               json={"type": "started", "attempt": a2["attempt"]}, headers=wh)
        c.post(f"/workers/{wid}/jobs/{running_id}/logs",
               json={"stream": "stdout", "data": "uv lock --upgrade ..."}, headers=wh)

        # 3. queued job no worker can take (unplaceable in derived)
        r = c.post("/jobs", headers=mh, json={
            "intent": "train on the gpu box", "kind": "claude",
            "requires": {"gpu_vram_gb": ">=24"},
            "hierarchy": {"can_dispatch": True},
        })
        queued_id = r.json()["id"]

        # -- snapshots the apps render -------------------------------------
        out["derived.json"] = c.get("/derived", headers=mh).json()
        out["jobs_list.json"] = c.get("/jobs", headers=mh).json()
        out["job_detail_succeeded.json"] = c.get(f"/jobs/{done_id}", headers=mh).json()
        out["job_detail_running.json"] = c.get(f"/jobs/{running_id}", headers=mh).json()
        out["job_detail_queued.json"] = c.get(f"/jobs/{queued_id}", headers=mh).json()
        out["job_logs.json"] = c.get(f"/jobs/{done_id}/logs", headers=mh).json()
        out["job_logs_since_2.json"] = c.get(
            f"/jobs/{done_id}/logs", params={"since": 2}, headers=mh).json()
        out["job_tree.json"] = c.get(f"/jobs/{done_id}/tree", headers=mh).json()
        out["job_derived_running.json"] = c.get(
            f"/jobs/{running_id}/derived", headers=mh).json()
        out["workers.json"] = c.get("/workers", headers=mh).json()
        out["healthz.json"] = c.get("/healthz").json()

        # SSE transcript of a finished job (terminates at event: done).
        with c.stream("GET", f"/jobs/{done_id}/stream", headers=mh) as resp:
            out["stream_succeeded.sse.txt"] = "".join(
                chunk for chunk in resp.iter_text())

        # Cancel response (cancel the queued one, as mobile).
        out["job_cancel_response.json"] = c.delete(
            f"/jobs/{queued_id}", headers=mh).json()

        # -- publish (API.md §6) — entirely AS the mobile token ------------
        def _bundle(page: bytes) -> bytes:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                info = tarfile.TarInfo("index.html")
                info.size = len(page)
                tar.addfile(info, io.BytesIO(page))
            return buf.getvalue()

        # Two-step flow: stage a blob, then publish by blob_id.
        r = c.post("/blobs", params={"name": "phone-site.tar.gz"},
                   content=_bundle(b"<h1>shipped from the phone</h1>"), headers=mh)
        blob = r.json()
        out["blob_upload_response.json"] = blob
        out["publish_response.json"] = c.post(
            "/publish", json={"blob_id": blob["id"]}, headers=mh).json()

        # One-shot flow (R7): the bundle IS the body, no staged blob; the
        # slug comes from ?name=. Same Site response shape as the two-step path.
        out["publish_oneshot_response.json"] = c.post(
            "/publish", params={"name": "phone-oneshot"},
            content=_bundle(b"<h1>one-shot from the phone</h1>"),
            headers={**mh, "Content-Type": "application/gzip"}).json()

        out["publish_list.json"] = c.get("/publish", headers=mh).json()

        # Error shapes the apps must handle.
        out["error_401.json"] = c.get(
            "/jobs", headers={"Authorization": "Bearer nope"}).json()
        out["error_403_admin_endpoint.json"] = c.get("/pair-tokens", headers=mh).json()
        out["error_404_job.json"] = c.get("/jobs/nope", headers=mh).json()

    db_path.unlink(missing_ok=True)
    return out


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    captured = capture(Path("/tmp/roost-fixtures.db.tmp"))
    for name, obj in captured.items():
        if name.endswith(".json"):
            (FIXTURES / name).write_text(
                json.dumps(obj, indent=2, sort_keys=True) + "\n")
        else:
            (FIXTURES / name).write_text(obj)
        print(f"  wrote fixtures/{name}")
    print("done.")


if __name__ == "__main__":
    main()
