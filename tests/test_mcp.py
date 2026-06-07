"""Conversational MCP server: protocol + inbox-formatting helpers (pure parts;
the CP-touching tools are validated live)."""
from __future__ import annotations

import json

import httpx

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


def test_roost_submit_schema_exposes_reason():
    """R33: the captain needs a `reason` field on roost_submit to record per-sub-job
    plan intent that `roost tree` later renders."""
    schema = next(t for t in mcp.TOOLS if t["name"] == "roost_submit")["inputSchema"]
    assert "reason" in schema["properties"]
    assert schema["properties"]["reason"]["type"] == "string"


def test_roost_submit_forwards_reason_to_cp(monkeypatch):
    """The reason a captain passes must reach the CP body verbatim (it persists in
    the child spec there)."""
    posted = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"id": "j1", "state": "queued"}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, path, json):
            posted["body"] = json
            return _Resp()

    monkeypatch.setattr(mcp, "_client", lambda: _C())
    monkeypatch.delenv("ROOST_PARENT_JOB_ID", raising=False)
    out = mcp.tool_roost_submit({"command": "ruff .", "reason": "cheap CPU gate, run first"})
    assert out["id"] == "j1"
    assert posted["body"]["reason"] == "cheap CPU gate, run first"


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


# ---------- _client(): env-driven base_url + bearer ----------


def test_client_uses_env_url_and_token(monkeypatch):
    monkeypatch.setenv("ROOST_URL", "http://cp.example:9000/")
    monkeypatch.setenv("ROOST_TOKEN", "sekret")
    c = mcp._client()
    try:
        # trailing slash stripped; token becomes a bearer header
        assert str(c.base_url) == "http://cp.example:9000"
        assert c.headers["authorization"] == "Bearer sekret"
    finally:
        c.close()


def test_client_without_token_sends_no_auth_header(monkeypatch):
    monkeypatch.setenv("ROOST_URL", "http://127.0.0.1:8787")
    monkeypatch.delenv("ROOST_TOKEN", raising=False)
    c = mcp._client()
    try:
        assert "authorization" not in c.headers
    finally:
        c.close()


def test_parent_id_empty_is_none(monkeypatch):
    monkeypatch.delenv("ROOST_PARENT_JOB_ID", raising=False)
    assert mcp._parent_id() is None
    monkeypatch.setenv("ROOST_PARENT_JOB_ID", "")
    assert mcp._parent_id() is None
    monkeypatch.setenv("ROOST_PARENT_JOB_ID", "p9")
    assert mcp._parent_id() == "p9"


# ---------- a reusable fake httpx.Client (records calls, scripts responses) ----------


class _FakeResp:
    def __init__(self, payload=None, *, status_code=200, text="", headers=None,
                 content=b""):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", "http://t/"),
                response=httpx.Response(self.status_code, text=self.text),
            )


class _FakeClient:
    """Records (verb, path, kwargs) and replays scripted responses by (verb, path).

    `routes` maps (verb, path) or (verb, None=any-path) -> _FakeResp | callable.
    """
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def _dispatch(self, verb, path, **kw):
        self.calls.append((verb, path, kw))
        r = self.routes.get((verb, path))
        if r is None:
            r = self.routes.get((verb, None))
        if r is None:
            raise AssertionError(f"unexpected {verb} {path}")
        return r(self, path, **kw) if callable(r) else r

    def get(self, path, **kw): return self._dispatch("GET", path, **kw)
    def post(self, path, **kw): return self._dispatch("POST", path, **kw)
    def delete(self, path, **kw): return self._dispatch("DELETE", path, **kw)
    def patch(self, path, **kw): return self._dispatch("PATCH", path, **kw)


def _bind(monkeypatch, routes):
    fc = _FakeClient(routes)
    monkeypatch.setattr(mcp, "_client", lambda: fc)
    monkeypatch.delenv("ROOST_PARENT_JOB_ID", raising=False)
    return fc


# ---------- roost_runs / roost_result ----------


