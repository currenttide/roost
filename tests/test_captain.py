"""Captain (intelligent dispatch) — deterministic unit coverage.

The live agent behavior is exercised by the `roost dispatch` smoke test; here
we lock the prompt/argv/config construction that must not regress.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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


def test_write_mcp_config_threads_parent_job_id_for_shared_lineage():
    """V2-1: when the captain anchors a captain-root, its sub-jobs must attach to
    that root via ROOST_PARENT_JOB_ID so the whole plan shares one lineage tree
    and one tree budget. This is the env the spawned roost-mcp inherits."""
    path = captain.write_mcp_config("http://cp:8787", "tok-123", parent_job_id="root-xyz")
    try:
        env = json.loads(path.read_text())["mcpServers"]["roost"]["env"]
        assert env["ROOST_PARENT_JOB_ID"] == "root-xyz"
        # The other env still flows through unchanged.
        assert env["ROOST_URL"] == "http://cp:8787"
        assert env["ROOST_TOKEN"] == "tok-123"
    finally:
        path.unlink(missing_ok=True)


# ---------- render_fleet: per-capability branch fill ----------


def test_render_fleet_surfaces_docker_and_gpu_facets():
    """Each capability facet the captain needs for placement must show up: GPU
    count/name, docker / docker_gpu flags. (These gate kind=docker decisions.)"""
    fleet = [
        {
            "name": "rig", "id": "w1", "status": "idle",
            "capabilities": {
                "os": "linux", "arch": "x86_64", "cpus": 32, "ram_gb": 256,
                "hostname": "rig.local",
                "gpu": "A100", "gpu_count": 4, "gpu_vram_gb": 80,
                "docker": True, "docker_gpu": True,
                "tools": ["claude"],
                "load": {"running": 1, "loadavg1": 2.0, "free_vram_gb": 60.0},
            },
        }
    ]
    out = captain.render_fleet(fleet)
    assert "hostname=rig.local" in out
    assert "gpu=A100" in out
    assert "gpu_count=4" in out
    assert "docker=true" in out
    assert "docker_gpu=true" in out
    assert "cpus=32" in out
    assert "ram_gb=256" in out
    assert "load=" in out


def test_render_fleet_omits_absent_capabilities():
    """A bare worker (no gpu/docker/cpus/ram/load) renders cleanly with only the
    facets it actually advertises — no `gpu_count=`, no `docker=`, no `load=`,
    no stray `cpus=`/`ram_gb=`. The captain must not see capabilities a node
    lacks (the SYSTEM_PROMPT forbids inventing them)."""
    fleet = [{"name": "bare", "id": "w0", "status": "offline", "capabilities": {}}]
    out = captain.render_fleet(fleet)
    assert "name=bare" in out and "status=offline" in out
    for absent in ("gpu_count=", "gpu=", "docker=", "docker_gpu=",
                   "cpus=", "ram_gb=", "load=", "hostname="):
        assert absent not in out


def test_render_fleet_surfaces_extra_operator_facts_only_once():
    """Operator-declared extras (repos/caches/mcp servers) are surfaced for
    locality, but the KNOWN keys (os/arch/python/gpus) are NOT re-dumped by the
    catch-all loop."""
    fleet = [{
        "name": "n", "id": "w", "status": "idle",
        "capabilities": {
            "os": "linux", "arch": "x86_64", "python": "3.12",
            "gpus": [0, 1],  # known key — must NOT leak via the extras loop
            "datasets": ["imagenet"],  # extra — must surface
        },
    }]
    out = captain.render_fleet(fleet)
    assert "datasets=" in out and "imagenet" in out
    # Known keys handled explicitly above are not echoed by the extras loop.
    assert "os=" not in out
    assert "python=" not in out
    assert "gpus=" not in out


# ---------- build_prompt: optional budget note ----------


def test_build_prompt_omits_budget_section_when_none():
    """No overall budget → no '## Overall budget' section, but the goal and the
    fleet snapshot are still present."""
    p = captain.build_prompt("ship it", FLEET, budget_note=None)
    assert "## Overall budget" not in p
    assert "ship it" in p
    assert "name=dgx" in p  # fleet still rendered into the prompt


# ---------- run(): orchestration, cleanup, and failure paths ----------


class FakeRun:
    """Records the argv captain shells out with; returns a scripted rc. Also
    snapshots whether the --mcp-config file exists AT CALL TIME (it must, so the
    spawned `claude` can read it) — proving cleanup happens only afterwards."""

    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.mcp_config_existed_during_call: bool | None = None

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        cfg = argv[argv.index("--mcp-config") + 1]
        self.mcp_config_existed_during_call = Path(cfg).exists()
        return subprocess.CompletedProcess(argv, self.returncode)


def _claude_present(monkeypatch):
    monkeypatch.setattr(captain.shutil, "which",
                        lambda name: "/usr/bin/claude" if name == "claude" else None)


def test_run_requires_claude_on_path(monkeypatch):
    """No `claude` CLI → the captain cannot run; it raises clearly and never
    shells out or leaves a temp mcp-config behind."""
    monkeypatch.setattr(captain.shutil, "which", lambda _name: None)
    called = []
    monkeypatch.setattr(captain.subprocess, "run",
                        lambda *a, **k: called.append(a) or subprocess.CompletedProcess(a, 0))
    with pytest.raises(FileNotFoundError, match="claude"):
        captain.run("http://cp:8787", "tok", "do the thing", FLEET)
    assert called == []  # bailed before dispatch


def test_run_builds_full_dispatch_argv_and_returns_rc(monkeypatch, tmp_path):
    """The captain end-to-end (minus the live LLM): it builds the prompt from the
    goal+fleet+budget, writes a real roost-mcp config carrying the captain-root
    lineage, shells out to `claude` with the restricted toolset and chosen model,
    and returns the child's exit code."""
    _claude_present(monkeypatch)
    fake = FakeRun(returncode=0)
    monkeypatch.setattr(captain.subprocess, "run", fake)
    # Capture the config path the captain actually wrote so we can prove cleanup.
    written: list[Path] = []
    real_write = captain.write_mcp_config
    monkeypatch.setattr(captain, "write_mcp_config",
                        lambda *a, **k: written.append(real_write(*a, **k)) or written[-1])

    rc = captain.run("http://cp:8787", "tok-9", "train a model",
                     FLEET, model="claude-opus-4-1",
                     budget_note="under 5k tokens", parent_job_id="root-77")

    assert rc == 0
    assert len(fake.calls) == 1
    argv = fake.calls[0]
    # Shape: claude -p <prompt> --mcp-config <cfg> --allowedTools <...> --verbose --model <...>
    assert argv[0] == "claude" and argv[1] == "-p"
    prompt = argv[2]
    assert "train a model" in prompt           # goal threaded in
    assert "under 5k tokens" in prompt          # budget threaded in
    assert "name=dgx" in prompt                 # fleet snapshot threaded in
    assert argv[argv.index("--model") + 1] == "claude-opus-4-1"
    allowed = argv[argv.index("--allowedTools") + 1]
    assert allowed == ",".join(captain.ALLOWED_TOOLS)
    # The mcp-config carried the captain-root lineage and existed during the call.
    cfg_path = Path(argv[argv.index("--mcp-config") + 1])
    assert fake.mcp_config_existed_during_call is True
    assert written and written[0] == cfg_path
    # ... and the `finally` removed it after the child returned.
    assert not cfg_path.exists()


