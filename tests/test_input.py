"""Interactive follow-up to running jobs (R38).

Covers the whole verb end-to-end:

  * schema: the V13 → V14 migration adds the `job_inputs` table (existing data kept).
  * CP queue: POST /jobs/{id}/input on a RUNNING job queues a durable row; client
    (mobile/agent) tokens are allowed; a terminal job is rejected with 409.
  * worker delivery: the heartbeat reports owned jobs with pending input; the worker
    fetches them, writes a `command` job's input to the live process stdin and ACKs
    `delivered`, and marks a kind that can't take mid-run stdin (claude/docker) as
    `dropped` — never silently lost.
  * live smoke: drive the REAL run_job for a `command` job that reads a line from
    stdin and echoes it; deliver an input and assert it reached the process.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roost import server
from roost.schema import CURRENT_VERSION, SCHEMA_V1, migrate
from roost.worker import INPUT_DELIVERY_UNSUPPORTED, Worker, _supports_live_input


TOKEN = "test-shared-token"


@pytest.fixture()
def client(tmp_path: Path):
    db = tmp_path / "roost.db"
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


# ---------- helpers ----------


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


def _running_job(client: TestClient, worker_id: str, cred: str, spec: dict) -> str:
    """Submit a job, lease it to the worker, move it to running. Returns job_id."""
    wh = {"Authorization": f"Bearer {cred}"}
    r = client.post("/jobs", json=spec)
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    r = client.get(f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh)
    assert r.status_code == 200, r.text
    attempt = r.json()["attempt"]
    r = client.post(
        f"/workers/{worker_id}/jobs/{job_id}/event",
        json={"type": "started", "attempt": attempt}, headers=wh,
    )
    assert r.status_code == 200, r.text
    return job_id


# ---------- schema: V13 → V14 migration ----------


def test_migration_v13_to_v14_adds_job_inputs(tmp_path: Path):
    """A V13 DB (no job_inputs table) migrates to V14, gaining the table while
    keeping its existing rows — the additive migration pattern schema.py uses."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    # Build a V13-shaped DB: the full current schema, minus job_inputs, stamped 13.
    ddl = SCHEMA_V1
    assert "job_inputs" in ddl  # guard: the table IS part of the fresh schema
    # Drop the job_inputs CREATE/INDEXes so we simulate a pre-V14 file.
    lines = ddl.splitlines()
    keep, skip = [], False
    for ln in lines:
        if "CREATE TABLE IF NOT EXISTS job_inputs" in ln:
            skip = True
        if not skip:
            keep.append(ln)
        if skip and ln.strip().endswith(");"):
            skip = False
            # also skip the two job_inputs indexes that immediately follow
            continue
    pre_v14 = "\n".join(
        ln for ln in keep if "idx_job_inputs" not in ln
    )
    conn.executescript(pre_v14)
    conn.execute("PRAGMA user_version = 13")
    # Seed a job row so we can prove data survives the migration.
    conn.execute(
        "INSERT INTO jobs(id, spec, requires, state, created_at, root_job_id) "
        "VALUES ('j-old', '{}', '{}', 'running', 1.0, 'j-old')")
    conn.commit()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_inputs" not in tables  # precondition

    version = migrate(conn)

    assert version == CURRENT_VERSION == 14
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_inputs" in tables
    # Existing row preserved.
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    # The new table is usable.
    conn.execute(
        "INSERT INTO job_inputs(id, job_id, text, state, created_at) "
        "VALUES ('i1', 'j-old', 'hello', 'queued', 2.0)")
    conn.commit()
    assert conn.execute("SELECT text FROM job_inputs WHERE id='i1'").fetchone()[0] == "hello"
    conn.close()


def test_fresh_install_has_job_inputs(tmp_path: Path):
    """A brand-new DB jumps straight to V14 with the table present."""
    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    version = migrate(conn)
    assert version == 14
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_inputs" in tables
    conn.close()


# ---------- CP: queue + reject-on-terminal ----------


