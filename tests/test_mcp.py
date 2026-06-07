"""Conversational MCP server: protocol + inbox-formatting helpers (pure parts;
the CP-touching tools are validated live)."""
from __future__ import annotations

from roost import mcp


def test_initialize():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION


def test_tools_list_has_conversational_tools():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"roost_do", "roost_runs", "roost_result", "roost_capabilities"} <= names
    # roost_do is the primary tool → listed first
    assert resp["result"]["tools"][0]["name"] == "roost_do"


def test_unknown_tool_errors():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "nope", "arguments": {}}})
    assert "error" in resp


def test_goal_of_handles_task_intent_command():
    assert mcp._goal_of({"spec": {"task": "do X"}}) == "do X"
    assert mcp._goal_of({"spec": {"intent": "think Y"}}) == "think Y"
    assert mcp._goal_of({"spec": {"command": ["echo", "hi"]}}) == "echo hi"


def test_phase_of():
    assert mcp._phase_of({"state": "succeeded"}) == "succeeded"
    assert mcp._phase_of({"state": "running", "last_activity": "🔎 verifying result"}) == "verifying"
    assert mcp._phase_of({"state": "running", "last_activity": "🔧 self-healing (attempt 1)"}) == "self-healing"
    assert mcp._phase_of({"state": "running", "last_activity": "→ Bash"}) == "running"


def test_run_summary_surfaces_verified_and_result():
    job = {"id": "abc", "state": "succeeded", "worker_id": "w1",
           "spec": {"task": "write a file"},
           "result": {"verified": True, "output": "wrote /tmp/x", "evidence": "confirmed"}}
    s = mcp._run_summary(job)
    assert s["run_id"] == "abc" and s["verified"] is True
    assert s["phase"] == "succeeded" and "wrote" in s["result"]


# ---------- roost_do mirrors the CLI trust loop ----------

from roost import cli as roost_cli


def _plan(**over):
    base = {"mode": "single", "ambiguous": False, "clarifying_question": None,
            "destructive": False, "simple": False, "restated": "do it",
            "classify_failed": False}
    base.update(over)
    return base


def test_roost_do_schema_has_confirm():
    schema = next(t for t in mcp.TOOLS if t["name"] == "roost_do")["inputSchema"]
    assert "confirm" in schema["properties"]


def test_roost_submit_kind_enum_includes_auto():
    """kind: auto must be in the roost_submit inputSchema enum (R27)."""
    schema = next(t for t in mcp.TOOLS if t["name"] == "roost_submit")["inputSchema"]
    kind_field = schema["properties"]["kind"]
    assert "auto" in kind_field["enum"], (
        "kind: auto missing from roost_submit schema — captain agents cannot use "
        "the self-selecting verified path via MCP"
    )
    # The other kinds must still be present
    for k in ("claude", "codex", "docker"):
        assert k in kind_field["enum"], f"kind: {k!r} was unexpectedly removed"


def test_roost_do_safe_single_runs(monkeypatch):
    posted = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"id": "job-1", "state": "queued"}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, path, json):
            posted["body"] = json
            return _Resp()

    monkeypatch.setattr(roost_cli, "_classify_goal", lambda _g: _plan())
    monkeypatch.setattr(mcp, "_client", lambda: _C())
    out = mcp.tool_roost_do({"goal": "report hostname"})
    assert out["run_id"] == "job-1"
    assert posted["body"]["kind"] == "auto" and posted["body"]["task"] == "report hostname"


def test_roost_do_multi_dispatches(monkeypatch):
    monkeypatch.setattr(roost_cli, "_classify_goal", lambda _g: _plan(mode="multi"))
    called = {}

    def _dispatch(url, token, goal, **k):
        called["goal"] = goal
        return ("root-9", 0)

    monkeypatch.setattr(roost_cli, "dispatch_goal", _dispatch)
    out = mcp.tool_roost_do({"goal": "a and b across the fleet"})
    assert out["mode"] == "multi" and out["run_id"] == "root-9"
    assert out["state"] == "succeeded"
    assert called["goal"] == "a and b across the fleet"


def test_roost_do_ambiguous_returns_question_without_running(monkeypatch):
    monkeypatch.setattr(roost_cli, "_classify_goal",
                        lambda _g: _plan(ambiguous=True, clarifying_question="which env?"))
    # _client must NOT be called — make it explode if it is.
    monkeypatch.setattr(mcp, "_client", lambda: (_ for _ in ()).throw(AssertionError("ran")))
    out = mcp.tool_roost_do({"goal": "deploy"})
    assert out["needs"] == "clarification"
    assert out["clarifying_question"] == "which env?"