def test_roost_runs_maps_jobs_to_summaries(monkeypatch):
    jobs = [
        {"id": "a", "state": "running", "worker_id": "w1",
         "spec": {"task": "build"}, "last_activity": "🔎 verifying"},
        {"id": "b", "state": "succeeded", "worker_id": "w2",
         "spec": {"intent": "ship"}, "result": {"verified": True, "output": "done"}},
    ]
    fc = _bind(monkeypatch, {("GET", "/jobs"): _FakeResp(jobs)})
    out = mcp.tool_roost_runs({"limit": 5})
    assert [r["run_id"] for r in out["runs"]] == ["a", "b"]
    assert out["runs"][0]["phase"] == "verifying"
    assert out["runs"][1]["verified"] is True
    # limit forwarded as a query param
    assert fc.calls[0][2]["params"] == {"limit": 5}


def test_roost_runs_default_limit(monkeypatch):
    fc = _bind(monkeypatch, {("GET", "/jobs"): _FakeResp([])})
    mcp.tool_roost_runs({})
    assert fc.calls[0][2]["params"] == {"limit": 15}


def test_roost_result_unpacks_verified_outcome(monkeypatch):
    monkeypatch.setattr(mcp, "tool_roost_wait", lambda a: {
        "id": "r1", "state": "succeeded", "tokens_used": 42,
        "result": {"verified": True, "evidence": "checked", "output": "hi"}})
    out = mcp.tool_roost_result({"run_id": "r1"})
    assert out["state"] == "succeeded" and out["verified"] is True
    assert out["evidence"] == "checked" and out["output"] == "hi"
    assert out["tokens_used"] == 42


def test_roost_result_passes_through_string_result(monkeypatch):
    # When result isn't a dict, output falls back to the raw result value.
    monkeypatch.setattr(mcp, "tool_roost_wait", lambda a: {
        "id": "r2", "state": "failed", "result": "boom", "error": "nope"})
    out = mcp.tool_roost_result({"run_id": "r2", "timeout_sec": 5})
    assert out["verified"] is None
    assert out["output"] == "boom"
    assert out["error"] == "nope"


# ---------- roost_capabilities ----------


def test_roost_capabilities_counts_live_and_gpus(monkeypatch):
    workers = [
        {"name": "cpu1", "status": "idle", "capabilities": {"cpus": 8}},
        {"name": "gpu1", "status": "busy",
         "capabilities": {"cpus": 16, "gpu_count": 2, "gpu": ["A100"],
                          "gpu_vram_gb": 80}},
        {"name": "off1", "status": "offline", "capabilities": {"cpus": 64}},
    ]
    _bind(monkeypatch, {("GET", "/workers"): _FakeResp(workers)})
    out = mcp.tool_roost_capabilities({})
    assert out["nodes"] == 2  # offline excluded
    assert out["cpu_cores"] == 24  # 8 + 16, not the offline 64
    assert out["gpu_nodes"] == [
        {"node": "gpu1", "count": 2, "gpu": "A100", "vram_gb": 80}]


def test_roost_capabilities_gpu_node_without_gpu_list_defaults(monkeypatch):
    workers = [{"name": "g", "status": "idle",
                "capabilities": {"gpu_count": 1}}]  # no 'gpu' list, no vram
    _bind(monkeypatch, {("GET", "/workers"): _FakeResp(workers)})
    out = mcp.tool_roost_capabilities({})
    assert out["gpu_nodes"][0]["gpu"] == "GPU"
    assert out["gpu_nodes"][0]["vram_gb"] is None


# ---------- roost_submit: parent attach + guardrail mapping ----------


def test_roost_submit_attaches_parent(monkeypatch):
    posted = {}
    def _post(self, path, **kw):
        posted["body"] = kw["json"]
        return _FakeResp({"id": "j", "state": "queued"})
    monkeypatch.setenv("ROOST_PARENT_JOB_ID", "parent-1")
    fc = _FakeClient({("POST", "/jobs"): _post})
    monkeypatch.setattr(mcp, "_client", lambda: fc)
    mcp.tool_roost_submit({"command": "echo hi"})
    assert posted["body"]["parent_job_id"] == "parent-1"


