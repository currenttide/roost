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


def test_least_utilized_worker_scores_higher():
    # SPREAD load: among workers of the same capacity, the LESS-loaded one wins
    # so jobs fan out rather than stacking onto one box.
    job = {}
    lighter = placement_score(_w("lighter", running=1, capacity=4), job, now=100.0)
    heavier = placement_score(_w("heavier", running=3, capacity=4), job, now=100.0)
    assert lighter > heavier


def test_two_equally_idle_workers_tie_on_free_fraction():
    # An idle small box competes evenly with an idle big box: both are 100% free,
    # so the free-slot term contributes equally (no concentration onto the big
    # box). Other terms are equal here, so the total scores tie.
    job = {}
    big = placement_score(_w("big", running=0, capacity=4), job, now=100.0)
    small = placement_score(_w("small", running=0, capacity=1), job, now=100.0)
    assert big == small


def test_free_fraction_spreads_off_loaded_big_box():
    # A capacity-4 worker already running 3 jobs is 25% free; a capacity-2 worker
    # running 0 is 100% free — the less-utilized one is preferred for spread.
    job = {}
    loaded = placement_score(_w("loaded", running=3, capacity=4), job, now=100.0)
    fresh = placement_score(_w("fresh", running=0, capacity=2), job, now=100.0)
    assert fresh > loaded


def test_capacity_defaults_to_one_when_absent():
    # An older worker that doesn't report capacity is treated as capacity 1;
    # idle (running=0) → fully free, no crash. Capacity falls back to the row
    # column if present in the worker dict; a fully-idle row of any capacity is
    # 100% free, so it ties the legacy fully-idle worker on this term.
    job = {}
    legacy = placement_score(_w("legacy", running=0), job, now=100.0)
    via_row = placement_score(
        {"id": "r", "status": "idle", "capacity": 3,
         "capabilities": {"load": {"running": 0}}, "last_assigned_at": None},
        job, now=100.0)
    assert via_row == legacy  # both fully idle → equal free fraction


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


# ---------- [R18] non-numeric caps vs numeric constraints ----------


def test_non_numeric_cap_does_not_satisfy_numeric_neq():
    # A worker whose capability probe produced a sentinel string must NOT
    # pass a numeric exclusion filter via the string-compare fallback.
    assert matches({"gpu_vram_gb": "N/A"}, {"gpu_vram_gb": "!=0"}) is False
    assert matches({"gpu_vram_gb": "none"}, {"gpu_vram_gb": "!=0"}) is False
    assert matches({"gpu_vram_gb": ""}, {"gpu_vram_gb": "!=0"}) is False


def test_non_numeric_cap_rejects_every_numeric_operator():
    for req in (">=8", "<=8", ">8", "<8", "==8", "!=8"):
        assert matches({"gpu_vram_gb": "unknown"}, {"gpu_vram_gb": req}) is False, req


def test_numeric_string_cap_still_satisfies_numeric_ops():
    # Numeric-looking strings keep coercing (pre-existing behavior).
    assert matches({"gpu_vram_gb": "24"}, {"gpu_vram_gb": ">=24"}) is True
    assert matches({"gpu_vram_gb": "24"}, {"gpu_vram_gb": "!=0"}) is True


def test_string_pins_unaffected_by_the_numeric_guard():
    # Both sides non-numeric → the documented hostname pin behavior holds.
    assert matches({"hostname": "dgx"}, {"hostname": "==dgx"}) is True
    assert matches({"hostname": "dgx"}, {"hostname": "!=pi0"}) is True
    assert matches({"hostname": "pi0"}, {"hostname": "!=pi0"}) is False
    # Numeric-looking cap against a string rhs still string-compares.
    assert matches({"hostname": "8"}, {"hostname": "!=pi0"}) is True


def test_non_finite_sentinels_rejected():
    # float("nan") != 0 is True and float("inf") >= anything — a broken probe
    # emitting "nan"/"inf" must not place via those quirks.
    assert matches({"gpu_vram_gb": "nan"}, {"gpu_vram_gb": "!=0"}) is False
    assert matches({"gpu_vram_gb": "inf"}, {"gpu_vram_gb": ">=24"}) is False
    assert matches({"gpu_vram_gb": float("nan")}, {"gpu_vram_gb": "!=0"}) is False


# ---------- [R20] prefer-by-name parity with target ----------


def test_prefer_resolves_name_like_id():
    import pytest
    import time as _t
    from roost.matcher import placement_score
    now = _t.time()
    w = {"id": "abc123", "name": "gpu-node", "status": "idle",
         "capabilities": {"cpus": 8}, "capacity": 1}
    base = placement_score(w, {}, now=now)
    by_id = placement_score(w, {"prefer": {"worker": "abc123"}}, now=now)
    by_name = placement_score(w, {"prefer": {"worker": "gpu-node"}}, now=now)
    by_str = placement_score(w, {"prefer": "gpu-node"}, now=now)
    assert by_id - base == pytest.approx(1000.0)
    assert by_name == by_id          # name gets the same preference bonus
    assert by_str == by_id           # bare-string prefer too
    # A non-matching prefer gives no bonus.
    other = placement_score(w, {"prefer": {"worker": "someone-else"}}, now=now)
    assert other == base
