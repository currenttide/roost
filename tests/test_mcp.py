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
