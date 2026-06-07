"""Captain (intelligent dispatch) — deterministic unit coverage.

The live agent behavior is exercised by the `roost dispatch` smoke test; here
we lock the prompt/argv/config construction that must not regress.
"""

from __future__ import annotations

import json
from pathlib import Path

from roost import captain


FLEET = [
    {
        "name": "dgx", "id": "w-gpu", "status": "idle",
        "capabilities": {
            "os": "linux", "arch": "x86_64", "cpus": 64, "gpu_vram_gb": 80,
            "tools": ["claude", "git"],
            "load": {"running": 0, "loadavg1": 0.4, "free_vram_gb": 79.2},
            "repos": ["pyg-deep-research"],
        },
    },
    {
        "name": "home", "id": "w-cpu", "status": "busy",
        "capabilities": {"os": "linux", "arch": "arm64", "cpus": 8, "tools": ["python3"]},
    },
]


def test_render_fleet_includes_state_and_locality():
    out = captain.render_fleet(FLEET)
    assert "name=dgx" in out and "status=idle" in out
    assert "gpu_vram_gb=80" in out
    assert "free_vram_gb" in out  # live load surfaced
    assert "repos=" in out        # operator-declared locality fact surfaced
    assert "status=busy" in out


def test_render_fleet_empty():
    assert "no workers" in captain.render_fleet([])


def test_build_prompt_carries_goal_and_budget():
    p = captain.build_prompt("do the thing", FLEET, budget_note="under 1000 tokens")
    assert "do the thing" in p
    assert "under 1000 tokens" in p
    assert "roost_workers" in p  # system instructions present


def test_system_prompt_instructs_recording_a_plan_reason():
    """R33: the captain must be told to set a per-sub-job `reason` so its plan is
    visible in `roost tree`."""
    p = captain.SYSTEM_PROMPT
    assert "reason" in p
    assert "roost tree" in p


def test_build_argv_restricts_tools_and_uses_mcp_config(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    argv = captain.build_argv("prompt text", cfg, model="claude-sonnet-4-6")
    assert argv[0] == "claude" and argv[1] == "-p" and argv[2] == "prompt text"
    assert "--mcp-config" in argv and str(cfg) in argv
    assert "--model" in argv and "claude-sonnet-4-6" in argv
    # Only roost-mcp tools are allowed (no Bash / file tools).
    allowed = argv[argv.index("--allowedTools") + 1]
    assert allowed == ",".join(captain.ALLOWED_TOOLS)
    assert all(t.startswith("mcp__roost__") for t in allowed.split(","))


def test_build_argv_defaults_to_sonnet_when_no_model(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    argv = captain.build_argv("prompt text", cfg, model=None)
    # "Sonnet by default everywhere": no explicit model → Sonnet, not Claude Code's
    # ambient default.
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == captain.DEFAULT_MODEL == "claude-sonnet-4-6"


def test_build_argv_explicit_model_overrides_default(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    argv = captain.build_argv("prompt text", cfg, model="claude-opus-4-1")
    assert argv[argv.index("--model") + 1] == "claude-opus-4-1"


def test_write_mcp_config_points_at_roost_mcp_without_parent():
    path = captain.write_mcp_config("http://cp:8787", "tok-123")
    try:
        cfg = json.loads(path.read_text())
        server = cfg["mcpServers"]["roost"]
        assert server["args"] == ["-m", "roost.mcp"]
        assert server["env"]["ROOST_URL"] == "http://cp:8787"
        assert server["env"]["ROOST_TOKEN"] == "tok-123"
        # Local captain → no parent job, so its sub-jobs are lineage roots.
        assert "ROOST_PARENT_JOB_ID" not in server["env"]
    finally:
        path.unlink(missing_ok=True)