def test_roost_do_destructive_needs_confirm(monkeypatch):
    monkeypatch.setattr(roost_cli, "_classify_goal",
                        lambda _g: _plan(destructive=True, restated="rm -rf /data"))
    monkeypatch.setattr(mcp, "_client", lambda: (_ for _ in ()).throw(AssertionError("ran")))
    out = mcp.tool_roost_do({"goal": "wipe the data"})
    assert out["needs"] == "confirmation"
    assert out["will_do"] == "rm -rf /data"


def test_roost_do_classify_failed_needs_confirm(monkeypatch):
    monkeypatch.setattr(roost_cli, "_classify_goal",
                        lambda _g: roost_cli._classify_failed("risky"))
    monkeypatch.setattr(mcp, "_client", lambda: (_ for _ in ()).throw(AssertionError("ran")))
    out = mcp.tool_roost_do({"goal": "risky"})
    assert out["needs"] == "confirmation"
    assert "could not classify" in out["reason"]


def test_roost_do_destructive_with_confirm_runs(monkeypatch):
    posted = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"id": "job-2", "state": "queued"}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, path, json):
            posted["body"] = json
            return _Resp()

    monkeypatch.setattr(roost_cli, "_classify_goal", lambda _g: _plan(destructive=True))
    monkeypatch.setattr(mcp, "_client", lambda: _C())
    out = mcp.tool_roost_do({"goal": "wipe it", "confirm": True})
    assert out["run_id"] == "job-2"
    assert posted["body"]["kind"] == "auto"


def test_roost_do_destructive_multi_without_confirm_does_not_dispatch(monkeypatch):
    # A destructive MULTI goal must demand confirmation BEFORE dispatching to the
    # captain — the needs-confirm gate runs before multi→single routing.
    monkeypatch.setattr(roost_cli, "_classify_goal",
                        lambda _g: _plan(mode="multi", destructive=True,
                                         restated="rm -rf /data across the fleet"))
    monkeypatch.setattr(roost_cli, "dispatch_goal",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched")))
    out = mcp.tool_roost_do({"goal": "wipe data on every box"})
    assert out["needs"] == "confirmation"
    assert out["will_do"] == "rm -rf /data across the fleet"


def test_roost_do_destructive_multi_with_confirm_dispatches(monkeypatch):
    monkeypatch.setattr(roost_cli, "_classify_goal",
                        lambda _g: _plan(mode="multi", destructive=True))
    called = {}

    def _dispatch(url, token, goal, **k):
        called["goal"] = goal
        return ("root-7", 0)

    monkeypatch.setattr(roost_cli, "dispatch_goal", _dispatch)
    out = mcp.tool_roost_do({"goal": "wipe data on every box", "confirm": True})
    assert out["mode"] == "multi" and out["run_id"] == "root-7"
    assert called["goal"] == "wipe data on every box"


# ---------- roost_exec: run a command on one pinned worker ----------


def test_roost_exec_tool_listed():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "roost_exec" in names
    assert "roost_exec" in mcp.TOOL_IMPL


class _WResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self): pass
    def json(self): return self._payload


def _exec_mcp_client(workers, posted):
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, path, **k):
            return _WResp(workers)
        def post(self, path, json):
            posted["body"] = json
            return _WResp({"id": "job-x", "state": "queued"})
    return _C()


def test_roost_exec_sets_target_and_submits_command(monkeypatch):
    posted = {}
    workers = [{"id": "aaa111", "name": "gpu-box", "status": "idle"}]
    monkeypatch.setattr(mcp, "_client", lambda: _exec_mcp_client(workers, posted))
    # don't block on a live job — stub the wait/logs helpers
    monkeypatch.setattr(mcp, "tool_roost_wait",
                        lambda a: {"id": "job-x", "state": "succeeded", "exit_code": 0})
    monkeypatch.setattr(mcp, "tool_roost_logs",
                        lambda a: {"logs": [{"data": "hello"}]})
    out = mcp.tool_roost_exec({"worker": "gpu-box", "command": "echo hello"})
    assert posted["body"]["kind"] == "command"
    assert posted["body"]["command"] == "echo hello"
    assert posted["body"]["target"] == "gpu-box"  # PINNED CONTRACT
    assert out["state"] == "succeeded" and out["exit_code"] == 0
    assert out["output"] == "hello"


def test_roost_exec_argv_command_is_joined(monkeypatch):
    posted = {}
    workers = [{"id": "aaa111", "name": "gpu-box", "status": "idle"}]
    monkeypatch.setattr(mcp, "_client", lambda: _exec_mcp_client(workers, posted))
    out = mcp.tool_roost_exec({"worker": "gpu-box", "command": ["df", "-h"],
                               "wait": False})
    assert posted["body"]["command"] == "df -h"
    assert posted["body"]["target"] == "gpu-box"
    assert out["job_id"] == "job-x"