def test_roost_submit_409_json_detail_becomes_guardrail(monkeypatch):
    r = _FakeResp({"detail": "depth limit"}, status_code=409,
                  headers={"content-type": "application/json"})
    _bind(monkeypatch, {("POST", "/jobs"): r})
    out = mcp.tool_roost_submit({"intent": "x"})
    assert out == {"error": "guardrail", "detail": "depth limit"}


def test_roost_submit_409_non_json_uses_text(monkeypatch):
    r = _FakeResp(status_code=409, text="tree budget exhausted",
                  headers={"content-type": "text/plain"})
    _bind(monkeypatch, {("POST", "/jobs"): r})
    out = mcp.tool_roost_submit({"intent": "x"})
    assert out["error"] == "guardrail"
    assert out["detail"] == "tree budget exhausted"


# ---------- roost_status / roost_logs / roost_cancel / roost_workers ----------


def test_roost_status_returns_job(monkeypatch):
    fc = _bind(monkeypatch, {("GET", "/jobs/j7"): _FakeResp(
        {"id": "j7", "state": "running", "idle_sec": 3})})
    out = mcp.tool_roost_status({"job_id": "j7"})
    assert out["state"] == "running" and out["idle_sec"] == 3
    assert fc.calls[0][1] == "/jobs/j7"


def test_roost_logs_forwards_since_and_limit(monkeypatch):
    fc = _bind(monkeypatch, {("GET", "/jobs/j/logs"): _FakeResp(
        {"logs": [{"seq": 41, "stream": "stdout", "data": "x"}]})})
    out = mcp.tool_roost_logs({"job_id": "j", "since": 40, "limit": 10})
    assert out["logs"][0]["seq"] == 41
    assert fc.calls[0][2]["params"] == {"since": 40, "limit": 10}


def test_roost_logs_default_params(monkeypatch):
    fc = _bind(monkeypatch, {("GET", "/jobs/j/logs"): _FakeResp({"logs": []})})
    mcp.tool_roost_logs({"job_id": "j"})
    assert fc.calls[0][2]["params"] == {"since": 0, "limit": 500}


def test_roost_cancel_success(monkeypatch):
    fc = _bind(monkeypatch, {("DELETE", "/jobs/j"): _FakeResp({"cancelled": 3})})
    out = mcp.tool_roost_cancel({"job_id": "j", "tree": True})
    assert out["cancelled"] == 3
    assert fc.calls[0][2]["params"] == {"tree": True}


def test_roost_cancel_terminal_maps_to_not_cancellable(monkeypatch):
    r = _FakeResp(status_code=409, text="already terminal")
    _bind(monkeypatch, {("DELETE", "/jobs/j"): r})
    out = mcp.tool_roost_cancel({"job_id": "j"})
    assert out == {"error": "not_cancellable", "detail": "already terminal"}


def test_roost_workers_wraps_list(monkeypatch):
    _bind(monkeypatch, {("GET", "/workers"): _FakeResp(
        [{"id": "w1", "name": "box"}])})
    out = mcp.tool_roost_workers({})
    assert out == {"workers": [{"id": "w1", "name": "box"}]}


# ---------- roost_wait: terminal short-circuit + timeout flag ----------


def test_roost_wait_returns_on_terminal_state(monkeypatch):
    _bind(monkeypatch, {("GET", "/jobs/j"): _FakeResp(
        {"id": "j", "state": "succeeded", "exit_code": 0})})
    out = mcp.tool_roost_wait({"job_id": "j", "timeout_sec": 5})
    assert out["state"] == "succeeded"
    assert "timed_out_waiting" not in out


def test_roost_wait_polls_then_settles(monkeypatch):
    states = iter([
        {"id": "j", "state": "queued"},
        {"id": "j", "state": "running"},
        {"id": "j", "state": "succeeded", "exit_code": 0},
    ])
    _bind(monkeypatch, {("GET", "/jobs/j"): lambda *a, **k: _FakeResp(next(states))})
    monkeypatch.setattr(mcp.time, "sleep", lambda _s: None)  # don't actually wait
    out = mcp.tool_roost_wait({"job_id": "j", "poll_interval_sec": 0.01})
    assert out["state"] == "succeeded"


