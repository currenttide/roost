from roost.matcher import matches, placement_score


def test_empty_requirements_always_match():
    assert matches({}, {}) is True
    assert matches({"anything": 1}, None) is True


def test_exact_string_match():
    assert matches({"os": "linux"}, {"os": "linux"}) is True
    assert matches({"os": "linux"}, {"os": "darwin"}) is False


def test_gte_lt_lte_gt_eq_neq():
    caps = {"gpu_vram_gb": 24, "cpus": 8}
    assert matches(caps, {"gpu_vram_gb": ">=24"}) is True
    assert matches(caps, {"gpu_vram_gb": ">=32"}) is False
    assert matches(caps, {"gpu_vram_gb": "<=24"}) is True
    assert matches(caps, {"gpu_vram_gb": "<24"}) is False
    assert matches(caps, {"gpu_vram_gb": ">23"}) is True
    assert matches(caps, {"gpu_vram_gb": "==24"}) is True
    assert matches(caps, {"gpu_vram_gb": "!=24"}) is False


def test_missing_key_fails():
    assert matches({"os": "linux"}, {"gpu_vram_gb": ">=8"}) is False


def test_list_requires_subset_in_capability_list():
    caps = {"tools": ["claude", "git", "python3"], "mcp": ["era-context"]}
    assert matches(caps, {"tools": ["claude"]}) is True
    assert matches(caps, {"tools": ["claude", "git"]}) is True
    assert matches(caps, {"tools": ["claude", "codex"]}) is False
    assert matches(caps, {"mcp": ["era-context"]}) is True


def test_string_value_satisfies_single_item_list():
    assert matches({"repo": "pyg-deep-research"}, {"repo": ["pyg-deep-research"]}) is True
    assert matches({"repo": "other-repo"}, {"repo": ["pyg-deep-research"]}) is False


def test_nested_dict_requirement():
    caps = {"limits": {"max_tokens": 200000, "max_wallclock_min": 30}}
    assert matches(caps, {"limits": {"max_tokens": ">=100000"}}) is True
    assert matches(caps, {"limits": {"max_tokens": ">=300000"}}) is False


def test_non_numeric_comparator_lhs_fails():
    assert matches({"gpu_vram_gb": "not-a-number"}, {"gpu_vram_gb": ">=8"}) is False


# ---------- placement_score (V2-2) ----------

def _w(wid, status="idle", **load):
    caps = {"tools": ["python3"]}
    if load:
        caps["load"] = load
    return {"id": wid, "status": status, "capabilities": caps, "last_assigned_at": None}


def test_prefer_dominates_all_other_signals():
    job = {"prefer": {"worker": "w-pref"}}
    pref = placement_score(_w("w-pref", loadavg1=9.0), job, now=100.0)
    other = placement_score(_w("w-other", loadavg1=0.0), job, now=100.0)
    assert pref > other  # prefer bonus outweighs the load penalty


def test_idle_beats_busy():
    job = {}
    idle = placement_score(_w("a", running=0), job, now=100.0)
    busy = placement_score(_w("b", running=1), job, now=100.0)
    assert idle > busy


def test_lower_loadavg_and_more_vram_score_higher():
    job = {}
    light = placement_score(_w("a", running=0, loadavg1=0.1, free_vram_gb=70), job, now=100.0)
    heavy = placement_score(_w("b", running=0, loadavg1=5.0, free_vram_gb=2), job, now=100.0)
    assert light > heavy


def test_recency_breaks_ties_for_spread():
    job = {}
    recent = {"id": "a", "status": "idle", "capabilities": {}, "last_assigned_at": 99.0}
    stale = {"id": "b", "status": "idle", "capabilities": {}, "last_assigned_at": 10.0}
    # The one assigned longer ago scores higher (gets the next job).
    assert placement_score(stale, job, now=100.0) > placement_score(recent, job, now=100.0)


def test_string_equality_pin_hostname():
    # Regression: `==`/`!=` with non-numeric operands must do string compare
    # so documented hard pins like `hostname: ==pi0` actually match.
    caps = {"hostname": "pi0"}
    assert matches(caps, {"hostname": "==pi0"}) is True
    assert matches(caps, {"hostname": "==pi9"}) is False
    assert matches(caps, {"hostname": "!=pi9"}) is True
    assert matches(caps, {"hostname": "!=pi0"}) is False
    # Missing capability with == pin must not match (no crash).
    assert matches({}, {"hostname": "==pi0"}) is False


def test_activity_from_stream_json():
    from roost.worker import activity_from_stream_json as act
    assert act({"type": "system", "subtype": "init"}).startswith("init")
    assert act({"type": "result", "subtype": "success"}) == "done"
    tool = act({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}})
    assert tool.startswith("→ Bash") and "pytest" in tool
    txt = act({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "thinking about it"}]}})
    assert txt.startswith("💬")
    assert act({"type": "assistant", "message": {"content": []}}) is None
    assert act({"weird": "shape"}) is None  # never raises


def test_build_docker_argv():
    from roost.worker import build_command, docker_container_name
    spec = {
        "kind": "docker",
        "image": "pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime",
        "command": "python train.py",
        "container": {"gpus": "all", "cpus": "8", "memory": "32g",
                      "volumes": ["/data:/data:ro"], "env": {"WANDB_MODE": "offline"},
                      "workdir": "/workspace", "shm_size": "16g"},
    }
    argv, _, _ = build_command(spec, "abc123")
    assert argv[:4] == ["docker", "run", "--rm", "--name"]
    assert docker_container_name("abc123") in argv
    assert "--gpus" in argv and argv[argv.index("--gpus") + 1] == "all"
    assert "--cpus" in argv and "--memory" in argv and "--shm-size" in argv
    assert "-v" in argv and "/data:/data:ro" in argv
    assert "-e" in argv and "WANDB_MODE=offline" in argv
    assert "-w" in argv and "/workspace" in argv
    # image precedes the in-container command
    img_i = argv.index("pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime")
    assert argv[img_i + 1:] == ["sh", "-c", "python train.py"]


def test_docker_job_requires_image():
    from roost.worker import build_command
    import pytest as _pytest
    with _pytest.raises(ValueError):
        build_command({"kind": "docker", "command": "echo hi"}, "j1")


def test_neq_on_missing_capability_does_not_match():
    # A worker lacking the capability must NOT satisfy a constraint about it.
    assert matches({"tools": ["x"]}, {"gpu_vram_gb": "!=0"}) is False
    assert matches({"gpu_vram_gb": 24}, {"gpu_vram_gb": "!=0"}) is True
    assert matches({}, {"hostname": "!=pi0"}) is False


def test_config_toml_roundtrip_escapes_special_chars(tmp_path):
    from roost import config as cfg
    p = tmp_path / "c.toml"
    data = {"url": "http://h", "name": 'weird"name\\back', "credential": "tok-_."}
    cfg.save(data, p)
    assert cfg.load(p) == data


def test_service_env_quoting_and_xml_escaping(monkeypatch):
    from roost import service
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/me/My Creds & stuff")
    unit = service._render_systemd_unit("/x/roost")
    assert 'Environment="CLAUDE_CONFIG_DIR=/home/me/My Creds & stuff"' in unit
    import xml.dom.minidom as md
    md.parseString(service._render_launchd_plist("/x/roost"))  # must be valid XML
