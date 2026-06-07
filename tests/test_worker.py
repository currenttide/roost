"""Bare-worker (kind: auto) pre-filter tests — the cheap deterministic gate that
declines an obvious capability mismatch without spending an LLM triage call."""
from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

from roost.worker import (
    DEFAULT_WALLCLOCK_FALLBACK_MIN,
    DEFAULT_WALLCLOCK_MIN,
    VERIFY_HEAL_TIMEOUT,
    Worker,
    _auto_prefilter,
    _budget_remaining,
    _build_claude_argv,
    _build_docker_argv,
    _resolve_timeout,
    _sanitize_env,
    _validate_container,
    build_bwrap_argv,
    detect_capabilities,
    load_snapshot,
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


# ---------- load_snapshot carries steward capacity (wire contract) ----------


def test_load_snapshot_includes_capacity_default():
    snap = load_snapshot(0)
    assert snap["running"] == 0
    assert snap["capacity"] == 1  # default fail-safe


def test_load_snapshot_includes_given_capacity():
    snap = load_snapshot(2, 5)
    assert snap["running"] == 2
    assert snap["capacity"] == 5


def test_load_snapshot_capacity_floored_at_one():
    # A bad/zero capacity must never report below 1 on the wire.
    assert load_snapshot(0, 0)["capacity"] == 1
    assert load_snapshot(0, -3)["capacity"] == 1


# ---------- [M4-parity] docker container.env sanitization ----------


def test_docker_argv_drops_unsafe_container_env():
    argv = _build_docker_argv(
        {"image": "alpine", "container": {"env": {
            "ANTHROPIC_BASE_URL": "http://evil", "HTTPS_PROXY": "p", "SAFE": "ok"}}},
        "job1", None)
    joined = " ".join(argv)
    assert "ANTHROPIC_BASE_URL" not in joined
    assert "HTTPS_PROXY" not in joined
    assert "SAFE=ok" in argv


def test_docker_argv_container_env_policy_opt_in():
    argv = _build_docker_argv(
        {"image": "alpine", "container": {"env": {"ANTHROPIC_BASE_URL": "http://x"}}},
        "job1", {"allow_unsafe_env": True})
    assert "ANTHROPIC_BASE_URL=http://x" in argv


# ---------- [R1] flag injection via spec-sourced argv positions ----------


def test_docker_argv_rejects_leading_dash_image():
    # `image: "--privileged"` would be parsed by docker as a flag, promoting the
    # first command element to the image — privilege escalation via spec.
    with pytest.raises(ValueError, match="leading '-'"):
        _build_docker_argv(
            {"image": "--privileged", "command": ["alpine", "sh", "-c", "id"]},
            "job1", None)


@pytest.mark.parametrize("field,value", [
    ("gpus", "--privileged"),
    ("cpus", "-1"),
    ("memory", "--pid=host"),
    ("shm_size", "--cap-add=ALL"),
    ("network", "--privileged"),
    ("workdir", "--volume=/:/host"),
])
def test_docker_argv_rejects_leading_dash_container_fields(field, value):
    with pytest.raises(ValueError, match="leading '-'"):
        _build_docker_argv(
            {"image": "alpine", "container": {field: value}}, "job1", None)


def test_docker_argv_rejects_leading_dash_volume():
    with pytest.raises(ValueError, match="leading '-'"):
        _build_docker_argv(
            {"image": "alpine", "container": {"volumes": ["--privileged"]}},
            "job1", None)


def test_docker_argv_rejects_whitespace_masked_dash():
    # Leading whitespace must not smuggle the dash past the check.
    with pytest.raises(ValueError, match="leading '-'"):
        _build_docker_argv(
            {"image": "  --privileged", "command": ["alpine"]}, "job1", None)


def test_docker_argv_rejects_empty_volume_entry():
    with pytest.raises(ValueError, match="must not be empty"):
        _build_docker_argv(
            {"image": "alpine", "container": {"volumes": [""]}}, "job1", None)


def test_docker_argv_rejects_whitespace_only_workdir():
    with pytest.raises(ValueError, match="must not be empty"):
        _build_docker_argv(
            {"image": "alpine", "container": {"workdir": "  "}}, "job1", None)


def test_docker_argv_legit_specs_still_build():
    # Ordinary values keep working, and in-container command flags stay legal —
    # they land after the image where docker stops flag parsing.
    argv = _build_docker_argv(
        {"image": "alpine:3.20",
         "command": ["ls", "-la", "/data"],
         "container": {"cpus": "2", "memory": "1g", "workdir": "/data",
                       "network": "bridge", "volumes": ["/data:/data:ro"]}},
        "job1", None)
    i = argv.index("alpine:3.20")
    assert argv[i + 1:] == ["ls", "-la", "/data"]
    assert "--cpus" in argv and "bridge" in argv


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


# ---------- [R2] default runtime cap for jobs with no wallclock budget ----------


def test_resolve_timeout_explicit_budget_wins():
    # An explicit budget always beats the default cap, whatever the kind.
    t, src = _resolve_timeout(
        {"kind": "docker", "budget": {"max_wallclock_min": 5}}, None)
    assert (t, src) == (300.0, "budget")
    t, src = _resolve_timeout({"budget": {"max_wallclock_sec": 90}}, None)
    assert (t, src) == (90.0, "budget")


@pytest.mark.parametrize("kind", ["command", "claude", "auto", "docker"])
def test_resolve_timeout_default_per_kind(kind):
    t, src = _resolve_timeout({"kind": kind}, None)
    assert src == "default"
    assert t == DEFAULT_WALLCLOCK_MIN[kind] * 60.0


def test_resolve_timeout_unknown_kind_uses_fallback():
    t, src = _resolve_timeout({"kind": "weird"}, None)
    assert src == "default"
    assert t == DEFAULT_WALLCLOCK_FALLBACK_MIN * 60.0


def test_resolve_timeout_missing_kind_is_claude():
    t, src = _resolve_timeout({}, None)
    assert src == "default"
    assert t == DEFAULT_WALLCLOCK_MIN["claude"] * 60.0


def test_resolve_timeout_policy_scalar_override():
    t, src = _resolve_timeout({"kind": "command"}, {"default_wallclock_min": 10})
    assert (t, src) == (600.0, "default")


def test_resolve_timeout_policy_per_kind_override():
    pol = {"default_wallclock_min": {"docker": 30}}
    t, src = _resolve_timeout({"kind": "docker"}, pol)
    assert (t, src) == (1800.0, "default")
    # Kinds absent from the mapping keep their built-in default.
    t, src = _resolve_timeout({"kind": "command"}, pol)
    assert t == DEFAULT_WALLCLOCK_MIN["command"] * 60.0 and src == "default"


def test_resolve_timeout_policy_zero_opts_out():
    t, src = _resolve_timeout({"kind": "command"}, {"default_wallclock_min": 0})
    assert (t, src) == (None, "none")
    t, src = _resolve_timeout(
        {"kind": "docker"}, {"default_wallclock_min": {"docker": -1}})
    assert (t, src) == (None, "none")


def test_resolve_timeout_garbage_budget_falls_to_default():
    t, src = _resolve_timeout(
        {"kind": "command", "budget": {"max_wallclock_min": "soon"}}, None)
    assert src == "default" and t == DEFAULT_WALLCLOCK_MIN["command"] * 60.0


def test_resolve_timeout_garbage_policy_falls_to_default():
    t, src = _resolve_timeout(
        {"kind": "command"}, {"default_wallclock_min": "lots"})
    assert src == "default" and t == DEFAULT_WALLCLOCK_MIN["command"] * 60.0


def _mk_runjob_worker(policy):
    """Worker with the network stubbed out, for driving the REAL run_job."""
    w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
    w.policy = policy
    events: list[dict] = []
    logs: list[tuple[str, str]] = []

    async def fake_post_event(job_id, event):
        events.append(event)

    async def fake_send_log(job_id, stream, data):
        logs.append((stream, data))

    async def fake_diagnose(spec, **kw):
        return {"summary": "stub", "category": "stub", "retriable": False}

    w._post_event = fake_post_event  # type: ignore[assignment]
    w._send_log = fake_send_log  # type: ignore[assignment]
    w._diagnose_failure = fake_diagnose  # type: ignore[assignment]
    return w, events, logs


def test_run_job_default_cap_kills_unbudgeted_job():
    """A no-budget job is killed by the per-kind default cap and reports the
    R2-distinct error token (not plain wallclock_exceeded)."""
    async def go():
        # 0.01 min = 0.6s cap via policy override; job would run 30s unbounded.
        w, events, logs = _mk_runjob_worker({"default_wallclock_min": 0.01})
        await asyncio.wait_for(
            w.run_job({"id": "jt1", "spec": {"kind": "command", "command": "sleep 30"}}),
            timeout=15.0)
        await w.close()
        terminal = [e for e in events if e.get("type") in ("succeeded", "failed")]
        assert terminal and terminal[-1]["type"] == "failed"
        assert terminal[-1]["error"] == "default_runtime_cap_exceeded"
        assert any("default runtime cap exceeded" in d for s, d in logs if s == "event")
        assert any("no wallclock budget set; applying default cap" in d
                   for s, d in logs if s == "event")

    asyncio.run(go())


def test_run_job_explicit_budget_keeps_wallclock_error():
    """An explicit budget that fires still reports wallclock_exceeded (unchanged)."""
    async def go():
        w, events, logs = _mk_runjob_worker({})
        await asyncio.wait_for(
            w.run_job({"id": "jt2", "spec": {
                "kind": "command", "command": "sleep 30",
                "budget": {"max_wallclock_sec": 0.5}}}),
            timeout=15.0)
        await w.close()
        terminal = [e for e in events if e.get("type") in ("succeeded", "failed")]
        assert terminal and terminal[-1]["type"] == "failed"
        assert terminal[-1]["error"] == "wallclock_exceeded"

    asyncio.run(go())


# ---------- [R3] lease reconciliation on the worker ----------


async def _start_sleeper(w, jid, seconds=30):
    """Drive the real run_job until its process is registered in _active."""
    task = asyncio.create_task(
        w.run_job({"id": jid, "spec": {"kind": "command",
                                       "command": f"sleep {seconds}"}}))
    for _ in range(200):
        if jid in w._active:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"{jid} never became active")