def test_roost_wait_times_out(monkeypatch):
    _bind(monkeypatch, {("GET", "/jobs/j"): _FakeResp(
        {"id": "j", "state": "running"})})
    monkeypatch.setattr(mcp.time, "sleep", lambda _s: None)
    # time advances past the deadline after the first poll
    ticks = iter([1000.0, 1000.0, 9999.0, 9999.0])
    monkeypatch.setattr(mcp.time, "time", lambda: next(ticks))
    out = mcp.tool_roost_wait({"job_id": "j", "timeout_sec": 5})
    assert out["timed_out_waiting"] is True
    assert out["state"] == "running"


# ---------- roost_exec: detach branch + parent attach ----------


def test_roost_exec_detach_returns_job_id_without_waiting(monkeypatch):
    workers = [{"id": "aaa", "name": "box", "status": "idle"}]
    posted = {}
    def _post(self, path, **kw):
        posted["body"] = kw["json"]
        return _FakeResp({"id": "job-d", "state": "queued"})
    fc = _FakeClient({("GET", "/workers"): _FakeResp(workers),
                      ("POST", "/jobs"): _post})
    monkeypatch.setattr(mcp, "_client", lambda: fc)
    monkeypatch.setenv("ROOST_PARENT_JOB_ID", "p-exec")
    # If wait were called it would explode (no route); detach must avoid it.
    out = mcp.tool_roost_exec({"worker": "box", "command": "sleep 1", "wait": False})
    assert out["job_id"] == "job-d"
    assert "submitted" in out["note"]
    assert posted["body"]["parent_job_id"] == "p-exec"
    assert posted["body"]["target"] == "box"


def test_roost_exec_ambiguous_name_errors(monkeypatch):
    # Two ONLINE workers share a name -> _resolve_target raises -> clean tool error.
    workers = [{"id": "id1", "name": "dup", "status": "idle"},
               {"id": "id2", "name": "dup", "status": "busy"}]
    _bind(monkeypatch, {("GET", "/workers"): _FakeResp(workers)})
    out = mcp.tool_roost_exec({"worker": "dup", "command": "ls"})
    assert out["error"] == "bad_target"
    assert "matches 2 workers" in out["detail"]


# ---------- transfer tools: no_hostname + fetch_failed paths ----------


def test_send_file_worker_without_hostname_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mcp, "_resolve_worker",
        lambda w: {"id": "w1", "name": "box", "capabilities": {"os": "linux"}})
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")
    out = mcp.tool_send_file({"worker": "box", "local_path": str(f),
                              "destination_path": "/tmp/x.bin"})
    assert out["error"] == "no_hostname"
    assert "advertises no hostname" in out["detail"]


def test_fetch_file_worker_without_hostname_errors(monkeypatch):
    monkeypatch.setattr(
        mcp, "_resolve_worker",
        lambda w: {"id": "w1", "name": "box", "capabilities": {}})
    out = mcp.tool_fetch_file({"worker": "box", "remote_path": "/var/log/x"})
    assert out["error"] == "no_hostname"


def test_fetch_file_bad_target_errors(monkeypatch):
    def _boom(_w):
        raise roost_cli.click.ClickException("no worker named/ided 'ghost'")
    monkeypatch.setattr(mcp, "_resolve_worker", _boom)
    out = mcp.tool_fetch_file({"worker": "ghost", "remote_path": "/x"})
    assert out["error"] == "bad_target"
    assert "ghost" in out["detail"]


def test_fetch_file_remote_upload_failure_reports(monkeypatch):
    """When the worker-side upload job doesn't succeed, fetch_file returns a
    fetch_failed error with the job's logs — and never downloads a blob."""
    monkeypatch.setattr(
        mcp, "_resolve_worker",
        lambda w: {"id": "w1", "name": "box",
                   "capabilities": {"hostname": "box-h"}})
    routes = {
        ("POST", "/blobs/presign"): _FakeResp(
            {"id": "blob-1", "put_url": "http://cp/blobs/blob-1/put?sig=x"}),
        ("POST", "/jobs"): _FakeResp({"id": "up-job", "state": "queued"}),
        # If reached, a blob GET would mean we wrongly tried to download.
        ("GET", "/blobs/blob-1"): lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not download after a failed upload")),
    }
    _bind(monkeypatch, routes)
    monkeypatch.setattr(mcp, "tool_roost_wait",
                        lambda a: {"id": "up-job", "state": "failed",
                                   "error": "curl: connection refused"})
    monkeypatch.setattr(mcp, "tool_roost_logs",
                        lambda a: {"logs": [{"data": "curl: (7) refused"}]})
    out = mcp.tool_fetch_file({"worker": "box", "remote_path": "/var/log/x.log"})
    assert out["error"] == "fetch_failed"
    assert out["state"] == "failed"
    assert "refused" in out["output"]


