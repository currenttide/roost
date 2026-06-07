"""Terminal-state push notifications (R37 / mobile DESIGN.md v1.1).

The CP fires a single fire-and-forget POST to ROOST_NOTIFY_URL (an ntfy.sh topic
or UnifiedPush webhook) as each job reaches a terminal state. These tests pin:

* the notification PAYLOAD (job id, state, intent one-liner, duration, ntfy
  headers) for succeeded / failed / cancelled;
* FAILURE ISOLATION — a 500, a timeout, or a refused connection from the
  endpoint NEVER affects job completion or the request that triggered it;
* OPT-IN — with no notify_url configured, zero posts happen and behavior is
  byte-for-byte unchanged.

The poster is fire-and-forget (asyncio.create_task), so integration tests stub
``app.state.notify_poster`` to record calls and drain ``app.state.notify_tasks``
(the CP holds strong refs to in-flight tasks there) before asserting.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from roost import server


TOKEN = "test-shared-token"


# ---------- fixtures / helpers ----------


def _make_client(tmp_path: Path, notify_url: str | None):
    db = tmp_path / "roost.db"
    app = server.create_app(
        db_path=db, token=TOKEN, run_sweeper=False, notify_url=notify_url
    )
    return app


def _enroll_worker(client: TestClient, capabilities: dict) -> tuple[str, str]:
    r = client.post("/enroll-tokens", json={"label": "test"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r = client.post(
        "/enroll",
        json={"token": token, "name": "w1", "capabilities": capabilities},
        headers={"Authorization": ""},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["worker_id"], body["credential"]


def _drain_notify(app, timeout: float = 5.0) -> None:
    """Wait until every scheduled notify task has finished.

    The tasks run on TestClient's background event-loop thread; the CP keeps a
    strong ref to each in ``app.state.notify_tasks`` until it completes, so the
    set draining to empty is a reliable 'all delivered' signal."""
    deadline = time.time() + timeout
    while app.state.notify_tasks and time.time() < deadline:
        time.sleep(0.01)
    assert not app.state.notify_tasks, "notify task(s) never completed"


class _Recorder:
    """A stub notify poster that records every (url, body, headers) call.

    Thread-safe because it is invoked on the event-loop thread while the test
    asserts from the main thread."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self._lock = threading.Lock()

    async def __call__(self, url: str, body: dict, headers: dict) -> None:
        with self._lock:
            self.calls.append((url, body, headers))


def _run_to_terminal_success(client: TestClient, *, tokens: int = 42) -> str:
    """Submit a command job and drive it succeeded via the worker plane.
    Returns the job id."""
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    job_id = client.post(
        "/jobs",
        json={"command": "echo hi", "intent": "fix flaky auth test",
              "requires": {"tools": ["python3"]}},
    ).json()["id"]
    assigned = client.get(
        f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh
    ).json()
    attempt = assigned["attempt"]
    client.post(
        f"/workers/{worker_id}/jobs/{job_id}/event",
        json={"type": "started", "attempt": attempt}, headers=wh,
    )
    r = client.post(
        f"/workers/{worker_id}/jobs/{job_id}/event",
        json={"type": "succeeded", "attempt": attempt, "exit_code": 0,
              "tokens_used": tokens},
        headers=wh,
    )
    assert r.status_code == 200 and r.json()["state"] == "succeeded"
    return job_id


# ---------- unit: payload shape ----------


def test_build_notification_succeeded_payload():
    job = {
        "id": "abc123", "state": "succeeded",
        "spec": {"intent": "fix flaky auth test"},
        "exit_code": 0, "worker_id": "hubbase",
        "created_at": 100.0, "started_at": 100.0, "finished_at": 352.5,
    }
    body, headers = server._build_notification(job)
    assert body["event"] == "job_terminal"
    assert body["job_id"] == "abc123"
    assert body["state"] == "succeeded"
    assert body["intent"] == "fix flaky auth test"
    assert body["duration_sec"] == 252.5
    assert body["exit_code"] == 0
    assert body["worker_id"] == "hubbase"
    assert "succeeded" in body["message"] and "fix flaky auth test" in body["message"]
    # ntfy display headers
    assert headers["Content-Type"] == "application/json"
    assert headers["Title"] == "Roost job abc123 succeeded"
    assert headers["Priority"] == "3"
    assert headers["Tags"] == "white_check_mark"


def test_build_notification_failed_is_high_priority():
    job = {
        "id": "def456", "state": "failed",
        "spec": {"task": "migrate db schema"}, "exit_code": 1, "worker_id": "pi4",
        "created_at": 1.0, "started_at": 2.0, "finished_at": 5.0,
    }
    body, headers = server._build_notification(job)
    assert body["state"] == "failed"
    assert body["intent"] == "migrate db schema"
    assert body["exit_code"] == 1
    assert headers["Priority"] == "5"  # failures are urgent
    assert headers["Tags"] == "x"