def test_reconcile_owned_kills_orphan_spares_fresh_and_owned():
    """An active job the server no longer owns is killed once past the grace;
    a fresh (just-leased) job and a still-owned old job are spared."""
    async def go():
        w, events, _logs = _mk_runjob_worker({})
        t_orphan = await _start_sleeper(w, "orphan")
        t_owned = await _start_sleeper(w, "still-owned")
        t_fresh = await _start_sleeper(w, "fresh")
        # Age the orphan and the owned entry past the grace; "fresh" stays new.
        from roost.worker import LEASE_LOST_GRACE
        w._active["orphan"]["since"] -= LEASE_LOST_GRACE + 10
        w._active["still-owned"]["since"] -= LEASE_LOST_GRACE + 10
        await w._reconcile_owned({"still-owned"})
        assert w._active["orphan"]["cancelled"] == "lease_lost"
        assert w._active["still-owned"]["cancelled"] is None
        assert w._active["fresh"]["cancelled"] is None
        # The orphan's run_job unwinds with NO terminal event (server moved on).
        await asyncio.wait_for(t_orphan, timeout=10.0)
        orphan_terminals = [e for e in events
                            if e.get("type") in ("succeeded", "failed")]
        assert orphan_terminals == []
        # Clean up the two live jobs.
        await w._kill_active_job("still-owned", "cancelled")
        await w._kill_active_job("fresh", "cancelled")
        await asyncio.wait_for(asyncio.gather(t_owned, t_fresh), timeout=10.0)
        await w.close()

    asyncio.run(go())