def test_stage_file_bad_path_propagates(monkeypatch, tmp_path):
    # _stage_blob opens the file; a missing path raises (caught at the RPC layer,
    # not inside the tool) — assert the tool itself doesn't swallow it.
    _bind(monkeypatch, {})
    with pytest.raises(FileNotFoundError):
        mcp.tool_stage_file({"path": str(tmp_path / "nope.bin")})


def test_list_staged_shape(monkeypatch):
    blobs = [{"id": "b1", "name": "f", "size": 3, "sha256": "ab",
              "state": "ready", "get_url": "http://cp/blobs/b1?sig=x",
              "expires_at": 9.0, "extra": "dropped"}]
    _bind(monkeypatch, {("GET", "/blobs"): _FakeResp(blobs)})
    out = mcp.tool_list_staged({})
    assert out["staged"] == [{
        "id": "b1", "name": "f", "size": 3, "sha256": "ab", "state": "ready",
        "get_url": "http://cp/blobs/b1?sig=x", "expires_at": 9.0}]


# ---------- roost_schedule: every subaction + error mapping (live CP) ----------


def test_roost_schedule_create_list_disable_enable_remove(cp, monkeypatch):
    _bind_client(monkeypatch, cp)
    # create from a plain goal -> kind:auto spec
    created = mcp.tool_roost_schedule(
        {"action": "create", "goal": "prune stale workers", "every": "30m",
         "name": "prune"})
    assert created["name"] == "prune"
    assert created["enabled"] is True
    assert created["spec"]["kind"] == "auto"
    assert created["spec"]["task"] == "prune stale workers"
    sid = created["id"]

    # list -> wrapped under "schedules"
    listed = mcp.tool_roost_schedule({"action": "list"})
    assert any(s["id"] == sid for s in listed["schedules"])

    # disable -> enabled flips to False
    disabled = mcp.tool_roost_schedule({"action": "disable", "schedule_id": sid})
    assert disabled["enabled"] is False

    # enable -> back to True
    enabled = mcp.tool_roost_schedule({"action": "enable", "schedule_id": sid})
    assert enabled["enabled"] is True

    # remove -> deleted
    removed = mcp.tool_roost_schedule({"action": "remove", "schedule_id": sid})
    assert removed.get("deleted") is True


def test_roost_schedule_default_action_is_list(cp, monkeypatch):
    _bind_client(monkeypatch, cp)
    out = mcp.tool_roost_schedule({})  # no action -> list
    assert "schedules" in out


def test_roost_schedule_create_with_explicit_spec(cp, monkeypatch):
    _bind_client(monkeypatch, cp)
    out = mcp.tool_roost_schedule(
        {"action": "create", "spec": {"kind": "command", "command": "true"},
         "every": "60"})
    assert out["spec"]["kind"] == "command"
    assert out["interval_sec"] == 60


def test_roost_schedule_create_without_goal_or_spec_errors(cp, monkeypatch):
    _bind_client(monkeypatch, cp)
    out = mcp.tool_roost_schedule({"action": "create", "every": "30m"})
    assert out == {"error": "bad_args", "detail": "create needs `goal` or `spec`"}


def test_roost_schedule_create_without_every_errors(cp, monkeypatch):
    _bind_client(monkeypatch, cp)
    out = mcp.tool_roost_schedule({"action": "create", "goal": "do it"})
    assert out["error"] == "bad_args"
    assert "every" in out["detail"]


def test_roost_schedule_mutation_without_id_errors(cp, monkeypatch):
    _bind_client(monkeypatch, cp)
    for action in ("remove", "enable", "disable"):
        out = mcp.tool_roost_schedule({"action": action})
        assert out["error"] == "bad_args"
        assert "schedule_id" in out["detail"]