def test_run_defaults_to_sonnet_when_no_model(monkeypatch):
    """No --model from the caller → the captain runs on Sonnet (not Claude Code's
    ambient default)."""
    _claude_present(monkeypatch)
    fake = FakeRun()
    monkeypatch.setattr(captain.subprocess, "run", fake)
    captain.run("http://cp:8787", "tok", "g", FLEET)
    argv = fake.calls[0]
    assert argv[argv.index("--model") + 1] == captain.DEFAULT_MODEL == "claude-sonnet-4-6"


def test_run_propagates_nonzero_child_rc(monkeypatch):
    """A failed captain run surfaces the child's nonzero rc (the CLI uses this to
    exit nonzero) — the captain does not swallow failures."""
    _claude_present(monkeypatch)
    fake = FakeRun(returncode=2)
    monkeypatch.setattr(captain.subprocess, "run", fake)
    rc = captain.run("http://cp:8787", "tok", "g", FLEET)
    assert rc == 2


def test_run_cleans_up_mcp_config_even_when_subprocess_raises(monkeypatch):
    """If the launch itself blows up mid-run, the `finally` must still remove the
    temp mcp-config — no leak of a credential-bearing file on the error path."""
    _claude_present(monkeypatch)
    written: list[Path] = []
    real_write = captain.write_mcp_config
    monkeypatch.setattr(captain, "write_mcp_config",
                        lambda *a, **k: written.append(real_write(*a, **k)) or written[-1])

    def _boom(*a, **k):
        raise OSError("exec failed")
    monkeypatch.setattr(captain.subprocess, "run", _boom)

    with pytest.raises(OSError, match="exec failed"):
        captain.run("http://cp:8787", "tok", "g", FLEET)
    assert written and not written[0].exists()  # cleaned up despite the failure