def test_reap_stale_attempt_unwinds_before_new_attempt():
    """A re-leased job kills the stale local attempt and waits for it to fully
    unwind (entries popped) before the new attempt may start."""
    async def go():
        w, events, _logs = _mk_runjob_worker({})
        task = await _start_sleeper(w, "j1")
        w._job_tasks["j1"] = task  # what _spawn_job would have recorded
        await asyncio.wait_for(w._reap_stale_attempt("j1"), timeout=10.0)
        assert "j1" not in w._active  # old attempt fully unwound
        assert task.done()
        terminals = [e for e in events if e.get("type") in ("succeeded", "failed")]
        assert terminals == []  # torn down silently — no stale terminal report
        # And a no-op when nothing is running:
        await w._reap_stale_attempt("j1")
        await w.close()

    asyncio.run(go())


def test_run_job_quick_job_unaffected_by_default_cap():
    """A fast job under the default cap succeeds normally."""
    async def go():
        w, events, _logs = _mk_runjob_worker({})
        await asyncio.wait_for(
            w.run_job({"id": "jt3", "spec": {"kind": "command", "command": "true"}}),
            timeout=15.0)
        await w.close()
        terminal = [e for e in events if e.get("type") in ("succeeded", "failed")]
        assert terminal and terminal[-1]["type"] == "succeeded"

    asyncio.run(go())