def test_roost_exec_unknown_worker_returns_error(monkeypatch):
    posted = {}
    workers = [{"id": "aaa111", "name": "gpu-box", "status": "idle"}]
    monkeypatch.setattr(mcp, "_client", lambda: _exec_mcp_client(workers, posted))
    out = mcp.tool_roost_exec({"worker": "ghost", "command": "ls"})
    assert out["error"] == "bad_target"
    assert "no worker named/ided 'ghost'" in out["detail"]
    assert posted == {}  # never submitted


# ---------- transfer tools (blob store: stage / send / fetch / list) ----------

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roost import server

TRANSFER_TOKEN = "test-shared-token"


@pytest.fixture()
def cp(tmp_path: Path):
    """A live TestClient-backed control plane; mcp._client() points at it."""
    db = tmp_path / "roost.db"
    app = server.create_app(db_path=db, token=TRANSFER_TOKEN, run_sweeper=False)
    with TestClient(app) as tc:
        tc.headers.update({"Authorization": f"Bearer {TRANSFER_TOKEN}"})
        yield tc


def _bind_client(monkeypatch, tc: TestClient) -> None:
    """Make mcp._client() yield the already-entered TestClient (no re-enter).

    The mcp tools use it as `with _client() as c: ...`; wrap so __enter__/__exit__
    are no-ops over the shared client.
    """
    class _Wrap:
        def __enter__(self): return tc
        def __exit__(self, *a): return False

    monkeypatch.setattr(mcp, "_client", lambda: _Wrap())


def _enroll(tc: TestClient, name: str, capabilities: dict) -> str:
    token = tc.post("/enroll-tokens", json={"label": "t"}).json()["token"]
    r = tc.post("/enroll", json={"token": token, "name": name,
                                 "capabilities": capabilities},
                headers={"Authorization": ""})
    assert r.status_code == 200, r.text
    return r.json()["worker_id"]


def _drive_to_success(tc: TestClient, worker_id: str, cred: str,
                      on_run=None) -> str:
    """Poll one job for the worker, mark started, optionally act, mark succeeded.

    Returns the job id. `on_run(job)` runs after 'started' (e.g. the worker-side
    leg of a fetch: PUT bytes to the presigned slot)."""
    wh = {"Authorization": f"Bearer {cred}"}
    assigned = tc.get(f"/workers/{worker_id}/poll", params={"timeout": 0},
                      headers=wh).json()
    job_id = assigned["id"]
    attempt = assigned["attempt"]
    tc.post(f"/workers/{worker_id}/jobs/{job_id}/event",
            json={"type": "started", "attempt": attempt}, headers=wh)
    if on_run is not None:
        on_run(assigned)
    tc.post(f"/workers/{worker_id}/jobs/{job_id}/event",
            json={"type": "succeeded", "attempt": attempt, "exit_code": 0},
            headers=wh)
    return job_id


def test_transfer_tools_listed():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 30, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"stage_file", "send_file", "fetch_file", "list_staged"} <= names
    for n in ("stage_file", "send_file", "fetch_file", "list_staged"):
        assert n in mcp.TOOL_IMPL


def test_stage_file_roundtrip(cp, tmp_path, monkeypatch):
    _bind_client(monkeypatch, cp)
    f = tmp_path / "report.txt"
    payload = b"hello fleet" * 50
    f.write_bytes(payload)

    out = mcp.tool_stage_file({"path": str(f)})
    assert out["name"] == "report.txt"
    assert out["size"] == len(payload)
    assert out["sha256"]
    assert "get_url" in out and "/blobs/" in out["get_url"]

    # it shows up in list_staged
    listed = mcp.tool_list_staged({})
    assert any(b["id"] == out["id"] for b in listed["staged"])

    # the presigned get_url downloads the bytes (no bearer needed)
    from urllib.parse import urlparse, parse_qs
    p = urlparse(out["get_url"])
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    r = cp.get(p.path, params=q, headers={"Authorization": ""})
    assert r.status_code == 200 and r.content == payload


