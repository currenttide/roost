"""Bare-worker (kind: auto) pre-filter tests — the cheap deterministic gate that
declines an obvious capability mismatch without spending an LLM triage call."""
from __future__ import annotations

from pathlib import Path

import pytest

from roost.worker import (
    VERIFY_HEAL_TIMEOUT,
    _auto_prefilter,
    _budget_remaining,
    _sanitize_env,
    _validate_container,
)

NO_GPU = {"cpus": 4, "tools": ["claude"]}
GPU = {"cpus": 32, "gpu_count": 1, "gpu_vram_gb": 24, "tools": ["claude"]}
DOCKER_GPU = {"cpus": 8, "docker_gpu": True, "tools": ["claude"]}


@pytest.mark.parametrize("task", [
    "Run a CUDA matmul benchmark and report GFLOP/s.",
    "Report the GPU model and total VRAM via nvidia-smi.",
    "This task requires a GPU with >=16GB VRAM.",
    "Train a model on the GPU for 50 steps.",
    "Check torch.cuda.is_available() and report.",
])
def test_prefilter_declines_gpu_task_on_cpu_node(task):
    assert _auto_prefilter(task, NO_GPU) is not None


@pytest.mark.parametrize("task", [
    "Run a CUDA matmul benchmark and report GFLOP/s.",
    "Report the GPU model and total VRAM via nvidia-smi.",
])
def test_prefilter_passes_gpu_task_on_gpu_node(task):
    assert _auto_prefilter(task, GPU) is None
    assert _auto_prefilter(task, DOCKER_GPU) is None


@pytest.mark.parametrize("task", [
    "Print the hostname and number of CPU cores.",
    "Count how many prime numbers are below 10000.",
    "Reverse the string orchestrator and print it.",
    "In one sentence, explain what a GPU is.",   # mentions gpu but doesn't require one
])
def test_prefilter_passes_cpu_task_on_cpu_node(task):
    assert _auto_prefilter(task, NO_GPU) is None


# ---------- [M4] env sanitization ----------


@pytest.mark.parametrize("key", [
    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_SUBAGENT_MODEL",
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "all_proxy", "ALL_PROXY",
    "MY_PROXY", "NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
])
def test_sanitize_env_drops_dangerous_keys(key):
    cleaned, dropped = _sanitize_env({key: "evil", "SAFE": "ok"}, None)
    assert key in dropped
    assert key not in cleaned
    assert cleaned["SAFE"] == "ok"


def test_sanitize_env_keeps_ordinary_keys():
    cleaned, dropped = _sanitize_env(
        {"WANDB_MODE": "offline", "MY_VAR": "1", "PATH_EXTRA": "/opt"}, None)
    assert dropped == []
    assert cleaned == {"WANDB_MODE": "offline", "MY_VAR": "1", "PATH_EXTRA": "/opt"}


def test_sanitize_env_coerces_values_to_str():
    cleaned, _ = _sanitize_env({"N": 5, "B": True}, None)
    assert cleaned == {"N": "5", "B": "True"}


def test_sanitize_env_none_is_empty():
    assert _sanitize_env(None, None) == ({}, [])


def test_sanitize_env_policy_opt_in_allows_everything():
    cleaned, dropped = _sanitize_env(
        {"ANTHROPIC_BASE_URL": "http://evil", "HTTPS_PROXY": "p"},
        {"allow_unsafe_env": True})
    assert dropped == []
    assert cleaned == {"ANTHROPIC_BASE_URL": "http://evil", "HTTPS_PROXY": "p"}


# ---------- [H3] docker container validation ----------


def test_validate_container_allows_ordinary_mount():
    _validate_container({"volumes": ["/data:/data:ro"]}, None)  # no raise


def test_validate_container_rejects_home():
    with pytest.raises(ValueError):
        _validate_container({"volumes": [f"{Path.home()}:/h"]}, None)


def test_validate_container_rejects_claude_creds():
    with pytest.raises(ValueError):
        _validate_container({"volumes": [f"{Path.home()/'.claude'}:/c:ro"]}, None)


def test_validate_container_rejects_ssh_and_etc_and_root():
    for host in (f"{Path.home()/'.ssh'}", "/etc", "/root", "/etc/passwd"):
        with pytest.raises(ValueError):
            _validate_container({"volumes": [f"{host}:/x"]}, None)


def test_validate_container_rejects_whole_root_fs():
    with pytest.raises(ValueError):
        _validate_container({"volumes": ["/:/host"]}, None)


def test_validate_container_rejects_path_traversal():
    with pytest.raises(ValueError):
        _validate_container({"volumes": ["/data/../etc:/x"]}, None)
    with pytest.raises(ValueError):
        _validate_container({"volumes": ["..:/x"]}, None)


def test_validate_container_rejects_host_network():
    with pytest.raises(ValueError):
        _validate_container({"network": "host"}, None)


def test_validate_container_allows_named_network_and_volume():
    _validate_container({"network": "bridge", "volumes": ["myvol:/data"]}, None)


def test_validate_container_policy_opt_in_allows_sensitive():
    _validate_container(
        {"volumes": [f"{Path.home()}:/h"], "network": "host"},
        {"allow_host_mounts": True})  # no raise


# ---------- [C4/H2] verify/self-heal budget bounding ----------


def test_budget_remaining_no_budget_uses_default_cap():
    rem, exhausted = _budget_remaining({}, elapsed_s=10.0, tokens_used=999999)
    assert rem == VERIFY_HEAL_TIMEOUT
    assert exhausted is False


def test_budget_remaining_token_cap_exhausts():
    rem, exhausted = _budget_remaining(
        {"max_tokens": 1000}, elapsed_s=0.0, tokens_used=1000)
    assert exhausted is True and rem == 0.0


def test_budget_remaining_token_under_cap_ok():
    rem, exhausted = _budget_remaining(
        {"max_tokens": 1000}, elapsed_s=0.0, tokens_used=500)
    assert exhausted is False and rem == VERIFY_HEAL_TIMEOUT


def test_budget_remaining_wallclock_caps_per_subprocess():
    # 10 min total budget, 9 min already spent -> ~60s remaining, capped below 300.
    rem, exhausted = _budget_remaining(
        {"max_wallclock_min": 10}, elapsed_s=540.0, tokens_used=0)
    assert exhausted is False
    assert rem == pytest.approx(60.0)


def test_budget_remaining_wallclock_exhausted():
    rem, exhausted = _budget_remaining(
        {"max_wallclock_min": 10}, elapsed_s=600.0, tokens_used=0)
    assert exhausted is True and rem == 0.0


def test_budget_remaining_wallclock_headroom_capped_at_default():
    # Lots of budget left -> capped at the per-subprocess ceiling, not the full budget.
    rem, exhausted = _budget_remaining(
        {"max_wallclock_min": 60}, elapsed_s=0.0, tokens_used=0)
    assert exhausted is False and rem == VERIFY_HEAL_TIMEOUT


def test_budget_remaining_garbage_values_safe():
    rem, exhausted = _budget_remaining(
        {"max_tokens": "nan", "max_wallclock_min": "x"}, elapsed_s=5.0, tokens_used=10)
    assert exhausted is False and rem == VERIFY_HEAL_TIMEOUT


def test_validate_container_rejects_claude_config_dir(monkeypatch, tmp_path):
    # On shared boxes the live creds live under CLAUDE_CONFIG_DIR — block mounting it.
    ccd = tmp_path / "isolated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ccd))
    with pytest.raises(ValueError):
        _validate_container({"volumes": [f"{ccd}:/c:ro"]}, None)