def test_validate_container_rejects_claude_config_dir(monkeypatch, tmp_path):
    # On shared boxes the live creds live under CLAUDE_CONFIG_DIR — block mounting it.
    ccd = tmp_path / "isolated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ccd))
    with pytest.raises(ValueError):
        _validate_container({"volumes": [f"{ccd}:/c:ro"]}, None)


# ---------- [BUG1] bounded concurrency in the worker main loop ----------
#
# These drive Worker.loop() with the heartbeat/creds tasks and poll/run mocked, so no
# real subprocess (claude/sh/docker) is ever spawned. The fake run_job records when each
# job is "running" so we can assert overlap (concurrency) and never-exceed-capacity.


def _mk_loop_worker(capacity=1):
    w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
    w._capacity = capacity
    # Neutralize the background tasks the loop spawns; we only test placement.

    async def _noop():
        await w._stop.wait()

    w.heartbeat_forever = _noop  # type: ignore[assignment]
    w.refresh_creds_forever = _noop  # type: ignore[assignment]

    async def _registered():
        return None

    w.ensure_registered = _registered  # type: ignore[assignment]
    return w


def test_loop_runs_two_jobs_concurrently_at_capacity_2():
    """capacity=2 → two overlapping fake jobs are both observed 'running' at once."""
    async def go():
        w = _mk_loop_worker(capacity=2)
        running = set()
        max_concurrent = 0
        gate = asyncio.Event()  # released once BOTH jobs are observed running
        to_hand = [{"id": "j1", "spec": {}}, {"id": "j2", "spec": {}}]

        async def fake_run_job(job):
            nonlocal max_concurrent
            running.add(job["id"])
            max_concurrent = max(max_concurrent, len(running))
            if len(running) == 2:
                gate.set()  # both overlapping → release without needing another poll
            await gate.wait()
            running.discard(job["id"])

        async def fake_poll():
            if to_hand:
                return to_hand.pop(0)
            if gate.is_set():  # both ran concurrently → done
                w.stop()
            await asyncio.sleep(0.005)
            return None

        w.run_job = fake_run_job  # type: ignore[assignment]
        w.poll_once = fake_poll  # type: ignore[assignment]
        await asyncio.wait_for(w.loop(), timeout=5.0)
        assert max_concurrent == 2
        await w.close()

    asyncio.run(go())


def test_loop_never_exceeds_capacity():
    """With capacity=2 and 5 queued jobs, no more than 2 ever run at once."""
    async def go():
        w = _mk_loop_worker(capacity=2)
        running = 0
        peak = 0
        handed = 0

        async def fake_run_job(job):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

        async def fake_poll():
            nonlocal handed
            if handed < 5:
                handed += 1
                return {"id": f"j{handed}", "spec": {}}
            if running == 0:
                w.stop()
            await asyncio.sleep(0.005)
            return None

        w.run_job = fake_run_job  # type: ignore[assignment]
        w.poll_once = fake_poll  # type: ignore[assignment]
        await asyncio.wait_for(w.loop(), timeout=5.0)
        assert peak <= 2
        await w.close()

    asyncio.run(go())


def test_loop_capacity_1_is_serial():
    """capacity=1 preserves the old one-at-a-time behavior: never two at once."""
    async def go():
        w = _mk_loop_worker(capacity=1)
        running = 0
        peak = 0
        handed = 0

        async def fake_run_job(job):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

        async def fake_poll():
            nonlocal handed
            if handed < 4:
                handed += 1
                return {"id": f"j{handed}", "spec": {}}
            if running == 0:
                w.stop()
            await asyncio.sleep(0.005)
            return None

        w.run_job = fake_run_job  # type: ignore[assignment]
        w.poll_once = fake_poll  # type: ignore[assignment]
        await asyncio.wait_for(w.loop(), timeout=5.0)
        assert peak == 1
        await w.close()

    asyncio.run(go())