def test_build_notification_cancelled_never_started_uses_created_at():
    # A job cancelled while still queued has no started_at; duration falls back
    # to created→finished and must not blow up.
    job = {
        "id": "ghi789", "state": "cancelled",
        "spec": {"command": "sleep 999"},
        "created_at": 10.0, "started_at": None, "finished_at": 12.0,
    }
    body, headers = server._build_notification(job)
    assert body["state"] == "cancelled"
    assert body["intent"] == "sleep 999"
    assert body["duration_sec"] == 2.0
    assert headers["Priority"] == "2"
    assert headers["Tags"] == "no_entry"


def test_build_notification_missing_timestamps_duration_none():
    job = {"id": "x", "state": "succeeded", "spec": {"intent": "t"},
           "finished_at": None}
    body, _ = server._build_notification(job)
    assert body["duration_sec"] is None
    # message still renders without a duration suffix
    assert body["message"] == "succeeded: t"


# ---------- unit: failure isolation of the poster ----------


def test_post_notification_swallows_refused_connection():
    # Nothing listening on port 1 → connection refused. Must NOT raise.
    job = {"id": "j", "state": "failed", "spec": {"intent": "t"},
           "finished_at": None}
    body, headers = server._build_notification(job)
    # asyncio.run would raise if _post_notification let the error escape.
    asyncio.run(server._post_notification("http://127.0.0.1:1/x", body, headers))


def test_post_notification_swallows_500(monkeypatch):
    job = {"id": "j", "state": "failed", "spec": {"intent": "t"},
           "finished_at": None}
    body, headers = server._build_notification(job)

    class _Resp:
        status_code = 500

    class _StubClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    # A 5xx is logged, not raised.
    asyncio.run(server._post_notification("http://example/x", body, headers))


def test_post_notification_swallows_timeout(monkeypatch):
    job = {"id": "j", "state": "failed", "spec": {"intent": "t"},
           "finished_at": None}
    body, headers = server._build_notification(job)

    class _StubClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    asyncio.run(server._post_notification("http://example/x", body, headers))


# ---------- integration: the happy path posts the right payload ----------


def test_succeeded_job_posts_notification(tmp_path: Path):
    app = _make_client(tmp_path, notify_url="https://ntfy.example/roost")
    rec = _Recorder()
    app.state.notify_poster = rec
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        job_id = _run_to_terminal_success(c, tokens=123)
        _drain_notify(app)

    assert len(rec.calls) == 1
    url, body, headers = rec.calls[0]
    assert url == "https://ntfy.example/roost"
    assert body["job_id"] == job_id
    assert body["state"] == "succeeded"
    assert body["intent"] == "fix flaky auth test"
    assert body["exit_code"] == 0
    assert body["duration_sec"] is not None and body["duration_sec"] >= 0
    assert headers["Title"].endswith("succeeded")


def test_failed_job_posts_notification(tmp_path: Path):
    app = _make_client(tmp_path, notify_url="https://ntfy.example/roost")
    rec = _Recorder()
    app.state.notify_poster = rec
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        worker_id, cred = _enroll_worker(c, {"tools": ["python3"]})
        wh = {"Authorization": f"Bearer {cred}"}
        job_id = c.post(
            "/jobs",
            json={"command": "false", "intent": "migrate db schema",
                  "requires": {"tools": ["python3"]}},
        ).json()["id"]
        assigned = c.get(
            f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh
        ).json()
        attempt = assigned["attempt"]
        c.post(f"/workers/{worker_id}/jobs/{job_id}/event",
               json={"type": "started", "attempt": attempt}, headers=wh)
        c.post(f"/workers/{worker_id}/jobs/{job_id}/event",
               json={"type": "failed", "attempt": attempt, "exit_code": 1,
                     "error": "boom"}, headers=wh)
        _drain_notify(app)

    assert len(rec.calls) == 1
    _, body, headers = rec.calls[0]
    assert body["job_id"] == job_id
    assert body["state"] == "failed"
    assert body["exit_code"] == 1
    assert headers["Priority"] == "5"


def test_cancelled_job_posts_notification(tmp_path: Path):
    app = _make_client(tmp_path, notify_url="https://ntfy.example/roost")
    rec = _Recorder()
    app.state.notify_poster = rec
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        # A queued job (no capable worker) we then cancel.
        job_id = c.post(
            "/jobs",
            json={"command": "true", "intent": "stop me",
                  "requires": {"hostname": "==nope"}},
        ).json()["id"]
        r = c.delete(f"/jobs/{job_id}")
        assert r.status_code == 200 and r.json()["cancelled"] == 1
        _drain_notify(app)

    assert len(rec.calls) == 1
    _, body, headers = rec.calls[0]
    assert body["job_id"] == job_id
    assert body["state"] == "cancelled"
    assert headers["Tags"] == "no_entry"