def test_roost_schedule_unknown_action_errors(cp, monkeypatch):
    _bind_client(monkeypatch, cp)
    out = mcp.tool_roost_schedule({"action": "frobnicate"})
    assert out["error"] == "bad_args"
    assert "frobnicate" in out["detail"]


def test_roost_schedule_http_error_is_mapped(cp, monkeypatch):
    """A CP 4xx (e.g. too-short interval) comes back as an http_<code> tool error
    rather than raising — the error-mapping branch in roost_schedule."""
    _bind_client(monkeypatch, cp)
    out = mcp.tool_roost_schedule(
        {"action": "create", "goal": "x", "every": "5"})  # below 30s minimum
    assert out["error"].startswith("http_4")
    assert "30" in out["detail"]


def test_roost_schedule_remove_missing_is_http_404(cp, monkeypatch):
    _bind_client(monkeypatch, cp)
    out = mcp.tool_roost_schedule(
        {"action": "remove", "schedule_id": "does-not-exist"})
    assert out["error"] == "http_404"


# ---------- JSON-RPC plumbing: handle()/main() ----------


def test_handle_notification_returns_none():
    # No id -> a notification -> no response per JSON-RPC.
    assert mcp.handle({"jsonrpc": "2.0", "method": "tools/list"}) is None


def test_handle_ping():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "ping"})
    assert resp["result"] == {} and resp["id"] == 5


def test_handle_unknown_method():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "bogus/thing"})
    assert resp["error"]["code"] == -32601
    assert "method not found" in resp["error"]["message"]


def test_tool_impl_routes_to_matching_function():
    """Every TOOL_IMPL entry must point at the function named for that tool —
    a swapped route (e.g. roost_status -> tool_roost_cancel) is a real bug."""
    for name, fn in mcp.TOOL_IMPL.items():
        assert fn is getattr(mcp, f"tool_{name}"), (
            f"TOOL_IMPL[{name!r}] is wired to {fn.__name__}, "
            f"expected tool_{name}")
    # And every advertised tool has an implementation (and vice-versa).
    advertised = {t["name"] for t in mcp.TOOLS}
    assert advertised == set(mcp.TOOL_IMPL), (
        "TOOLS and TOOL_IMPL disagree on the tool set")


def test_handle_dispatches_to_the_real_named_impl(monkeypatch):
    """handle('tools/call', name=roost_status) must invoke the actual
    tool_roost_status — which GETs /jobs/{id}. A route swapped to roost_cancel
    would DELETE instead, so assert on the verb+path the dispatched impl used.
    """
    fc = _bind(monkeypatch, {("GET", "/jobs/jx"): _FakeResp(
        {"id": "jx", "state": "running"})})
    resp = mcp.handle({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                       "params": {"name": "roost_status",
                                  "arguments": {"job_id": "jx"}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["state"] == "running"
    # roost_status issues exactly one GET to the job path (not a DELETE/cancel).
    assert fc.calls == [("GET", "/jobs/jx", {})]


def test_handle_dispatches_cancel_via_delete(monkeypatch):
    """The mirror of the above: roost_cancel must route to tool_roost_cancel,
    which DELETEs /jobs/{id}. Together these pin the two routes apart so a swap
    between them is caught."""
    fc = _bind(monkeypatch, {("DELETE", "/jobs/jy"): _FakeResp({"cancelled": 1})})
    resp = mcp.handle({"jsonrpc": "2.0", "id": 100, "method": "tools/call",
                       "params": {"name": "roost_cancel",
                                  "arguments": {"job_id": "jy"}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["cancelled"] == 1
    assert fc.calls[0][0] == "DELETE" and fc.calls[0][1] == "/jobs/jy"


def test_handle_tools_call_success_serializes_payload(monkeypatch):
    monkeypatch.setitem(mcp.TOOL_IMPL, "roost_status",
                        lambda args: {"state": "running", "echo": args})
    resp = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "roost_status",
                                  "arguments": {"job_id": "j1"}}})
    assert "isError" not in resp["result"]
    text = resp["result"]["content"][0]["text"]
    assert json.loads(text)["state"] == "running"
    assert json.loads(text)["echo"] == {"job_id": "j1"}


def test_handle_tools_call_http_status_error_maps_to_iserror(monkeypatch):
    def _boom(_args):
        raise httpx.HTTPStatusError(
            "bad", request=httpx.Request("GET", "http://t/"),
            response=httpx.Response(503, text="upstream down"))
    monkeypatch.setitem(mcp.TOOL_IMPL, "roost_status", _boom)
    resp = mcp.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                       "params": {"name": "roost_status", "arguments": {}}})
    assert resp["result"]["isError"] is True
    txt = resp["result"]["content"][0]["text"]
    assert "http error 503" in txt and "upstream down" in txt


def test_handle_tools_call_generic_exception_maps_to_iserror(monkeypatch):
    def _boom(_args):
        raise ValueError("kaboom")
    monkeypatch.setitem(mcp.TOOL_IMPL, "roost_status", _boom)
    resp = mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "roost_status", "arguments": {}}})
    assert resp["result"]["isError"] is True
    assert "ValueError: kaboom" in resp["result"]["content"][0]["text"]