def test_cancel_one_job_does_not_kill_the_other():
    """A server cancel for job X kills ONLY X's process group, not the sibling job."""
    async def go():
        w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)

        class FakeProc:
            def __init__(self):
                self.returncode = None
                self.pid = -1
                self.killed = False

        p1, p2 = FakeProc(), FakeProc()
        w._active = {
            "jX": {"process": p1, "is_docker": False, "cancelled": None},
            "jY": {"process": p2, "is_docker": False, "cancelled": None},
        }
        killed = []
        # os.killpg would fail on pid -1; record the intent and mark the proc dead.
        import roost.worker as wm

        def fake_killpg(pgid, sig):
            killed.append(pgid)

        def fake_getpgid(pid):
            # map pid -> a sentinel so we can tell which proc was targeted
            return pid

        orig_killpg, orig_getpgid = wm.os.killpg, wm.os.getpgid
        wm.os.killpg = fake_killpg  # type: ignore[assignment]
        wm.os.getpgid = fake_getpgid  # type: ignore[assignment]
        try:
            await w._kill_active_job("jX", "cancelled")
        finally:
            wm.os.killpg, wm.os.getpgid = orig_killpg, orig_getpgid
        # Only jX targeted; jY untouched and still cancellable later.
        assert killed == [p1.pid]
        assert w._active["jX"]["cancelled"] == "cancelled"
        assert w._active["jY"]["cancelled"] is None
        await w.close()

    asyncio.run(go())


def test_shutdown_cancels_all_in_flight_jobs():
    """On shutdown the loop cancels EVERY in-flight job task, not just one."""
    async def go():
        w = _mk_loop_worker(capacity=3)
        started = set()
        cancelled = set()
        handed = 0

        async def fake_run_job(job):
            started.add(job["id"])
            if len(started) == 3:
                w.stop()  # all three in flight → request shutdown
            try:
                await asyncio.Event().wait()  # runs until cancelled
            except asyncio.CancelledError:
                cancelled.add(job["id"])
                raise

        async def fake_poll():
            nonlocal handed
            if handed < 3:
                handed += 1
                return {"id": f"j{handed}", "spec": {}}
            await asyncio.sleep(0.005)
            return None

        w.run_job = fake_run_job  # type: ignore[assignment]
        w.poll_once = fake_poll  # type: ignore[assignment]
        await asyncio.wait_for(w.loop(), timeout=5.0)
        assert started == {"j1", "j2", "j3"}
        assert cancelled == {"j1", "j2", "j3"}
        assert w._job_tasks == {}
        await w.close()

    asyncio.run(go())


# ---------- [BUG2] capacity judgment must not block the heartbeat ----------


def test_slow_capacity_judgment_does_not_delay_heartbeat(monkeypatch):
    """A slow steward capacity call runs detached; the heartbeat POSTs immediately with
    the cached (fail-safe) value rather than waiting on the judgment."""
    async def go():
        w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
        judged = asyncio.Event()

        async def slow_judge():
            await asyncio.sleep(0.5)  # simulate a ~claude -p subprocess
            w._capacity = 7
            w._capacity_at = 1.0
            w._capacity_running = w._running
            judged.set()

        # Replace the real judgment with a slow stub; _judge_capacity is what the
        # background refresh awaits.
        monkeypatch.setattr(w, "_judge_capacity", slow_judge)

        posted = []

        class FakeResp:
            status_code = 200

            def json(self):
                return {}

        async def fake_post(url, json=None):
            posted.append(json["capabilities"]["load"]["capacity"])
            return FakeResp()

        monkeypatch.setattr(w.client, "post", fake_post)

        # Drive ONE heartbeat iteration's critical path: trigger the (slow) refresh,
        # then POST. The trigger must return immediately and the POST use the cached 1.
        w._maybe_spawn_capacity_refresh()
        assert w._capacity_task is not None and not w._capacity_task.done()
        from roost.worker import load_snapshot as _ls
        r = await fake_post(
            "/hb", json={"capabilities": {"load": _ls(w._running, w._capacity)}})
        assert posted[0] == 1  # cached fail-safe, judgment not yet finished
        assert not judged.is_set()
        # The detached judgment eventually lands and updates the cache.
        await asyncio.wait_for(judged.wait(), timeout=2.0)
        assert w._capacity == 7
        await w.close()

    asyncio.run(go())


# ---------- Jetson/Tegra integrated-GPU detection ----------