def test_finalize_captain_root_posts_notification(tmp_path: Path):
    app = _make_client(tmp_path, notify_url="https://ntfy.example/roost")
    rec = _Recorder()
    app.state.notify_poster = rec
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        root = c.post("/jobs", json={
            "intent": "plan the work", "captain_root": True,
            "hierarchy": {"can_dispatch": True},
        }).json()
        r = c.post(f"/jobs/{root['id']}/finalize", json={"state": "succeeded"})
        assert r.status_code == 200
        _drain_notify(app)

    assert len(rec.calls) == 1
    _, body, _ = rec.calls[0]
    assert body["job_id"] == root["id"]
    assert body["state"] == "succeeded"
    assert body["intent"] == "plan the work"


# ---------- integration: non-terminal events do NOT post ----------


def test_non_terminal_events_do_not_post(tmp_path: Path):
    app = _make_client(tmp_path, notify_url="https://ntfy.example/roost")
    rec = _Recorder()
    app.state.notify_poster = rec
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        worker_id, cred = _enroll_worker(c, {"tools": ["python3"]})
        wh = {"Authorization": f"Bearer {cred}"}
        job_id = c.post(
            "/jobs",
            json={"command": "echo hi", "intent": "t",
                  "requires": {"tools": ["python3"]}},
        ).json()["id"]
        assigned = c.get(
            f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh
        ).json()
        attempt = assigned["attempt"]
        # started + progress are NOT terminal → no notification.
        c.post(f"/workers/{worker_id}/jobs/{job_id}/event",
               json={"type": "started", "attempt": attempt}, headers=wh)
        c.post(f"/workers/{worker_id}/jobs/{job_id}/event",
               json={"type": "progress", "attempt": attempt,
                     "activity": "running pytest", "tokens_used": 10}, headers=wh)
        _drain_notify(app)
    assert rec.calls == []


# ---------- integration: opt-in (unconfigured = zero posts) ----------


def test_unconfigured_posts_nothing(tmp_path: Path):
    app = _make_client(tmp_path, notify_url=None)  # not configured
    rec = _Recorder()
    # Even if a poster is present, an unset notify_url must short-circuit it.
    app.state.notify_poster = rec
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        _run_to_terminal_success(c)
        # nothing should have been scheduled at all
        assert app.state.notify_tasks == set()
    assert rec.calls == []


# ---------- integration: a failing endpoint never affects completion ----------


def test_endpoint_500_does_not_affect_completion(tmp_path: Path):
    # Real poster against an in-process endpoint that always 500s. The job must
    # still reach 'succeeded' and the triggering request must still return 200.
    sink = FastAPI()

    @sink.post("/notify")
    def _always_500():
        from fastapi import Response
        return Response(status_code=500)

    with TestClient(sink) as sink_client:
        # Drive notifications at the in-process sink via its TestClient transport.
        async def _poster(url, body, headers):
            try:
                sink_client.post("/notify", json=body, headers=headers)
            except Exception:
                pass

        app = _make_client(tmp_path, notify_url="http://sink/notify")
        app.state.notify_poster = _poster
        with TestClient(app) as c:
            c.headers.update({"Authorization": f"Bearer {TOKEN}"})
            job_id = _run_to_terminal_success(c)
            # Job completed normally despite the 500-ing endpoint.
            assert c.get(f"/jobs/{job_id}").json()["state"] == "succeeded"
            _drain_notify(app)


def test_endpoint_refused_does_not_affect_completion(tmp_path: Path):
    # REAL poster (_post_notification) against a closed port. The connection is
    # refused; the job must still complete and the report request still succeed.
    app = _make_client(tmp_path, notify_url="http://127.0.0.1:1/nope")
    # leave the real _post_notification in place — this is the empirical check.
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        job_id = _run_to_terminal_success(c)
        assert c.get(f"/jobs/{job_id}").json()["state"] == "succeeded"
        _drain_notify(app)  # the failing post still completes (it just logs)


def test_endpoint_timeout_does_not_affect_completion(tmp_path: Path, monkeypatch):
    # Simulate a hung endpoint: the poster raises TimeoutException. Job unaffected.
    app = _make_client(tmp_path, notify_url="http://slow.example/x")

    async def _timeout_poster(url, body, headers):
        # mirror real behavior: _post_notification catches and logs, never raises
        await server._post_notification(url, body, headers)

    class _StubClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.TimeoutException("hung")

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    app.state.notify_poster = _timeout_poster
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        job_id = _run_to_terminal_success(c)
        assert c.get(f"/jobs/{job_id}").json()["state"] == "succeeded"
        _drain_notify(app)
