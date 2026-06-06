"""Record golden JSON fixtures for the mobile apps.

Spins up an in-process control plane (TestClient, no network), drives a tiny
fleet through enroll → submit → run → log → finish, and snapshots every
response shape the phone apps consume into mobile-app/fixtures/.

Re-run after any server change that touches these shapes:

    python mobile-app/record_fixtures.py

Both app test suites parse these files; if a shape drifts, regenerate and the
app-side decode tests tell you exactly what broke.
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from roost import server  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOKEN = "fixture-admin-token"


def _dump(name: str, obj) -> None:
    (FIXTURES / name).write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    print(f"  wrote fixtures/{name}")


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    app = server.create_app(db_path=Path("/tmp/roost-fixtures.db.tmp"), token=TOKEN,
                            run_sweeper=False)
    # Fresh DB every run.
    Path("/tmp/roost-fixtures.db.tmp").unlink(missing_ok=True)
    app = server.create_app(db_path=Path("/tmp/roost-fixtures.db.tmp"), token=TOKEN,
                            run_sweeper=False)

    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})

        # -- pairing ------------------------------------------------------
        r = c.post("/pair-tokens", json={"label": "fixture-phone"})
        pair = r.json()
        _dump("pair_token_response.json", pair)
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
        _dump("job_submit_response.json", job_submit_resp)

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
        _dump("derived.json", c.get("/derived", headers=mh).json())
        _dump("jobs_list.json", c.get("/jobs", headers=mh).json())
        _dump("job_detail_succeeded.json", c.get(f"/jobs/{done_id}", headers=mh).json())
        _dump("job_detail_running.json", c.get(f"/jobs/{running_id}", headers=mh).json())
        _dump("job_detail_queued.json", c.get(f"/jobs/{queued_id}", headers=mh).json())
        _dump("job_logs.json", c.get(f"/jobs/{done_id}/logs", headers=mh).json())
        _dump("job_logs_since_2.json",
              c.get(f"/jobs/{done_id}/logs", params={"since": 2}, headers=mh).json())
        _dump("job_tree.json", c.get(f"/jobs/{done_id}/tree", headers=mh).json())
        _dump("job_derived_running.json",
              c.get(f"/jobs/{running_id}/derived", headers=mh).json())
        _dump("workers.json", c.get("/workers", headers=mh).json())
        _dump("healthz.json", c.get("/healthz").json())

        # SSE transcript of a finished job (terminates at event: done).
        with c.stream("GET", f"/jobs/{done_id}/stream", headers=mh) as resp:
            sse = "".join(chunk for chunk in resp.iter_text())
        (FIXTURES / "stream_succeeded.sse.txt").write_text(sse)
        print("  wrote fixtures/stream_succeeded.sse.txt")

        # Cancel response (cancel the queued one, as mobile).
        _dump("job_cancel_response.json",
              c.delete(f"/jobs/{queued_id}", headers=mh).json())

        # -- publish (API.md §6) — entirely AS the mobile token ------------
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            page = b"<h1>shipped from the phone</h1>"
            info = tarfile.TarInfo("index.html")
            info.size = len(page)
            tar.addfile(info, io.BytesIO(page))
        r = c.post("/blobs", params={"name": "phone-site.tar.gz"},
                   content=buf.getvalue(), headers=mh)
        blob = r.json()
        _dump("blob_upload_response.json", blob)
        _dump("publish_response.json",
              c.post("/publish", json={"blob_id": blob["id"]},
                     headers=mh).json())
        _dump("publish_list.json", c.get("/publish", headers=mh).json())

        # Error shapes the apps must handle.
        _dump("error_401.json",
              c.get("/jobs", headers={"Authorization": "Bearer nope"}).json())
        _dump("error_403_admin_endpoint.json",
              c.get("/pair-tokens", headers=mh).json())
        _dump("error_404_job.json", c.get("/jobs/nope", headers=mh).json())

    Path("/tmp/roost-fixtures.db.tmp").unlink(missing_ok=True)
    print("done.")


if __name__ == "__main__":
    main()