def test_tegra_gpu_detected_via_nv_tegra_release(monkeypatch):
    """A Jetson (nv_tegra_release stamp present) advertises an integrated GPU even
    though the discrete-GPU probe returns nothing usable."""
    import roost.worker as wm

    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])  # discrete probe yields nothing
    monkeypatch.setattr(wm.os.path, "exists",
                        lambda p: p == "/etc/nv_tegra_release")
    monkeypatch.setattr(wm, "_device_tree_model",
                        lambda: "NVIDIA Jetson AGX Orin Developer Kit")
    monkeypatch.setattr(wm, "_detect_ram_gb", lambda: 64.0)
    monkeypatch.setattr(wm, "_detect_docker", lambda: {})
    monkeypatch.setattr(wm.shutil, "which", lambda _n: None)

    caps = detect_capabilities(self_test=False)
    assert caps.get("gpu_count") == 1
    assert caps.get("tegra") is True
    assert caps.get("gpu_vram_gb") and caps["gpu_vram_gb"] > 0
    assert "Orin" in caps["gpu"][0]


def test_tegra_gpu_detected_via_device_tree_model(monkeypatch):
    import roost.worker as wm

    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])
    monkeypatch.setattr(wm.os.path, "exists", lambda p: False)  # no L4T stamp
    monkeypatch.setattr(wm, "_device_tree_model", lambda: "NVIDIA Jetson Orin Nano")
    monkeypatch.setattr(wm, "_detect_ram_gb", lambda: 8.0)
    gpus = wm._detect_tegra_gpu()
    assert len(gpus) == 1
    assert gpus[0]["tegra"] is True
    assert gpus[0]["vram_gb"] >= 1.0


def test_tegra_vram_floored_for_tiny_board(monkeypatch):
    import roost.worker as wm

    monkeypatch.setattr(wm.os.path, "exists", lambda p: p == "/etc/nv_tegra_release")
    monkeypatch.setattr(wm, "_device_tree_model", lambda: "NVIDIA Jetson Nano")
    monkeypatch.setattr(wm, "_detect_ram_gb", lambda: 1.0)  # tiny shared RAM
    gpus = wm._detect_tegra_gpu()
    assert gpus and gpus[0]["vram_gb"] >= 1.0


def test_no_tegra_on_ordinary_host(monkeypatch):
    """A normal Linux box (no Tegra signals) does not falsely advertise a GPU."""
    import roost.worker as wm

    monkeypatch.setattr(wm.os.path, "exists", lambda p: False)
    monkeypatch.setattr(wm, "_device_tree_model", lambda: None)
    monkeypatch.setattr(wm, "_find_nvidia_smi", lambda: None)
    assert wm._detect_tegra_gpu() == []


def test_discrete_gpu_unchanged_when_present(monkeypatch):
    """When the standard discrete-GPU probe finds a GPU, the Tegra fallback is NOT
    consulted and no `tegra` marker is set."""
    import roost.worker as wm

    monkeypatch.setattr(wm, "_detect_gpus",
                        lambda: [{"name": "NVIDIA RTX 4090", "vram_gb": 24.0, "driver": "550"}])
    called = {"tegra": False}

    def _tegra():
        called["tegra"] = True
        return [{"name": "should-not-be-used", "vram_gb": 99}]

    monkeypatch.setattr(wm, "_detect_tegra_gpu", _tegra)
    monkeypatch.setattr(wm, "_detect_docker", lambda: {})
    monkeypatch.setattr(wm.shutil, "which", lambda _n: None)
    caps = detect_capabilities(self_test=False)
    assert caps["gpu"] == ["NVIDIA RTX 4090"]
    assert "tegra" not in caps
    assert called["tegra"] is False


# ---------- bwrap detection capability ----------


def test_bwrap_capability_advertised_when_present(monkeypatch):
    import roost.worker as wm

    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])
    monkeypatch.setattr(wm, "_detect_tegra_gpu", lambda: [])
    monkeypatch.setattr(wm, "_detect_docker", lambda: {})
    monkeypatch.setattr(wm.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    caps = detect_capabilities(self_test=False)
    assert caps.get("bwrap") is True


def test_bwrap_capability_absent_when_missing(monkeypatch):
    import roost.worker as wm

    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])
    monkeypatch.setattr(wm, "_detect_tegra_gpu", lambda: [])
    monkeypatch.setattr(wm, "_detect_docker", lambda: {})
    monkeypatch.setattr(wm.shutil, "which", lambda _n: None)
    caps = detect_capabilities(self_test=False)
    assert "bwrap" not in caps


# ---------- bwrap sandbox argv construction ----------