def test_handle_tools_call_missing_arguments_defaults_to_empty(monkeypatch):
    seen = {}
    monkeypatch.setitem(mcp.TOOL_IMPL, "roost_status",
                        lambda args: seen.update(args=args) or {"ok": True})
    mcp.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "roost_status"}})  # no "arguments" key
    assert seen["args"] == {}


def test_ok_and_err_helpers_shape():
    assert mcp._ok(1, {"a": 1}) == {"jsonrpc": "2.0", "id": 1, "result": {"a": 1}}
    e = mcp._err(2, -32000, "nope", data={"x": 1})
    assert e["error"] == {"code": -32000, "message": "nope", "data": {"x": 1}}
    # data omitted when None
    assert "data" not in mcp._err(3, -1, "m")["error"]


def test_main_reads_jsonrpc_lines_and_writes_responses(monkeypatch, capsys):
    """main() reads line-delimited JSON-RPC from stdin: it answers a request,
    skips blank lines, drops notifications, and emits a parse error for bad JSON.
    """
    import io
    lines = io.StringIO(
        "\n"  # blank line skipped
        + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "tools/list"}) + "\n"  # notif
        + "{not json}\n"
    )
    monkeypatch.setattr(mcp.sys, "stdin", lines)
    mcp.main()
    out_lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    parsed = [json.loads(l) for l in out_lines]
    # ping answered; notification produced nothing; parse error reported once.
    assert any(p.get("id") == 1 and p.get("result") == {} for p in parsed)
    assert any("error" in p and p["error"]["code"] == -32700 for p in parsed)
    # the notification (no id) must NOT have produced a response line
    assert sum(1 for p in parsed if p.get("id") == 1) == 1


# ---------- roost_do: remaining branches (captain-unavailable, model, parent) ----------


def test_roost_do_multi_captain_unavailable(monkeypatch):
    monkeypatch.setattr(roost_cli, "_classify_goal", lambda _g: _plan(mode="multi"))

    def _no_claude(*a, **k):
        raise FileNotFoundError("claude not found on PATH")

    monkeypatch.setattr(roost_cli, "dispatch_goal", _no_claude)
    out = mcp.tool_roost_do({"goal": "a and b"})
    assert out["error"] == "captain_unavailable"
    assert "claude not found" in out["detail"]


def test_roost_do_multi_nonzero_rc_is_failed(monkeypatch):
    monkeypatch.setattr(roost_cli, "_classify_goal", lambda _g: _plan(mode="multi"))
    monkeypatch.setattr(roost_cli, "dispatch_goal", lambda *a, **k: ("root-1", 2))
    out = mcp.tool_roost_do({"goal": "a and b"})
    assert out["state"] == "failed" and out["run_id"] == "root-1"