def test_input_queued_for_running_job(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})

    r = client.post(f"/jobs/{job_id}/input", json={"text": "hello agent"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "queued"
    assert body["job_id"] == job_id
    input_id = body["input_id"]

    # Visible in the inputs list with state queued.
    r = client.get(f"/jobs/{job_id}/inputs")
    assert r.status_code == 200, r.text
    inputs = r.json()["inputs"]
    assert len(inputs) == 1
    assert inputs[0]["id"] == input_id
    assert inputs[0]["state"] == "queued"

    # Counts surfaced on the job detail.
    r = client.get(f"/jobs/{job_id}")
    assert r.json()["inputs"] == {"queued": 1, "delivered": 0, "dropped": 0}

    # And it shows up as a log divider in the job stream.
    r = client.get(f"/jobs/{job_id}/logs")
    assert any("input_queued" in log["data"] for log in r.json()["logs"])


def test_input_rejected_for_terminal_job(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "echo hi", "requires": {"tools": ["python3"]}})
    # Finish it.
    r = client.post(
        f"/workers/{worker_id}/jobs/{job_id}/event",
        json={"type": "succeeded", "attempt": 1, "exit_code": 0}, headers=wh)
    assert r.status_code == 200

    r = client.post(f"/jobs/{job_id}/input", json={"text": "too late"})
    assert r.status_code == 409, r.text
    assert "terminal" in r.json()["detail"].lower()


def test_input_unknown_job_404(client: TestClient):
    r = client.post("/jobs/nope/input", json={"text": "hi"})
    assert r.status_code == 404


def test_input_empty_text_rejected(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    r = client.post(f"/jobs/{job_id}/input", json={"text": ""})
    assert r.status_code == 400


def test_input_oversize_rejected(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    big = "x" * (server.JOB_INPUT_MAX_BYTES + 1)
    r = client.post(f"/jobs/{job_id}/input", json={"text": big})
    assert r.status_code == 413


# ---------- CP: input counts surface on the aggregate views (R59) ----------


def test_derived_surfaces_input_counts(client: TestClient):
    """A run that has received input carries `inputs: {queued, delivered, dropped}`
    on GET /derived, mirroring the only-when-nonzero rule of GET /jobs/{id}."""
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    client.post(f"/jobs/{job_id}/input", json={"text": "steer left"})

    r = client.get("/derived")
    assert r.status_code == 200, r.text
    runs = {run["run_id"]: run for run in r.json()["runs"]}
    assert runs[job_id]["inputs"] == {"queued": 1, "delivered": 0, "dropped": 0}


def test_derived_omits_inputs_for_jobs_without_any(client: TestClient):
    """The 99% case: a job that never received input has NO `inputs` key on the
    derived run row — the payload stays lean."""
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    with_input = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    client.post(f"/jobs/{with_input}/input", json={"text": "x"})
    # A second job that never receives input (left queued — it still rides /derived).
    r = client.post("/jobs", json={"command": "true"})
    without = r.json()["id"]

    runs = {run["run_id"]: run for run in client.get("/derived").json()["runs"]}
    assert "inputs" in runs[with_input]
    assert "inputs" not in runs[without]  # only-when-nonzero


def test_job_derived_endpoint_carries_input_counts(client: TestClient):
    """The single-job /jobs/{id}/derived (the §2 run shape) also carries counts."""
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    client.post(f"/jobs/{job_id}/input", json={"text": "hi"})
    r = client.get(f"/jobs/{job_id}/derived")
    assert r.json()["inputs"] == {"queued": 1, "delivered": 0, "dropped": 0}


def _dispatch_child(client: TestClient, worker_id: str, cred: str,
                    parent_id: str, spec: dict) -> str:
    """Dispatch a sub-job under `parent_id` as the owning worker — the real
    hierarchy path, so the child inherits the parent's root_job_id and lands in
    the tree. The parent must have been submitted with hierarchy.can_dispatch."""
    wh = {"Authorization": f"Bearer {cred}"}
    body = dict(spec, parent_job_id=parent_id)
    r = client.post("/jobs", json=body, headers=wh)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_tree_endpoint_annotates_input_counts_per_node(client: TestClient):
    """A tree whose child has received input shows the counts on THAT node only —
    siblings without input omit the key (same rule as the single-job detail)."""
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    # Root job declares hierarchy.can_dispatch so the worker may dispatch children.
    root = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]},
         "hierarchy": {"can_dispatch": True}})
    child_a = _dispatch_child(
        client, worker_id, cred, root,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    child_b = _dispatch_child(
        client, worker_id, cred, root,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    # Input is accepted on any non-terminal job; child_a is queued, which is fine.
    client.post(f"/jobs/{child_a}/input", json={"text": "to the child"})

    nodes = {n["id"]: n for n in client.get(f"/jobs/{root}/tree").json()}
    assert nodes[child_a]["inputs"] == {"queued": 1, "delivered": 0, "dropped": 0}
    assert "inputs" not in nodes[child_b]  # sibling without input
    assert "inputs" not in nodes[root]     # root without input


# ---------- CP: client (mobile/agent) tokens may send input ----------


def _mint_client_token(client: TestClient, scope: str = "mobile") -> str:
    r = client.post("/pair-tokens", json={"label": "phone", "scope": scope})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_client_token_can_send_input(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    tok = _mint_client_token(client, "mobile")
    r = client.post(
        f"/jobs/{job_id}/input", json={"text": "from the phone"},
        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "queued"
    # The audit trail records the principal kind.
    r = client.get(f"/jobs/{job_id}/inputs")
    assert r.json()["inputs"][0]["created_by"] == "client"


# ---------- CP: worker-plane fetch + ack ----------


def test_heartbeat_reports_pending_inputs(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    client.post(f"/jobs/{job_id}/input", json={"text": "ping"})

    r = client.post(f"/workers/{worker_id}/heartbeat", json={}, headers=wh)
    assert r.status_code == 200, r.text
    assert job_id in r.json()["inputs"]


def test_worker_fetch_and_ack_flow(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    r = client.post(f"/jobs/{job_id}/input", json={"text": "deliver me"})
    input_id = r.json()["input_id"]

    # Worker fetches its pending inputs.
    r = client.get(f"/workers/{worker_id}/jobs/{job_id}/inputs", headers=wh)
    assert r.status_code == 200, r.text
    inputs = r.json()["inputs"]
    assert [i["id"] for i in inputs] == [input_id]
    assert inputs[0]["text"] == "deliver me"

    # Worker ACKs delivered → state flips; heartbeat no longer reports it.
    r = client.post(
        f"/workers/{worker_id}/jobs/{job_id}/input-ack",
        json={"input_id": input_id, "state": "delivered", "detail": "to stdin"},
        headers=wh)
    assert r.status_code == 200 and r.json()["acked"] is True

    r = client.get(f"/jobs/{job_id}/inputs")
    assert r.json()["inputs"][0]["state"] == "delivered"
    r = client.post(f"/workers/{worker_id}/heartbeat", json={}, headers=wh)
    assert job_id not in r.json()["inputs"]
    # A second ack is a harmless no-op (idempotent).
    r = client.post(
        f"/workers/{worker_id}/jobs/{job_id}/input-ack",
        json={"input_id": input_id, "state": "delivered"}, headers=wh)
    assert r.status_code == 200 and r.json()["acked"] is False


def test_ack_bad_state_rejected(client: TestClient):
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    wh = {"Authorization": f"Bearer {cred}"}
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    r = client.post(f"/jobs/{job_id}/input", json={"text": "x"})
    input_id = r.json()["input_id"]
    r = client.post(
        f"/workers/{worker_id}/jobs/{job_id}/input-ack",
        json={"input_id": input_id, "state": "bogus"}, headers=wh)
    assert r.status_code == 400


def test_other_worker_cannot_fetch_or_ack(client: TestClient):
    """A worker may only fetch/ack inputs for jobs assigned to IT."""
    worker_id, cred = _enroll_worker(client, {"tools": ["python3"]})
    job_id = _running_job(
        client, worker_id, cred,
        {"command": "cat", "requires": {"tools": ["python3"]}})
    r = client.post(f"/jobs/{job_id}/input", json={"text": "x"})
    input_id = r.json()["input_id"]
    # A second worker (different credential) sees no inputs for this job and
    # cannot ack it (require_matching_worker plus the ownership check).
    other_id, other_cred = _enroll_worker(client, {"tools": ["python3"]})
    oh = {"Authorization": f"Bearer {other_cred}"}
    r = client.get(f"/workers/{other_id}/jobs/{job_id}/inputs", headers=oh)
    assert r.status_code == 200 and r.json()["inputs"] == []
    r = client.post(
        f"/workers/{other_id}/jobs/{job_id}/input-ack",
        json={"input_id": input_id, "state": "delivered"}, headers=oh)
    assert r.status_code == 200 and r.json()["acked"] is False
    # The original input is still queued.
    r = client.get(f"/jobs/{job_id}/inputs")
    assert r.json()["inputs"][0]["state"] == "queued"


# ---------- worker: which kinds support live delivery ----------


def test_supports_live_input_matrix():
    assert _supports_live_input({"command": "cat"}) is True
    assert _supports_live_input({"command": ["cat"], "kind": "command"}) is True
    assert _supports_live_input({"intent": "do a thing", "kind": "claude"}) is False
    assert _supports_live_input({"intent": "do a thing"}) is False  # default claude
    assert _supports_live_input({"task": "x", "kind": "auto"}) is False
    assert _supports_live_input(
        {"kind": "docker", "image": "x", "command": "echo"}) is False


# ---------- worker: deliver to a stubbed process / drop for non-live ----------


class _FakeStdin:
    """Minimal asyncio.StreamWriter stand-in capturing writes."""

    def __init__(self, closing: bool = False):
        self.buf = bytearray()
        self._closing = closing
        self.drained = 0

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        self.drained += 1

    def is_closing(self) -> bool:
        return self._closing


class _FakeProc:
    def __init__(self, returncode=None, stdin=None):
        self.returncode = returncode
        self.stdin = stdin


class _FakeClient:
    """Records the worker's fetch GET + ack POSTs; serves a scripted inputs list."""

    def __init__(self, inputs: list[dict]):
        self._inputs = inputs
        self.acks: list[dict] = []
        self.logs: list[dict] = []

    async def get(self, url: str):
        return _Resp(200, {"inputs": self._inputs})

    async def post(self, url: str, json: dict):
        if url.endswith("/input-ack"):
            self.acks.append(json)
        elif url.endswith("/logs"):
            self.logs.append(json)
        return _Resp(200, {})

    async def aclose(self) -> None:
        pass


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _worker_with_client(inputs: list[dict]) -> tuple[Worker, _FakeClient]:
    w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
    fc = _FakeClient(inputs)
    w.client = fc  # type: ignore[assignment]
    return w, fc


def test_deliver_writes_to_live_stdin_and_acks_delivered():
    async def go():
        stdin = _FakeStdin()
        proc = _FakeProc(returncode=None, stdin=stdin)
        w, fc = _worker_with_client([{"id": "i1", "text": "hello"}])
        w._active["j1"] = {"process": proc, "is_docker": False,
                           "cancelled": None, "live_stdin": True}
        await w._deliver_inputs("j1")
        # Bytes reached the process, newline-terminated + drained.
        assert bytes(stdin.buf) == b"hello\n"
        assert stdin.drained == 1
        assert fc.acks == [
            {"input_id": "i1", "state": "delivered", "detail": "written to process stdin"}]
        await w.close()

    asyncio.run(go())


def test_deliver_preserves_existing_trailing_newline():
    async def go():
        stdin = _FakeStdin()
        proc = _FakeProc(returncode=None, stdin=stdin)
        w, fc = _worker_with_client([{"id": "i1", "text": "hello\n"}])
        w._active["j1"] = {"process": proc, "is_docker": False,
                           "cancelled": None, "live_stdin": True}
        await w._deliver_inputs("j1")
        assert bytes(stdin.buf) == b"hello\n"  # not doubled
        await w.close()

    asyncio.run(go())


def test_deliver_drops_for_non_live_kind():
    async def go():
        # An agent/docker job: no live stdin → dropped with the documented reason.
        proc = _FakeProc(returncode=None, stdin=None)
        w, fc = _worker_with_client([{"id": "i1", "text": "hello"}])
        w._active["j1"] = {"process": proc, "is_docker": True,
                           "cancelled": None, "live_stdin": False}
        await w._deliver_inputs("j1")
        assert len(fc.acks) == 1
        assert fc.acks[0]["state"] == "dropped"
        assert fc.acks[0]["detail"] == INPUT_DELIVERY_UNSUPPORTED
        await w.close()

    asyncio.run(go())


def test_deliver_drops_when_process_exited():
    async def go():
        stdin = _FakeStdin()
        proc = _FakeProc(returncode=0, stdin=stdin)  # already exited
        w, fc = _worker_with_client([{"id": "i1", "text": "hello"}])
        w._active["j1"] = {"process": proc, "is_docker": False,
                           "cancelled": None, "live_stdin": True}
        await w._deliver_inputs("j1")
        assert bytes(stdin.buf) == b""  # nothing written
        assert fc.acks[0]["state"] == "dropped"
        await w.close()

    asyncio.run(go())


def test_deliver_drops_when_stdin_closing():
    async def go():
        stdin = _FakeStdin(closing=True)
        proc = _FakeProc(returncode=None, stdin=stdin)
        w, fc = _worker_with_client([{"id": "i1", "text": "hello"}])
        w._active["j1"] = {"process": proc, "is_docker": False,
                           "cancelled": None, "live_stdin": True}
        await w._deliver_inputs("j1")
        assert fc.acks[0]["state"] == "dropped"
        await w.close()

    asyncio.run(go())


# ---------- LIVE smoke: real command process receives stdin ----------


def test_live_command_job_receives_stdin_end_to_end():
    """Drive the REAL run_job for a `command` job that reads ONE line from stdin and
    echoes it. Deliver an input via the same path the heartbeat uses and assert the
    text reached the process (it appears in the relayed stdout)."""
    async def go():
        w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
        events: list[dict] = []
        logs: list[tuple[str, str]] = []

        async def fake_post_event(job_id, event):
            events.append(event)

        async def fake_send_log(job_id, stream, data):
            logs.append((stream, data))

        # Stub only the ACK + fetch transport (no real CP); delivery itself is real.
        acks: list[dict] = []

        async def fake_ack(job_id, input_id, state, detail=None):
            acks.append({"input_id": input_id, "state": state, "detail": detail})

        w._post_event = fake_post_event  # type: ignore[assignment]
        w._send_log = fake_send_log  # type: ignore[assignment]
        w._ack_input = fake_ack  # type: ignore[assignment]

        # A shell that reads one line and echoes it back, then exits — so the job
        # terminates on its own once the input arrives (no wallclock wait needed).
        spec = {"kind": "command",
                "command": "read line; echo \"GOT:$line\""}
        task = asyncio.create_task(w.run_job({"id": "live1", "spec": spec}))
        # Wait until the process is live and has a stdin pipe.
        for _ in range(300):
            entry = w._active.get("live1")
            if entry and entry.get("live_stdin") and entry.get("process") and \
                    entry["process"].stdin is not None:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("command job never opened a live stdin")

        # Deliver directly via the real fetch/deliver method, with the fetch stubbed.
        async def fake_get(url):
            return _Resp(200, {"inputs": [{"id": "i1", "text": "hello-from-test"}]})
        w.client.get = fake_get  # type: ignore[assignment]
        await w._deliver_inputs("live1")

        # The job should now finish; wait for it.
        await asyncio.wait_for(task, timeout=15.0)
        await w.close()

        # The delivered text reached the process and came back on stdout.
        stdout = "\n".join(d for s, d in logs if s == "stdout")
        assert "GOT:hello-from-test" in stdout, stdout
        assert acks == [
            {"input_id": "i1", "state": "delivered", "detail": "written to process stdin"}]
        # Job succeeded (read+echo exits 0).
        terminal = [e for e in events if e.get("type") in ("succeeded", "failed")]
        assert terminal and terminal[-1]["type"] == "succeeded"

    asyncio.run(go())
