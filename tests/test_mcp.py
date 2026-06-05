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