def test_roost_do_single_forwards_model_and_parent(monkeypatch):
    posted = {}
    def _post(self, path, **kw):
        posted["body"] = kw["json"]
        return _FakeResp({"id": "job-m", "state": "queued"})
    monkeypatch.setattr(roost_cli, "_classify_goal", lambda _g: _plan())
    fc = _FakeClient({("POST", "/jobs"): _post})
    monkeypatch.setattr(mcp, "_client", lambda: fc)
    monkeypatch.setenv("ROOST_PARENT_JOB_ID", "p-do")
    out = mcp.tool_roost_do({"goal": "report hostname", "model": "haiku",
                             "wallclock_min": 5})
    assert out["run_id"] == "job-m"
    assert posted["body"]["model"] == "haiku"
    assert posted["body"]["parent_job_id"] == "p-do"
    assert posted["body"]["budget"]["max_wallclock_min"] == 5


# ---------- transfer tools: ttl param + parent attach on the delivery/upload job ----------


def test_stage_file_passes_ttl_param(monkeypatch, tmp_path):
    posted = {}
    def _post(self, path, **kw):
        posted["params"] = kw["params"]
        return _FakeResp({"id": "b", "name": "x.bin", "size": 4, "sha256": "ab",
                          "get_url": "http://cp/blobs/b?sig=x", "expires_at": 9.0})
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")
    fc = _FakeClient({("POST", "/blobs"): _post})
    monkeypatch.setattr(mcp, "_client", lambda: fc)
    out = mcp.tool_stage_file({"path": str(f), "ttl_sec": 120})
    assert out["id"] == "b"
    assert posted["params"]["ttl_sec"] == 120
    assert posted["params"]["name"] == "x.bin"


def test_send_file_attaches_parent_to_delivery_job(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mcp, "_resolve_worker",
        lambda w: {"id": "w1", "name": "box",
                   "capabilities": {"hostname": "box-h", "os": "linux"}})
    posted = {}
    def _blob_post(self, path, **kw):
        return _FakeResp({"id": "b", "name": "m.bin", "size": 7, "sha256": "deadbeef",
                          "get_url": "http://cp/blobs/b?sig=x", "expires_at": 9.0})
    def _job_post(self, path, **kw):
        posted["body"] = kw["json"]
        return _FakeResp({"id": "deliver-job", "state": "queued"})
    fc = _FakeClient({("POST", "/blobs"): _blob_post,
                      ("POST", "/jobs"): _job_post})
    monkeypatch.setattr(mcp, "_client", lambda: fc)
    monkeypatch.setenv("ROOST_PARENT_JOB_ID", "p-send")
    f = tmp_path / "m.bin"
    f.write_bytes(b"weights")
    out = mcp.tool_send_file({"worker": "box", "local_path": str(f),
                              "destination_path": "/opt/m.bin", "ttl_sec": 600})
    assert out["job_id"] == "deliver-job"
    assert posted["body"]["parent_job_id"] == "p-send"
    assert posted["body"]["requires"] == {"hostname": "==box-h"}


def test_fetch_file_attaches_parent_to_upload_job(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mcp, "_resolve_worker",
        lambda w: {"id": "w1", "name": "box",
                   "capabilities": {"hostname": "box-h"}})
    posted = {}
    def _job_post(self, path, **kw):
        posted["body"] = kw["json"]
        return _FakeResp({"id": "up-job", "state": "queued"})
    routes = {
        ("POST", "/blobs/presign"): _FakeResp(
            {"id": "blob-1", "put_url": "http://cp/blobs/blob-1/put?sig=x"}),
        ("POST", "/jobs"): _job_post,
        ("GET", "/blobs/blob-1"): _FakeResp(content=b"remote-bytes"),
    }
    fc = _FakeClient(routes)
    monkeypatch.setattr(mcp, "_client", lambda: fc)
    monkeypatch.setenv("ROOST_PARENT_JOB_ID", "p-fetch")
    monkeypatch.setattr(mcp, "tool_roost_wait",
                        lambda a: {"id": "up-job", "state": "succeeded"})
    out = mcp.tool_fetch_file({"worker": "box", "remote_path": "/var/log/x.log",
                               "local_path": str(tmp_path / "got.log")})
    assert posted["body"]["parent_job_id"] == "p-fetch"
    assert out["size"] == len(b"remote-bytes")
    assert Path(out["local_path"]).read_bytes() == b"remote-bytes"