def test_build_bwrap_argv_binds_and_wraps_claude(tmp_path):
    cwd = str(tmp_path)
    claude_argv = ["claude", "-p", "do the thing", "--dangerously-skip-permissions"]
    argv = build_bwrap_argv(claude_argv, cwd)
    assert argv[0] == "bwrap"
    # The claude command is wrapped after a `--` separator, intact and in order.
    assert "--" in argv
    sep = argv.index("--")
    assert argv[sep + 1:] == claude_argv
    # Whole host mounted read-only.
    assert "--ro-bind" in argv
    # cwd is bound read-write (a RW hole punched into the ro host).
    pairs = list(zip(argv, argv[1:], argv[2:]))
    assert ("--bind", cwd, cwd) in pairs
    # Network is NOT unshared (claude needs the API); host PIDs ARE hidden.
    assert "--unshare-net" not in argv
    assert "--unshare-pid" in argv
    assert "--die-with-parent" in argv


def test_build_bwrap_argv_binds_claude_config_dir(monkeypatch, tmp_path):
    ccd = tmp_path / "isolated-claude"
    ccd.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ccd))
    argv = build_bwrap_argv(["claude", "-p", "x"], str(tmp_path))
    pairs = list(zip(argv, argv[1:], argv[2:]))
    assert ("--bind", str(ccd), str(ccd)) in pairs


def _bwrap_policy_worker():
    return {"sandbox": "bwrap"}


def test_claude_argv_wrapped_in_bwrap_when_policy_enabled_and_no_sandbox_flag(monkeypatch):
    """OPT-IN: policy sandbox=bwrap + bwrap available + claude has no --sandbox →
    claude runs --dangerously-skip-permissions INSIDE a bwrap jail, even on a
    NON-trusted worker (trust_skip not set)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which",
                        lambda n: "/usr/bin/" + n if n in ("claude", "bwrap") else None)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: False)
    argv = _build_claude_argv(
        {"intent": "build it", "permissions": {"sandbox": True}}, "job1",
        worker_policy={"sandbox": "bwrap"},  # NOT trusted
        base_url=None, token=None, can_dispatch=False, tempfiles=[], cwd="/work")
    assert argv[0] == "bwrap"
    assert "--dangerously-skip-permissions" in argv
    # The wrapped claude invocation is present after the separator.
    sep = argv.index("--")
    assert argv[sep + 1] == "claude"


def test_claude_argv_not_wrapped_when_policy_off(monkeypatch):
    """Default (no bwrap policy): non-trusted worker without --sandbox support falls
    back to --permission-mode default, NOT bwrap — existing behavior unchanged."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which",
                        lambda n: "/usr/bin/" + n if n in ("claude", "bwrap") else None)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: False)
    argv = _build_claude_argv(
        {"intent": "build it", "permissions": {"sandbox": True}}, "job1",
        worker_policy={},  # policy OFF
        base_url=None, token=None, can_dispatch=False, tempfiles=[], cwd="/work")
    assert argv[0] == "claude"
    assert "bwrap" not in argv
    assert "--permission-mode" in argv


def test_claude_argv_not_wrapped_when_bwrap_missing(monkeypatch):
    """Policy enabled but bwrap not installed → no wrap (graceful)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which",
                        lambda n: "/usr/bin/claude" if n == "claude" else None)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: False)
    argv = _build_claude_argv(
        {"intent": "x", "permissions": {"sandbox": True}}, "job1",
        worker_policy={"sandbox": "bwrap"},
        base_url=None, token=None, can_dispatch=False, tempfiles=[], cwd="/work")
    assert argv[0] == "claude"
    assert "bwrap" not in argv


def test_native_sandbox_flag_preferred_over_bwrap(monkeypatch):
    """If claude DOES support --sandbox, use it directly even when bwrap policy is on."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which",
                        lambda n: "/usr/bin/" + n if n in ("claude", "bwrap") else None)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv = _build_claude_argv(
        {"intent": "x", "permissions": {"sandbox": True}}, "job1",
        worker_policy={"sandbox": "bwrap"},
        base_url=None, token=None, can_dispatch=False, tempfiles=[], cwd="/work")
    assert argv[0] == "claude"
    assert "--sandbox" in argv
    assert "bwrap" not in argv