def test_send_file_end_to_end(cp, tmp_path, monkeypatch):
    _bind_client(monkeypatch, cp)
    cred = cp.post("/enroll-tokens", json={"label": "t"}).json()
    # enroll a worker with a known hostname so the delivery job can pin to it
    token = cred["token"]
    r = cp.post("/enroll", json={"token": token, "name": "boxA",
                                 "capabilities": {"hostname": "boxA-host",
                                                  "os": "linux"}},
                headers={"Authorization": ""})
    worker_id = r.json()["worker_id"]
    worker_cred = r.json()["credential"]

    f = tmp_path / "model.bin"
    f.write_bytes(b"weights")

    out = mcp.tool_send_file({"worker": "boxA", "local_path": str(f),
                              "destination_path": "/opt/data/model.bin"})
    assert out["destination"] == "/opt/data/model.bin"
    assert "boxA" in out["worker"]
    assert out["blob"]["sha256"]
    job_id = out["job_id"]

    # the submitted job is a command job hard-pinned to the worker's hostname,
    # and its command mkdir -ps, curls the presigned blob, and sha256-verifies.
    job = cp.get(f"/jobs/{job_id}").json()
    spec = job["spec"]
    assert spec["requires"] == {"hostname": "==boxA-host"}
    assert "mkdir -p" in spec["command"]
    assert "curl" in spec["command"] and "/blobs/" in spec["command"]
    assert "sha256sum" in spec["command"]  # linux → sha256sum
    assert out["blob"]["sha256"] in spec["command"]

    # the pinned worker can actually lease it (requires matched)
    leased = _drive_to_success(cp, worker_id, worker_cred)
    assert leased == job_id


def test_send_file_darwin_uses_shasum(cp, tmp_path, monkeypatch):
    _bind_client(monkeypatch, cp)
    token = cp.post("/enroll-tokens", json={"label": "t"}).json()["token"]
    cp.post("/enroll", json={"token": token, "name": "mac",
                             "capabilities": {"hostname": "mac-host", "os": "darwin"}},
            headers={"Authorization": ""})
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    out = mcp.tool_send_file({"worker": "mac", "local_path": str(f),
                              "destination_path": "/tmp/x.bin"})
    job = cp.get(f"/jobs/{out['job_id']}").json()
    assert "shasum -a 256" in job["spec"]["command"]
    assert "sha256sum" not in job["spec"]["command"]


def test_send_file_unknown_worker_errors(cp, tmp_path, monkeypatch):
    _bind_client(monkeypatch, cp)
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    out = mcp.tool_send_file({"worker": "ghost", "local_path": str(f),
                              "destination_path": "/tmp/x.bin"})
    assert out["error"] == "bad_target"
    assert "no worker named/ided 'ghost'" in out["detail"]
    # nothing was staged (no job, no blob beyond what other tests made)
    assert mcp.tool_list_staged({})["staged"] == []


def test_fetch_file_end_to_end(cp, tmp_path, monkeypatch):
    _bind_client(monkeypatch, cp)
    token = cp.post("/enroll-tokens", json={"label": "t"}).json()["token"]
    r = cp.post("/enroll", json={"token": token, "name": "boxB",
                                 "capabilities": {"hostname": "boxB-host",
                                                  "os": "linux"}},
                headers={"Authorization": ""})
    worker_id = r.json()["worker_id"]
    worker_cred = r.json()["credential"]

    remote_bytes = b"remote result data" * 20

    # Drive the real worker single-threaded by intercepting the blocking wait:
    # when fetch_file would poll the job, lease it, perform the worker-side leg
    # (PUT the remote bytes to the presigned slot via the curl -T url baked into
    # the command), and report success — then let fetch_file download the blob.
    real_wait = mcp.tool_roost_wait

    def _wait_that_drives(args):
        from urllib.parse import urlparse, parse_qs
        wh = {"Authorization": f"Bearer {worker_cred}"}
        assigned = cp.get(f"/workers/{worker_id}/poll", params={"timeout": 0},
                          headers=wh).json()
        assert assigned["id"] == args["job_id"]
        attempt = assigned["attempt"]
        cp.post(f"/workers/{worker_id}/jobs/{assigned['id']}/event",
                json={"type": "started", "attempt": attempt}, headers=wh)
        url = assigned["spec"]["command"].split("'")[1]  # the curl -T put_url
        p = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(p.query).items()}
        rr = cp.put(p.path, params=q, content=remote_bytes,
                    headers={"Authorization": ""})
        assert rr.status_code == 200, rr.text
        cp.post(f"/workers/{worker_id}/jobs/{assigned['id']}/event",
                json={"type": "succeeded", "attempt": attempt, "exit_code": 0},
                headers=wh)
        return real_wait(args)

    monkeypatch.setattr(mcp, "tool_roost_wait", _wait_that_drives)

    out = mcp.tool_fetch_file(
        {"worker": "boxB", "remote_path": "/var/log/result.log",
         "local_path": str(tmp_path / "got.log"), "timeout_min": 1})
    assert "error" not in out, out
    assert out["size"] == len(remote_bytes)
    assert Path(out["local_path"]).read_bytes() == remote_bytes
    assert "boxB" in out["worker"]
    assert out["local_path"] == str(tmp_path / "got.log")
