"""Tests for roost/triage.py — the bare-worker triage prompt rendering (R17).

The prompt is the worker's accept/decline "brain" for kind: auto; these
tests pin what the rendered prompt actually tells the agent: this machine's
specs, the fleet snapshot, the decline protocol, and the don't-delegate rule.
"""
from __future__ import annotations

from roost import triage


CAPS_GPU = {
    "hostname": "gpubox", "arch": "x86_64", "cpus": 32,
    "gpu_count": 2, "gpu": ["RTX 4090"], "gpu_vram_gb": 24,
    "docker": True, "docker_gpu": True,
    "tools": ["python3", "claude"],
}
CAPS_PI = {"hostname": "pi", "arch": "aarch64", "cpus": 4}


def test_render_fills_all_placeholders():
    out = triage.render(CAPS_GPU, [])
    assert "{capabilities}" not in out
    assert "{fleet}" not in out
    assert "{decline_marker}" not in out


def test_render_contains_machine_specs():
    out = triage.render(CAPS_GPU, [])
    assert "- hostname: gpubox  (x86_64)" in out
    assert "- CPU cores: 32" in out
    assert "- GPUs: 2× RTX 4090, ~24GB VRAM each" in out
    assert "- docker: yes (GPU containers)" in out
    assert "- tools available: python3, claude" in out


def test_render_cpu_only_machine():
    out = triage.render(CAPS_PI, [])
    assert "- GPUs: none" in out
    assert "- docker: no" in out
    assert "(basic shell only)" in out  # no tools detected


def test_decline_protocol_is_explicit():
    out = triage.render(CAPS_PI, [])
    # The agent is told the exact sentinel and that it must be the FINAL line.
    assert triage.DECLINE_MARKER == "ROOST_DECLINE:"
    assert f"{triage.DECLINE_MARKER} <one-line reason" in out
    assert "FINAL line EXACTLY" in out
    # And the no-delegation rule that prevents job-spawning loops.
    assert "Do NOT call the\n`roost` CLI" in out or "Do NOT call" in out


def test_fleet_summary_rendering():
    fleet = [
        {"name": "gpubox", "status": "idle",
         "capabilities": {"cpus": 32, "gpu_count": 2, "gpu_vram_gb": 24}},
        {"name": "pi", "status": "busy", "capabilities": {"cpus": 4}},
    ]
    out = triage.render(CAPS_PI, fleet)
    assert "- gpubox: 32 cores, 2× GPU ~24GB, status=idle" in out
    assert "- pi: 4 cores, no GPU, status=busy" in out


def test_fleet_empty_snapshot_message():
    out = triage.render(CAPS_PI, None)
    assert "(no snapshot available" in out


def test_fleet_capped_at_20_rows():
    fleet = [{"name": f"n{i}", "status": "idle", "capabilities": {"cpus": 1}}
             for i in range(50)]
    out = triage.render(CAPS_PI, fleet)
    assert "- n19:" in out
    assert "- n20:" not in out  # capped — the prompt stays bounded


def test_missing_capability_fields_degrade_gracefully():
    out = triage.render({}, [{"name": "x"}, {}])
    assert "- hostname: ?  (?)" in out
    assert "- CPU cores: None" in out
    assert "- x: ? cores, no GPU, status=?" in out  # tolerant fleet row
