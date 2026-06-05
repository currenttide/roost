"""Bare-worker (kind: auto) pre-filter tests — the cheap deterministic gate that
declines an obvious capability mismatch without spending an LLM triage call."""
from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

from roost.worker import (
    VERIFY_HEAL_TIMEOUT,
    Worker,
    _auto_prefilter,
    _budget_remaining,
    _build_docker_argv,
    _sanitize_env,
    _validate_container,
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