def test_run_job_oversized_line_does_not_kill_relay():
    """[R11] One >64 KiB stdout line must not crash the relay task (pre-fix it
    raised ValueError out of stream.readline() and silently lost every
    subsequent line). The line is dropped with a loud event marker; later
    output still relays; the job still succeeds."""
    async def go():
        w, events, logs = _mk_runjob_worker({})
        await asyncio.wait_for(
            w.run_job({"id": "jt-big", "spec": {
                "kind": "command",
                "command": "python3 -c \"print('x'*70000); print('after-line')\"",
            }}),
            timeout=30.0)
        await w.close()
        terminal = [e for e in events if e.get("type") in ("succeeded", "failed")]
        assert terminal and terminal[-1]["type"] == "succeeded"
        # The oversized line was dropped with a clear marker...
        assert any("oversized output line dropped" in d
                   for s, d in logs if s == "event")
        # ...and the relay SURVIVED: the next line still arrived.
        assert any(d == "after-line" for s, d in logs if s == "stdout")
        # The 70k payload itself never made it through as one line.
        assert not any(len(d) >= 70000 for _, d in logs)

    asyncio.run(go())


def test_auto_job_crash_after_decline_marker_reported_as_failed():
    """[R24] A kind:auto triage subprocess that emits ROOST_DECLINE: in stdout
    then exits non-zero must be type='failed', not type='declined'.
    'declined' requeues on another node; a crash causing requeue = infinite retry loop."""
    from unittest.mock import patch
    from roost import triage as triage_mod
    import roost.worker as wmod

    class _FakeStream:
        def __init__(self, data: bytes):
            self._lines = iter(data.splitlines(keepends=True) + [b""])
        async def readline(self):
            return next(self._lines, b"")

    class _FakeProcess:
        returncode = 1
        def __init__(self):
            self.stdout = _FakeStream(
                (triage_mod.DECLINE_MARKER + " not the right node\n").encode())
            self.stderr = _FakeStream(b"crash\n")
        async def wait(self):
            return 1

    async def _fake_create(*a, **k):
        return _FakeProcess()

    async def go():
        w, events, logs = _mk_runjob_worker({})
        with patch("asyncio.create_subprocess_exec", side_effect=_fake_create), \
             patch.object(wmod, "build_command",
                          side_effect=lambda spec, job_id, **kw: (
                              ["/bin/true"], "/tmp", [])):
            await asyncio.wait_for(
                w.run_job({"id": "j-r24", "spec": {
                    "kind": "auto", "intent": "test", "verify": False,
                }}),
                timeout=30.0,
            )
        await w.close()
        terminal = [e for e in events if e.get("type") in
                    ("succeeded", "failed", "declined")]
        assert terminal, f"no terminal event; events={events}"
        assert terminal[-1]["type"] == "failed", (
            f"crash after decline marker must be 'failed', got {terminal[-1]['type']!r}"
        )

    asyncio.run(go())


def test_bug4_other_oserror_does_not_escape_run_job():
    """[R26] OSError subclasses beyond FileNotFoundError/PermissionError (e.g.
    BlockingIOError EAGAIN when the fd table is exhausted, OSError EMFILE) must
    not escape run_job uncaught.  Pre-fix they propagated out, leaving _running
    incremented and no terminal event posted to the control plane.

    The fix broadens the except clause to the OSError base class so any spawn
    failure posts a type='failed' event and decrements _running correctly."""
    from unittest.mock import patch
    import errno
    import roost.worker as wmod

    async def _fake_create_emfile(*a, **k):
        raise OSError(errno.EMFILE, "Too many open files")

    async def go():
        w, events, logs = _mk_runjob_worker({})
        running_before = w._running
        with patch("asyncio.create_subprocess_exec", side_effect=_fake_create_emfile), \
             patch.object(wmod, "build_command",
                          side_effect=lambda spec, job_id, **kw: (
                              ["/bin/true"], "/tmp", [])):
            # Must NOT raise — the OSError must be caught inside run_job.
            await asyncio.wait_for(
                w.run_job({"id": "j-r26", "spec": {"kind": "command", "command": "true"}}),
                timeout=10.0,
            )
        await w.close()

        # A terminal 'failed' event must have been posted.
        terminal = [e for e in events if e.get("type") in ("succeeded", "failed")]
        assert terminal, f"no terminal event posted; events={events}"
        assert terminal[-1]["type"] == "failed", (
            f"spawn OSError must produce type='failed', got {terminal[-1]['type']!r}"
        )
        # The error description must be present.
        assert "Too many open files" in terminal[-1].get("error", ""), (
            f"error field missing OS message: {terminal[-1]}"
        )
        # _running must be back to its pre-call value (decremented on failure).
        assert w._running == running_before, (
            f"_running not decremented: before={running_before} after={w._running}"
        )

    asyncio.run(go())
