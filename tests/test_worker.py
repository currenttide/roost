"""Bare-worker (kind: auto) pre-filter tests — the cheap deterministic gate that
declines an obvious capability mismatch without spending an LLM triage call."""
from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

import roost.worker as wmod_R72  # [R72] for DOCKER_TEARDOWN_TIMEOUT + seam patching
from roost.worker import (
    AUTO_DEFAULT_MODEL,
    DEFAULT_WALLCLOCK_FALLBACK_MIN,
    DEFAULT_WALLCLOCK_MIN,
    VERIFY_HEAL_TIMEOUT,
    Worker,
    _auto_prefilter,
    _budget_remaining,
    _build_auto_argv,
    _build_claude_argv,
    _build_codex_argv,
    _build_docker_argv,
    _resolve_timeout,
    _sanitize_env,
    _validate_container,
    build_bwrap_argv,
    build_command,
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


# ---------- [R67] stale done-callback must not evict a re-leased job's task ----
#
# _spawn_job's _done callback runs LATER (call_soon), after run_job's task
# finishes. _reap_stale_attempt early-returns when the old task is already
# done() WITHOUT draining that pending callback, so the loop installs the NEW
# attempt's task at the same job_id before the OLD task's _done fires. An
# unconditional pop would then evict the live NEW task; the fix is _done's
# identity guard (only the task that still owns the entry may evict it). Harm if
# regressed: capacity gate under-counts -> over-lease; orphaned task escapes
# _shutdown_jobs; next re-lease's reap can't find it -> double execution.


class _EOFStream:
    """A stdout/stderr stand-in at EOF, so run_job's relay tasks finish at once."""

    async def readline(self) -> bytes:
        return b""


class _QuickProc:
    """An already-exited process: wait() returns 0 immediately."""

    pid = 111
    returncode = 0

    def __init__(self) -> None:
        self.stdout = _EOFStream()
        self.stderr = _EOFStream()

    async def wait(self) -> int:
        return 0


class _HangingProc:
    """A still-alive process: wait() never returns, so the job keeps its slot."""

    pid = 222
    returncode = None

    def __init__(self) -> None:
        self.stdout = _EOFStream()
        self.stderr = _EOFStream()

    async def wait(self) -> int:
        await asyncio.sleep(9999)
        return 0


def test_reap_early_return_lets_stale_callback_evict_new_task_entry():
    """Promoted A1 hunt #6 repro. The OLD (finished) attempt's pending _done must
    NOT evict the NEW attempt's _job_tasks entry once it has been re-installed."""
    from unittest.mock import patch
    import roost.worker as wmod

    async def go():
        w, _events, _logs = _mk_runjob_worker({})
        w._capacity = 4
        spec = {"id": "jX", "spec": {"kind": "command", "command": "x",
                                     "verify": False}}

        # Attempt 1 gets a quick (already-exited) process so its task finishes
        # fast; attempt 2 (the re-lease) gets a HANGING process so it stays in
        # _job_tasks — making an eviction unambiguously a bug (a live task lost
        # from tracking) rather than a normal self-removal.
        proc_seq = [_QuickProc, _HangingProc]

        async def _fake_create(*_a, **_k):
            return proc_seq.pop(0)()

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_create), \
             patch.object(wmod, "build_command",
                          side_effect=lambda spec, jid, **kw: (
                              ["/bin/true"], "/tmp", [])):
            # First attempt — run_job finishes quickly.
            w._spawn_job(spec)
            old = w._job_tasks["jX"]

            # Pump the loop until the deterministic window: old task DONE, but its
            # _done callback NOT YET run (jX still maps to the OLD task) — exactly
            # the state a poll await can leave.
            window = False
            for _ in range(2000):
                await asyncio.sleep(0)
                if old.done():
                    window = w._job_tasks.get("jX") is old
                    break
            assert old.done(), "old task should have finished"
            assert window, (
                "could not reach the done-but-callback-pending window "
                "(scheduling); rerun")

            # The real loop body for a re-lease of jX, with NO await between the
            # reap's early return and _spawn_job (matches the loop at worker.py).
            await w._reap_stale_attempt("jX")   # old.done() -> early return
            w._spawn_job(spec)                  # install NEW task (hanging proc)
            new = w._job_tasks.get("jX")
            assert new is not None and new is not old

            # Let the NEW attempt genuinely start (register in _active) so it is
            # provably a LIVE, still-running task — then let the OLD task's
            # pending callback fire.
            for _ in range(200):
                if "jX" in w._active and not new.done():
                    break
                await asyncio.sleep(0)
            assert not new.done() and "jX" in w._active, (
                "new attempt should be live and running")
            for _ in range(10):
                await asyncio.sleep(0)

            present = w._job_tasks.get("jX")
            new_still_running = not new.done()
            # Tidy up before asserting so a leak can't hang asyncio shutdown.
            for t in list(w._job_tasks.values()):
                t.cancel()
            if new is not None and not new.done():
                new.cancel()
            for _ in range(20):
                await asyncio.sleep(0)

            assert present is new and new_still_running, (
                "the NEW attempt's _job_tasks entry was EVICTED by the OLD "
                f"task's stale done-callback while the NEW task was still "
                f"running (present={present!r}, new_running={new_still_running}); "
                "the worker now under-counts in-flight jobs (capacity gate "
                "over-leases) and the running task is orphaned from _job_tasks "
                "(shutdown/reap miss it -> double execution on the next re-lease)")
        await w.close()

    asyncio.run(go())


def test_done_callback_identity_guard():
    """Pins the REAL _spawn_job/_done identity-guard semantics: a stale task's
    late callback must NOT evict a newer entry, while the OWNING task's callback
    still cleans up normally (the common path must keep popping)."""
    from unittest.mock import patch
    import roost.worker as wmod

    async def go():
        w, _events, _logs = _mk_runjob_worker({})
        w._capacity = 4

        # Drive the REAL _spawn_job (-> real run_job -> real _done callback) for
        # the same job_id twice: a quick first attempt, then a hanging re-lease.
        proc_seq = [_QuickProc, _HangingProc]

        async def _fake_create(*_a, **_k):
            return proc_seq.pop(0)()

        spec = {"id": "jG", "spec": {"kind": "command", "command": "x",
                                     "verify": False}}
        with patch("asyncio.create_subprocess_exec", side_effect=_fake_create), \
             patch.object(wmod, "build_command",
                          side_effect=lambda spec, jid, **kw: (
                              ["/bin/true"], "/tmp", [])):
            w._spawn_job(spec)
            old = w._job_tasks["jG"]

            # Window: old finished but its _done not yet drained (jG -> old).
            for _ in range(2000):
                await asyncio.sleep(0)
                if old.done() and w._job_tasks.get("jG") is old:
                    break
            assert old.done() and w._job_tasks.get("jG") is old, (
                "could not reach done-but-callback-pending window; rerun")

            # Install the NEW (owning) attempt at the same key.
            w._spawn_job(spec)
            new = w._job_tasks.get("jG")
            assert new is not None and new is not old

            # Drain the OLD task's late callback: with the identity guard it is a
            # no-op (jG now points at NEW), so the newer entry must SURVIVE.
            for _ in range(50):
                await asyncio.sleep(0)
            assert w._job_tasks.get("jG") is new, (
                "stale callback evicted the newer entry; identity guard broken")

            # The OWNING (NEW) task's callback still cleans up normally: cancel it
            # and confirm its own _done pops the entry (the common path).
            new.cancel()
            for _ in range(50):
                await asyncio.sleep(0)
            assert "jG" not in w._job_tasks, (
                "owning task's callback failed to pop its own entry")

            for t in list(w._job_tasks.values()):
                t.cancel()
            for _ in range(20):
                await asyncio.sleep(0)
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


# ---------- [R41] GPU detection: no-GPU vs detection-FAILED ----------
#
# The GPU probe (_detect_gpus) returns [] both when nvidia-smi is absent (genuinely
# no GPU) and when nvidia-smi is present but errors (driver hiccup / timeout / GPU
# off the bus). These tests pin the four distinct paths so a BROKEN node advertises
# `gpu_detection: "failed"` (additive) while a BARE node advertises no gpu* keys —
# and so placement still skips both for GPU jobs.


def _no_gpu_env(monkeypatch):
    """Common stubs for detect_capabilities GPU-branch tests: no Tegra, no docker,
    nothing else on PATH. Caller controls _find_nvidia_smi / subprocess.run."""
    import roost.worker as wm
    monkeypatch.setattr(wm, "_detect_tegra_gpu", lambda: [])
    monkeypatch.setattr(wm, "_detect_docker", lambda: {})
    monkeypatch.setattr(wm.shutil, "which", lambda _n: None)


def test_gpu_probe_success_advertises_gpu_no_failed_marker(monkeypatch):
    """Path 1 — probe SUCCEEDS: real gpu* keys, and no `gpu_detection` marker."""
    import roost.worker as wm
    _no_gpu_env(monkeypatch)
    monkeypatch.setattr(
        wm, "_detect_gpus",
        lambda: [{"name": "NVIDIA RTX 4090", "vram_gb": 24.0, "driver": "550"}])
    caps = detect_capabilities(self_test=False)
    assert caps.get("gpu_vram_gb") == 24.0
    assert "gpu_detection" not in caps  # success never marks a failure


def test_gpu_absent_no_gpu_keys_and_no_failed_marker(monkeypatch):
    """Path 2 — ABSENCE: nvidia-smi not installed → no gpu* keys, no marker."""
    import roost.worker as wm
    _no_gpu_env(monkeypatch)
    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])
    monkeypatch.setattr(wm, "_find_nvidia_smi", lambda: None)  # no probe tool at all
    caps = detect_capabilities(self_test=False)
    assert "gpu_vram_gb" not in caps
    assert "gpu_count" not in caps
    assert "gpu_detection" not in caps  # genuinely bare, NOT failed


def test_gpu_probe_failed_exception_advertises_failed(monkeypatch):
    """Path 3 — FAILURE via exception: nvidia-smi present but the subprocess raises
    (e.g. OSError / a driver that won't load). Advertise gpu_detection=failed."""
    import roost.worker as wm
    _no_gpu_env(monkeypatch)
    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])
    monkeypatch.setattr(wm, "_find_nvidia_smi", lambda: "/usr/bin/nvidia-smi")

    def _boom(*a, **k):
        raise OSError("nvidia-smi: cannot execute")
    monkeypatch.setattr(wm.subprocess, "run", _boom)

    caps = detect_capabilities(self_test=False)
    assert caps.get("gpu_detection") == "failed"
    # Crucially still NO usable GPU advertised — placement must skip it like a bare node.
    assert "gpu_vram_gb" not in caps
    assert "gpu_count" not in caps


def test_gpu_probe_failed_nonzero_exit_advertises_failed(monkeypatch):
    """Path 4 — FAILURE via nonzero exit: nvidia-smi present but exits nonzero
    (CalledProcessError — the canonical "driver loaded but GPU unhealthy" case)."""
    import roost.worker as wm
    _no_gpu_env(monkeypatch)
    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])
    monkeypatch.setattr(wm, "_find_nvidia_smi", lambda: "/usr/bin/nvidia-smi")

    def _nonzero(*a, **k):
        raise wm.subprocess.CalledProcessError(
            returncode=255, cmd="nvidia-smi",
            stderr="Failed to initialize NVML: Driver/library version mismatch")
    monkeypatch.setattr(wm.subprocess, "run", _nonzero)

    caps = detect_capabilities(self_test=False)
    assert caps.get("gpu_detection") == "failed"
    assert "gpu_vram_gb" not in caps


def test_gpu_probe_timeout_advertises_failed(monkeypatch):
    """A hung GPU/driver makes nvidia-smi time out → still classified as failed."""
    import roost.worker as wm
    _no_gpu_env(monkeypatch)
    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])
    monkeypatch.setattr(wm, "_find_nvidia_smi", lambda: "/usr/bin/nvidia-smi")

    def _timeout(*a, **k):
        raise wm.subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5.0)
    monkeypatch.setattr(wm.subprocess, "run", _timeout)

    assert wm._gpu_probe_failed() == "nvidia-smi timed out"
    assert detect_capabilities(self_test=False).get("gpu_detection") == "failed"


def test_gpu_probe_failed_emits_loud_log_line(monkeypatch, capsys):
    """The failure path emits a structured, greppable worker log line so the broken
    node is visible in the worker's own logs (operability), not just over the API."""
    import roost.worker as wm
    _no_gpu_env(monkeypatch)
    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])
    monkeypatch.setattr(wm, "_find_nvidia_smi", lambda: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(wm.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    detect_capabilities(self_test=False)
    out = capsys.readouterr().out
    assert "GPU_DETECTION_FAILED" in out
    assert "gpu_detection=failed" in out


def test_tegra_board_not_marked_detection_failed(monkeypatch):
    """A Jetson (integrated nvgpu) is handled by the Tegra fallback BEFORE the
    failure branch, so it advertises a real GPU and is never mislabeled as failed."""
    import roost.worker as wm
    monkeypatch.setattr(wm, "_detect_gpus", lambda: [])  # discrete probe empty
    monkeypatch.setattr(
        wm, "_detect_tegra_gpu",
        lambda: [{"name": "Orin (nvgpu)", "vram_gb": 16.0, "driver": None,
                  "tegra": True, "integrated": True}])
    monkeypatch.setattr(wm, "_detect_docker", lambda: {})
    monkeypatch.setattr(wm.shutil, "which", lambda _n: None)
    caps = detect_capabilities(self_test=False)
    assert caps.get("tegra") is True
    assert caps.get("gpu_vram_gb") == 16.0
    assert "gpu_detection" not in caps


def test_gpu_probe_failed_succeeds_with_no_rows_is_absence(monkeypatch):
    """_gpu_probe_failed: nvidia-smi runs cleanly but returns no rows → NOT a
    failure (e.g. an integrated board with no discrete GPU) → returns None."""
    import roost.worker as wm
    monkeypatch.setattr(wm, "_find_nvidia_smi", lambda: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(wm.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "", "stderr": ""})())
    assert wm._gpu_probe_failed() is None


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


def test_oneshot_agent_keeps_bwrap_argv_intact_with_system_prompt(monkeypatch):
    """[R30] _oneshot_agent must splice --append-system-prompt after `claude -p
    <intent>`, NOT at a fixed argv[:3]. Under worker policy sandbox=bwrap with a
    claude lacking --sandbox, _build_claude_argv returns a bwrap-wrapped argv
    (`bwrap --ro-bind / / ... -- claude -p <intent> ...`); a fixed index 3 would
    splice the flag into the middle of bwrap's own options and corrupt the jail."""
    import roost.worker as wm

    w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
    w.policy = {"sandbox": "bwrap"}
    # Force the bwrap branch: pretend the installed claude has no --sandbox flag.
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: False)
    monkeypatch.setattr(wm.shutil, "which", lambda name: f"/usr/bin/{name}")

    captured: dict = {}

    async def _capture(*argv, **kwargs):
        captured["argv"] = list(argv)
        # Bail out of _oneshot_agent cleanly: it catches FileNotFoundError around
        # the spawn and returns early.
        raise FileNotFoundError("stub — argv captured")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capture)

    asyncio.run(
        w._oneshot_agent(
            "job-A", "do the thing", system_prompt="SYS PROMPT", label="verify",
            timeout_s=1.0,
        )
    )
    asyncio.run(w.close())

    argv = captured["argv"]
    assert argv and argv[0] == "bwrap", f"expected a bwrap-wrapped argv, got {argv!r}"
    # bwrap's own leading options must be untouched: `--ro-bind / /` intact.
    assert argv[1:4] == ["--ro-bind", "/", "/"], (
        "bwrap options corrupted — --append-system-prompt was spliced into the "
        f"middle of bwrap's flags: {argv[1:6]!r}"
    )
    # The flag + value must sit AFTER `claude -p <intent>`, inside the jailed
    # command (after bwrap's `--`), so claude actually receives the system prompt.
    sep = argv.index("--")
    ci = argv.index("claude")
    assert ci > sep, "claude must appear after bwrap's `--` separator"
    assert argv[ci:ci + 3] == ["claude", "-p", "do the thing"]
    assert argv[ci + 3:ci + 5] == ["--append-system-prompt", "SYS PROMPT"], (
        "--append-system-prompt must be inserted right after `claude -p <intent>`, "
        f"not elsewhere: {argv[ci:ci + 6]!r}"
    )


def test_oneshot_agent_cancels_relay_tasks_on_cancellation(monkeypatch):
    """[R31] When _oneshot_agent is cancelled while parked in
    `asyncio.wait_for(proc.wait(), ...)`, it must cancel its two stdout/stderr
    relay tasks in a `finally`. Pre-fix the gather(t1, t2) lived in the `try`, so
    on CancelledError the relays were left pending forever (unowned tasks →
    'task was destroyed but it is pending' warnings + cross-test interference)."""

    class _NeverReader:
        """Relay stand-in: parks in readline() forever (never yields EOF), so the
        only thing that can finish a relay task is cancellation."""

        async def readline(self) -> bytes:
            await asyncio.sleep(3600)
            return b""

    class _HangingProc:
        """asyncio.subprocess stand-in whose wait() never returns, so the parent
        can be cancelled precisely while inside `asyncio.wait_for(proc.wait())`.
        wait() sets `parked` once suspended so the driver can cancel at that point."""

        def __init__(self, parked: asyncio.Event) -> None:
            self.pid = 424242
            self.returncode = None
            self.stdout = _NeverReader()
            self.stderr = _NeverReader()
            self._parked = parked

        async def wait(self) -> int:
            self._parked.set()
            await asyncio.sleep(3600)
            return 0

    w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
    w.policy = {}  # plain (non-bwrap) path — argv building is irrelevant here
    monkeypatch.setattr("roost.worker.shutil.which", lambda name: f"/usr/bin/{name}")

    # Capture the two relay tasks _oneshot_agent creates (the `rel` coroutines).
    relays: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def _tracking_create_task(coro, *a, **k):
        t = real_create_task(coro, *a, **k)
        if getattr(coro, "__name__", None) == "rel":
            relays.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _tracking_create_task)

    async def _drive() -> int:
        parked = asyncio.Event()

        async def _fake_spawn(*argv, **kwargs):
            return _HangingProc(parked)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

        task = real_create_task(
            w._oneshot_agent("job-B", "do the thing", label="verify", timeout_s=3600.0)
        )
        # Wait until _oneshot_agent has spawned the proc, created both relay tasks,
        # and is genuinely suspended inside `asyncio.wait_for(proc.wait(), ...)`.
        await asyncio.wait_for(parked.wait(), timeout=5.0)

        # Cancel the parent the way a server cancel / job teardown does.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)  # let the parent's `finally` cleanup run

        # Inspect WHILE the loop is still alive — asyncio.run() cancels leftover
        # tasks at shutdown, which would mask the leak after the loop closed.
        leaked = [t for t in relays if not t.done()]
        for t in leaked:  # tidy up so asyncio.run shutdown stays quiet
            t.cancel()
        return len(leaked)

    leaked_count = asyncio.run(_drive())
    asyncio.run(w.close())

    assert len(relays) == 2, f"expected 2 relay tasks, tracked {len(relays)}"
    assert leaked_count == 0, (
        f"{leaked_count} relay task(s) left PENDING after _oneshot_agent was "
        "cancelled — the relays must be cancelled in a `finally` on CancelledError"
    )


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


def test_bug1_running_and_active_not_leaked_on_cancellation():
    """[R25] When run_job is cancelled via task.cancel() while the subprocess is
    running, _running must be decremented and _active must be cleared.
    Before the fix a CancelledError propagated out leaving both counters wrong."""
    from unittest.mock import patch
    import roost.worker as wmod

    # A fake process whose streams immediately signal EOF (so relay tasks finish
    # quickly), but whose wait() hangs — the target await for task.cancel().
    class _HangingProcess:
        returncode = None
        def __init__(self):
            self.stdout = _EOFStream()
            self.stderr = _EOFStream()
        async def wait(self):
            await asyncio.sleep(9999)  # hangs until outer task is cancelled
            return 0

    class _EOFStream:
        """Returns EOF immediately so relay tasks exit without blocking gather."""
        async def readline(self):
            return b""

    async def _fake_create(*a, **k):
        return _HangingProcess()

    async def go():
        w, events, _logs = _mk_runjob_worker({})
        task = asyncio.create_task(
            _run_job_with_patches(w, "j-r25", _fake_create, wmod)
        )
        # Let the coroutine start and reach process.wait() (relay tasks finish fast
        # because EOF streams, so gather in finally completes immediately on cancel).
        await asyncio.sleep(0.05)
        # Cancel the task to simulate worker shutdown / _spawn_job task.cancel()
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        assert w._running == 0, (
            f"_running leaked after cancellation: {w._running}")
        assert "j-r25" not in w._active, (
            f"_active leaked after cancellation: {w._active}")

    async def _run_job_with_patches(w, job_id, fake_create, wmod):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_create), \
             patch.object(wmod, "build_command",
                          side_effect=lambda spec, jid, **kw: (
                              ["/bin/true"], "/tmp", [])):
            await w.run_job({"id": job_id, "spec": {"intent": "hang forever", "verify": False}})


# ---------- [R43] Claude-creds refresh vs. lease/heartbeat auth ----------
#
# Hypothesis under test (pre-loop survey, never verified): the worker's periodic
# CREDENTIAL REFRESH may race the LEASE lifecycle — e.g. a refresh mid-lease
# invalidating the credential the CP authenticates the worker with, a refresh
# window where heartbeats 401, or a refresh racing a "rotating token" so the
# lease expires → spurious requeue / double-execution.
#
# These tests drive a REAL Worker (its real httpx client wired to a real
# in-process control plane via ASGITransport, so the genuine auth dependency and
# lease-renewal code run) and try to MAKE that race happen. They pass because the
# two "credentials" are disjoint:
#   * worker-plane bearer (self.token) — minted once at /enroll, immutable for the
#     worker's lifetime (worker.py:1145, 1181-1188); authenticates lease/heartbeat
#     via _worker_by_cred_hash (server.py:2396). NEVER rotated by the worker.
#   * Claude OAuth creds (credentials_json) — what `claude` subprocesses read;
#     refresh_creds_forever re-pulls them every 20 min and writes ONLY the local
#     creds FILE (worker.py:1531-1562). GET /claude-creds is a pure read of the
#     host file (server.py:2576) and writes nothing to the DB, so it can't touch
#     workers.cred_hash.
# The refresh therefore shares no mutable state with the lease auth; it cannot
# invalidate it. The tests stand as regression guards on that invariant.


def _r43_client_for_app(app, cred=None):
    """An async httpx client bound to the in-process app (so the genuine auth
    dependency + lease-renewal code run on every call). With `cred`, it carries
    that worker-plane bearer; otherwise the admin token."""
    import httpx

    bearer = cred or "adm"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cp",
        headers={"Authorization": f"Bearer {bearer}"},
    )


async def _r43_enroll(app):
    """Mint an enroll token + enroll a worker via real HTTP. Returns (wid, cred)."""
    admin = _r43_client_for_app(app)
    try:
        tok = (await admin.post("/enroll-tokens",
                                json={"label": "r43"})).json()["token"]
        body = (await admin.post(
            "/enroll",
            json={"token": tok, "name": "w-r43",
                  "capabilities": {"tools": ["claude"]}},
            headers={"Authorization": ""})).json()
        return body["worker_id"], body["credential"]
    finally:
        await admin.aclose()


async def _r43_worker_wired_to_app(app, cred, worker_id):
    """A real Worker whose httpx client speaks to the in-process app."""
    import httpx

    w = Worker("http://cp", cred, worker_id, self_test=False, enrolled=True)
    await w.client.aclose()  # drop the default transport
    w.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cp",
        headers={"Authorization": f"Bearer {cred}"},
    )
    return w


def test_r43_creds_refresh_does_not_break_lease_auth(tmp_path, monkeypatch):
    """Adversarial: interleave the REAL creds refresh (host OAuth creds rotating
    every call) with REAL heartbeats while the worker holds a lease. The
    hypothesis predicts a window where the heartbeat 401s and the lease lapses.
    It never does: every heartbeat stays 200 and renews the lease, because the
    refresh touches only the local creds file, never the worker-plane bearer."""
    import time as _time

    from roost import server

    async def go():
        db = tmp_path / "roost.db"
        # The CP serves whatever the operator's host creds currently are; flip
        # `state["creds"]` to simulate an OAuth rotation on the host.
        state = {"creds": '{"claudeAiOauth":{"accessToken":"v0"}}'}
        monkeypatch.setattr(server, "_read_host_claude_creds",
                            lambda: state["creds"])

        app = server.create_app(db_path=db, token="adm", run_sweeper=False)
        wid, cred = await _r43_enroll(app)
        w = await _r43_worker_wired_to_app(app, cred, wid)
        try:
            # Point the worker's claude-creds file at a temp path so the real
            # write path runs without touching ~/.claude.
            creds_file = tmp_path / "isolated" / ".credentials.json"
            monkeypatch.setattr(w, "_claude_creds_path", lambda: creds_file)

            # Submit + lease a job so the worker holds a live lease.
            jid = server._insert_job(db, {"command": "true"})["id"]
            job = await w.poll_once()
            assert job is not None and job["id"] == jid
            assert server._get_job(db, jid)["state"] == "assigned"
            lease0 = server._get_job(db, jid)["lease_expires_at"]
            assert lease0 is not None

            # Hammer: rotate the host OAuth creds and run the REAL refresh,
            # interleaved with REAL heartbeats. If a refresh ever invalidated the
            # worker-plane credential, the next heartbeat would 404/401 and
            # _worker_by_cred_hash would stop matching.
            rotations = [
                '{"claudeAiOauth":{"accessToken":"v1"}}',
                '{"claudeAiOauth":{"accessToken":"v2"}}',
                '{"claudeAiOauth":{"accessToken":"v3"}}',
            ]
            last_lease = lease0
            for i, new_creds in enumerate(rotations):
                state["creds"] = new_creds
                await w._refresh_claude_creds()          # the real refresh
                # The local creds file tracks the rotation (proves refresh ran).
                assert creds_file.read_text() == new_creds
                # The worker-plane credential the CP knows is UNCHANGED.
                assert server._worker_by_cred_hash(
                    db, server._hash_cred(cred))["id"] == wid
                # A real heartbeat through the real auth dependency: still 200,
                # still ours, lease still renewed (monotonic non-decreasing).
                before = _time.time()
                r = await w.client.post(f"/workers/{wid}/heartbeat",
                                        json={"capabilities": w.capabilities})
                assert r.status_code == 200, (i, r.status_code, r.text)
                assert jid in (r.json().get("owned") or [])
                lease = server._get_job(db, jid)["lease_expires_at"]
                assert lease is not None and lease >= last_lease
                assert lease >= before  # heartbeat genuinely renewed it
                last_lease = lease

            # The lease never expired across the whole refresh storm.
            assert server._get_job(db, jid)["state"] in ("assigned", "running")

            # Strongest form of the hypothesis: a refresh truly CONCURRENT with a
            # heartbeat on the SAME httpx client. Rule out any client/connection-
            # level interference ("refresh window"). Both must succeed.
            state["creds"] = '{"claudeAiOauth":{"accessToken":"v9"}}'

            async def _hb():
                return await w.client.post(
                    f"/workers/{wid}/heartbeat",
                    json={"capabilities": w.capabilities})

            _, hb_resp = await asyncio.gather(w._refresh_claude_creds(), _hb())
            assert hb_resp.status_code == 200
            assert jid in (hb_resp.json().get("owned") or [])
            assert creds_file.read_text() == '{"claudeAiOauth":{"accessToken":"v9"}}'
            assert server._get_job(db, jid)["state"] in ("assigned", "running")
        finally:
            await w.close()

    asyncio.run(go())


def test_r43_refresh_disabled_on_cp_leaves_lease_auth_intact(tmp_path):
    """If the CP has creds provisioning OFF, GET /claude-creds 404s — the refresh
    is a no-op (worker.py:1539-1540) and must NOT disturb the worker-plane
    bearer. The hypothesised 'refresh window where heartbeats 401' cannot occur
    even when the refresh fails outright."""
    from roost import server

    async def go():
        db = tmp_path / "roost.db"
        app = server.create_app(db_path=db, token="adm", run_sweeper=False,
                                provision_claude_auth=False)
        wid, cred = await _r43_enroll(app)
        w = await _r43_worker_wired_to_app(app, cred, wid)
        try:
            jid = server._insert_job(db, {"command": "true"})["id"]
            assert (await w.poll_once())["id"] == jid

            # Provisioning is off → /claude-creds 404 → refresh returns early.
            r = await w.client.get("/claude-creds")
            assert r.status_code == 404
            await w._refresh_claude_creds()  # real no-op refresh

            # Heartbeat still authenticates and renews the lease.
            r = await w.client.post(f"/workers/{wid}/heartbeat", json={})
            assert r.status_code == 200
            assert jid in (r.json().get("owned") or [])
            assert server._get_job(db, jid)["state"] in ("assigned", "running")
        finally:
            await w.close()

    asyncio.run(go())

    asyncio.run(go())


# ---------- [R51] verify / self-heal phase, end-to-end through run_job ----------
#
# These drive the REAL run_job verify/self-heal phase with stubbed subprocesses.
# The seam: run_job's executor argv comes from build_command, and the verify /
# self-heal subprocesses' argv come from _build_claude_argv (inside _oneshot_agent).
# We patch both to emit a marker argv we can recognise, then patch
# asyncio.create_subprocess_exec with a factory that dispatches on that marker —
# so the REAL _run_verifier, _oneshot_agent, the self-heal loop, _phase_progress
# budget plumbing, and verify.parse_verdict/render_user/render_fix all execute.
# Nothing real (claude/sh) is ever spawned.

import json  # noqa: E402
from unittest.mock import patch as _patch  # noqa: E402
import roost.worker as _wm  # noqa: E402
from roost.verify import VERIFY_MARKER  # noqa: E402

_EXEC_MARK = "ROOST_TEST_EXEC"
_ONESHOT_MARK = "ROOST_TEST_ONESHOT"


class _ScriptedStream:
    """Yields the given byte lines, then EOF — an asyncio.StreamReader stand-in."""
    def __init__(self, data: bytes):
        self._lines = iter(data.splitlines(keepends=True) + [b""])

    async def readline(self):
        return next(self._lines, b"")


class _ScriptedProc:
    """A fake subprocess: emits scripted stdout, exits with `returncode`.

    If `hang` is set, wait() blocks until either (a) the caller's asyncio.wait_for
    cap cancels it (the verify-subprocess TIMEOUT path: _oneshot_agent then calls
    os.killpg — ProcessLookupError on this fake pid, swallowed — and reaps with a
    second wait() that returns -9 like a SIGKILLed process), or (b) kill() is
    called (the server-CANCEL teardown path: _kill_aux_procs SIGKILLs the aux proc,
    which here trips _killed so wait() returns promptly instead of parking for the
    full cap). pid is real-ish so the killpg branch runs."""
    def __init__(self, *, stdout: bytes = b"", returncode: int = 0, hang: bool = False):
        self.stdout = _ScriptedStream(stdout)
        self.stderr = _ScriptedStream(b"")
        self.returncode = returncode
        self.pid = 424242
        self._hang = hang
        self._killed = asyncio.Event()

    def kill(self):
        # os.killpg on the fake pid raises ProcessLookupError → _kill_aux_procs
        # falls back to proc.kill(); make that actually release a hanging wait().
        self.returncode = -9
        self._killed.set()

    async def wait(self):
        if self._hang and not self._killed.is_set():
            try:
                await self._killed.wait()  # released by kill(); else cancelled by the cap
            except asyncio.CancelledError:
                # Timeout path: the cap cancelled this wait(); mark dead so the
                # follow-up reap wait() (post-SIGKILL) returns -9 immediately.
                self.returncode = -9
                self._killed.set()
                raise
        return self.returncode


def _result_line(text: str) -> bytes:
    """A claude stream-json `result` line carrying `text` (what the executor /
    verifier 'said'), so the relay captures it into result_text / the verdict."""
    return (json.dumps({"type": "result", "result": text}) + "\n").encode()


def _verify_e2e_patches(*, executor, verifier):
    """Context managers that wire run_job's three subprocess seams to fakes.

    `executor`/`verifier` are callables returning a _ScriptedProc. `verifier` is
    given the spawn index (0-based count of verify/heal subprocesses so far) so a
    test can vary verdicts across self-heal rounds; its argv tells fix from verify.
    """
    calls = {"verify_spawns": 0, "labels": []}

    def _fake_build_command(spec, job_id, **kw):
        # Executor argv: a recognisable no-op; build_command normally returns
        # (argv, cwd, tempfiles).
        return (["/bin/true", _EXEC_MARK, job_id], "/tmp", [])

    def _fake_build_claude_argv(spec, job_id, **kw):
        # _oneshot_agent passes job_id as f"{parent}-{label}" (…-verify / …-fix1).
        return ["/bin/true", _ONESHOT_MARK, job_id]

    async def _fake_create(*argv, **kwargs):
        flat = [str(a) for a in argv]
        if _EXEC_MARK in flat:
            return executor()
        if _ONESHOT_MARK in flat:
            # _build_claude_argv put the f"{parent}-{label}" job_id right after the
            # marker; _oneshot_agent then splices --append-system-prompt in after
            # index 3, so we read the element next to the marker (not flat[-1]).
            label = flat[flat.index(_ONESHOT_MARK) + 1]
            calls["labels"].append(label)
            idx = calls["verify_spawns"]
            calls["verify_spawns"] += 1
            return verifier(idx, label)
        raise AssertionError(f"unexpected spawn argv: {flat}")

    cms = (
        _patch.object(_wm, "build_command", side_effect=_fake_build_command),
        _patch.object(_wm, "_build_claude_argv", side_effect=_fake_build_claude_argv),
        _patch("asyncio.create_subprocess_exec", side_effect=_fake_create),
    )
    return cms, calls


def _run_verify_job(spec_extra=None, *, executor, verifier, policy=None, job_id="jv"):
    """Drive run_job for a verifying agent job to completion; return (events, logs,
    calls). The spec is kind:claude with verify:true so the trust loop engages."""
    async def go():
        w, events, logs = _mk_runjob_worker(policy or {})
        spec = {"kind": "claude", "intent": "do the thing", "task": "make X true",
                "verify": True}
        spec.update(spec_extra or {})
        cms, calls = _verify_e2e_patches(executor=executor, verifier=verifier)
        with cms[0], cms[1], cms[2]:
            await asyncio.wait_for(
                w.run_job({"id": job_id, "spec": spec}), timeout=30.0)
        await w.close()
        return events, logs, calls

    return asyncio.run(go())


def _terminal(events):
    t = [e for e in events if e.get("type") in ("succeeded", "failed", "declined")]
    assert t, f"no terminal event; events={events}"
    return t[-1]


def test_r51_verify_pass_records_verified_success():
    """Executor exits 0, the independent verifier returns PASS → terminal
    'succeeded' with verified=True and the verifier's evidence recorded."""
    def executor():
        return _ScriptedProc(stdout=_result_line("I made X true."), returncode=0)

    def verifier(idx, label):
        # Only the verify subprocess should run (no self-heal on a pass).
        assert "verify" in label, f"unexpected aux subprocess: {label}"
        return _ScriptedProc(
            stdout=_result_line(f"Checked X; it holds. {VERIFY_MARKER} PASS — X is true"),
            returncode=0)

    events, logs, calls = _run_verify_job(executor=executor, verifier=verifier)
    term = _terminal(events)
    assert term["type"] == "succeeded", term
    assert term["result"]["verified"] is True
    assert term["result"]["self_heal_attempts"] == 0
    assert "X is true" in term["result"]["evidence"]
    assert term["result"]["output"] == "I made X true."
    # Exactly one verify subprocess, no fix.
    assert calls["verify_spawns"] == 1
    assert all("fix" not in lbl for lbl in calls["labels"])
    # The PASS verdict was logged.
    assert any("PASS" in d for s, d in logs if s == "event")


def test_r51_self_heal_succeeds_after_initial_fail():
    """Verifier FAILs, one self-heal fix runs, re-verify PASSes → 'succeeded'
    with verified=True and self_heal_attempts=1 (heal recovered the job)."""
    def executor():
        return _ScriptedProc(stdout=_result_line("first (bad) attempt"), returncode=0)

    def verifier(idx, label):
        if "fix" in label:
            # The self-heal fix subprocess produces a new result_text.
            return _ScriptedProc(stdout=_result_line("fixed it properly"), returncode=0)
        # First verify FAILs, the re-verify (after the fix) PASSes.
        if idx == 0:
            return _ScriptedProc(
                stdout=_result_line(f"{VERIFY_MARKER} FAIL — X is still false"),
                returncode=0)
        return _ScriptedProc(
            stdout=_result_line(f"{VERIFY_MARKER} PASS — X now true"), returncode=0)

    events, logs, calls = _run_verify_job(executor=executor, verifier=verifier)
    term = _terminal(events)
    assert term["type"] == "succeeded", term
    assert term["result"]["verified"] is True
    assert term["result"]["self_heal_attempts"] == 1
    # The healed result_text replaced the original.
    assert term["result"]["output"] == "fixed it properly"
    # verify, fix1, re-verify == three aux spawns.
    assert calls["verify_spawns"] == 3
    assert [("fix" in l) for l in calls["labels"]] == [False, True, False]
    assert any("self-healing attempt 1" in d for s, d in logs if s == "event")


def test_r51_self_heal_exhausted_fails_with_honest_error():
    """Verifier FAILs every time → after MAX_FIX_ATTEMPTS self-heals the job is
    terminal 'failed' (exit_code 0) with an honest 'verification failed' error,
    not a false success."""
    def executor():
        return _ScriptedProc(stdout=_result_line("attempt"), returncode=0)

    def verifier(idx, label):
        if "fix" in label:
            return _ScriptedProc(stdout=_result_line("tried again"), returncode=0)
        return _ScriptedProc(
            stdout=_result_line(f"{VERIFY_MARKER} FAIL — still broken"), returncode=0)

    events, logs, calls = _run_verify_job(executor=executor, verifier=verifier)
    term = _terminal(events)
    assert term["type"] == "failed", term
    assert term["exit_code"] == 0  # the executor succeeded; verification did not
    assert term["result"]["verified"] is False
    assert term["result"]["self_heal_attempts"] == _wm.MAX_FIX_ATTEMPTS
    assert "verification failed" in term["error"]
    assert "still broken" in term["error"]
    # 1 verify + MAX_FIX_ATTEMPTS * (fix + re-verify).
    assert calls["verify_spawns"] == 1 + 2 * _wm.MAX_FIX_ATTEMPTS
    fixes = [l for l in calls["labels"] if "fix" in l]
    assert len(fixes) == _wm.MAX_FIX_ATTEMPTS
    # A failed terminal carries a diagnosis (stubbed in _mk_runjob_worker).
    assert "diagnosis" in term


def test_r51_verifier_inconclusive_accepts_unverified_not_false_success():
    """Pin the degradation: a verifier that produces NO verdict (crash/garbage
    output) is retried once (M3) and, still inconclusive, the job completes
    'succeeded' but with verified=None — accepted-unverified, never a confident
    success and never a self-heal (passed is None, not False)."""
    def executor():
        return _ScriptedProc(stdout=_result_line("did something"), returncode=0)

    def verifier(idx, label):
        # No marker at all → parse_verdict returns (None, ...); _run_verifier
        # retries once, so we expect exactly two verify spawns.
        assert "fix" not in label, "inconclusive must NOT trigger self-heal"
        return _ScriptedProc(stdout=_result_line("I am confused and emit no verdict"),
                             returncode=0)

    events, logs, calls = _run_verify_job(executor=executor, verifier=verifier)
    term = _terminal(events)
    assert term["type"] == "succeeded", term
    assert term["result"]["verified"] is None  # inconclusive, NOT True
    assert term.get("verified") is None
    assert term["result"]["self_heal_attempts"] == 0
    assert "inconclusive" in term["result"]["evidence"].lower()
    # M3: one retry → exactly two verify subprocesses, zero fixes.
    assert calls["verify_spawns"] == 2
    assert all("fix" not in l for l in calls["labels"])
    assert any("retrying once" in d for s, d in logs if s == "event")


def test_r51_unrecognized_verdict_is_inconclusive_not_pass():
    """A verifier that emits the marker but a word that is neither PASS nor FAIL
    (e.g. 'MAYBE') is parsed as no usable verdict (verify.parse_verdict ->
    (None, 'unrecognized verdict: …')); run_job retries once (M3) then completes
    'succeeded' with verified=None — an unrecognized verdict must NEVER read as a
    confident pass, and must NOT trigger self-heal (passed is None, not False)."""
    def executor():
        return _ScriptedProc(stdout=_result_line("did something"), returncode=0)

    def verifier(idx, label):
        assert "fix" not in label, "unrecognized verdict must NOT trigger self-heal"
        return _ScriptedProc(
            stdout=_result_line(f"weighing it up… {VERIFY_MARKER} MAYBE, hard to say"),
            returncode=0)

    events, logs, calls = _run_verify_job(executor=executor, verifier=verifier)
    term = _terminal(events)
    assert term["type"] == "succeeded", term
    assert term["result"]["verified"] is None
    assert term["result"]["self_heal_attempts"] == 0
    assert "inconclusive" in term["result"]["evidence"].lower()
    # M3 retry on the inconclusive (unrecognized) verdict → two verify spawns.
    assert calls["verify_spawns"] == 2
    # The unrecognized-verdict reason surfaced in the verifier log line.
    assert any("unrecognized verdict" in d for s, d in logs if s == "event")


def test_r51_verifier_timeout_is_inconclusive_accepted_unverified():
    """A verifier subprocess that hangs past its cap is SIGKILLed; with no
    verdict captured the outcome is the same inconclusive accepted-unverified
    completion (verified=None), exercising the _oneshot_agent timeout branch."""
    def executor():
        return _ScriptedProc(stdout=_result_line("did something"), returncode=0)

    def verifier(idx, label):
        return _ScriptedProc(stdout=b"", returncode=0, hang=True)

    # A tiny per-subprocess cap via an explicit wallclock budget so the verify
    # subprocess's wait_for trips quickly. job_started is ~now, so a 1.2s budget
    # leaves ~1.2s headroom for the verify subprocess (capped below default).
    events, logs, calls = _run_verify_job(
        executor=executor, verifier=verifier,
        spec_extra={"budget": {"max_wallclock_sec": 1.2}})
    term = _terminal(events)
    assert term["type"] == "succeeded", term
    assert term["result"]["verified"] is None
    assert "inconclusive" in term["result"]["evidence"].lower()


def test_r51_budget_exhausted_skips_verification_with_unverified_note():
    """When the token budget is already spent by the time the verify phase
    starts, _phase_progress returns no cap: verification is SKIPPED, the result
    is accepted with verified=None and the explicit budget-exhausted note — no
    verify/heal subprocess is ever spawned."""
    def executor():
        # Burn tokens in the executor so tokens_used >= max_tokens before verify.
        usage = {"type": "result", "result": "ran",
                 "usage": {"input_tokens": 600, "output_tokens": 600}}
        return _ScriptedProc(stdout=(json.dumps(usage) + "\n").encode(), returncode=0)

    def verifier(idx, label):  # must never be called
        raise AssertionError("verification must be skipped when budget exhausted")

    events, logs, calls = _run_verify_job(
        executor=executor, verifier=verifier,
        spec_extra={"budget": {"max_tokens": 1000}})
    term = _terminal(events)
    assert term["type"] == "succeeded", term
    assert calls["verify_spawns"] == 0  # no verifier spawned
    assert term["result"]["verified"] is None
    assert "budget exhausted" in term["result"]["evidence"]
    assert "verification skipped" in term["result"]["evidence"]


def test_r51_self_heal_blocked_when_no_budget_for_first_fix():
    """If the FIRST verify FAILs but also spends the whole token budget, the
    self-heal loop's very first _phase_progress reports exhausted: NO fix
    subprocess runs (the loop breaks before it), and the FAIL verdict stands ->
    terminal 'failed' with self_heal_attempts counted but zero fixes spawned."""
    def executor():
        return _ScriptedProc(stdout=_result_line("attempt"), returncode=0)

    def verifier(idx, label):
        # The (single) verify both FAILs and burns the entire budget, so the
        # self-heal step can't even start a fix.
        assert "fix" not in label, "no fix may run once the budget is spent"
        usage = {"type": "result",
                 "result": f"{VERIFY_MARKER} FAIL — broken",
                 "usage": {"input_tokens": 5000, "output_tokens": 5000}}
        return _ScriptedProc(stdout=(json.dumps(usage) + "\n").encode(), returncode=0)

    events, logs, calls = _run_verify_job(
        executor=executor, verifier=verifier,
        spec_extra={"budget": {"max_tokens": 1000}})
    term = _terminal(events)
    assert term["type"] == "failed", term
    assert term["result"]["verified"] is False
    # heals incremented to 1 before _phase_progress returned None and broke.
    assert term["result"]["self_heal_attempts"] == 1
    assert calls["verify_spawns"] == 1  # only the verify; no fix, no re-verify
    assert all("fix" not in l for l in calls["labels"])


def test_r51_server_cancel_during_verify_posts_no_terminal_event():
    """Pin the teardown path (worker.py:2048-2051): if the server cancels the job
    while it is in the verify phase, run_job detects the teardown after the
    verifier returns and posts NO terminal (succeeded/failed) event — the control
    plane has moved on (mirrors the lease-reconciliation contract). We trip the
    cancel from inside the verifier spawn, the moment the verify subprocess
    starts, so the post-verify _is_cancelled() check fires deterministically."""
    captured = {"w": None}

    async def go():
        w, events, logs = _mk_runjob_worker({})
        captured["w"] = w
        spec = {"kind": "claude", "intent": "do the thing", "task": "make X true",
                "verify": True}

        def executor():
            return _ScriptedProc(stdout=_result_line("ran"), returncode=0)

        def verifier(idx, label):
            # Simulate a server cancel landing exactly while the verifier runs:
            # mark the active entry torn down (what _kill_active_job does).
            entry = captured["w"]._active.get("jvc")
            assert entry is not None, "job should be active during verify"
            entry["cancelled"] = "cancelled"
            # Return a real PASS so the ONLY reason no success is posted is the
            # cancel teardown — not a missing/failed verdict.
            return _ScriptedProc(
                stdout=_result_line(f"{VERIFY_MARKER} PASS — would have passed"),
                returncode=0)

        cms, calls = _verify_e2e_patches(executor=executor, verifier=verifier)
        with cms[0], cms[1], cms[2]:
            await asyncio.wait_for(
                w.run_job({"id": "jvc", "spec": spec}), timeout=15.0)
        await w.close()
        return events, calls

    events, calls = asyncio.run(go())
    terminal = [e for e in events if e.get("type") in ("succeeded", "failed")]
    assert terminal == [], f"cancel during verify must post no terminal event; got {terminal}"
    # The verifier did run (so we're truly testing the post-verify teardown path,
    # not a pre-verify short-circuit), and self-heal never started.
    assert calls["verify_spawns"] == 1


def test_r51_self_heal_stops_when_budget_runs_out_midway():
    """If the budget is exhausted partway through self-heal (after the first
    fix's tokens land), the loop breaks and the LAST verdict stands — here the
    first verify FAILed, so the job is terminal 'failed' with the heals counted
    so far, rather than looping until MAX_FIX_ATTEMPTS."""
    def executor():
        return _ScriptedProc(stdout=_result_line("attempt"), returncode=0)

    def verifier(idx, label):
        if "fix" in label:
            # The fix burns the rest of the token budget, so the next
            # _phase_progress ('re-verifying') reports exhausted and breaks.
            usage = {"type": "result", "result": "tried",
                     "usage": {"input_tokens": 5000, "output_tokens": 5000}}
            return _ScriptedProc(stdout=(json.dumps(usage) + "\n").encode(), returncode=0)
        return _ScriptedProc(
            stdout=_result_line(f"{VERIFY_MARKER} FAIL — broken"), returncode=0)

    events, logs, calls = _run_verify_job(
        executor=executor, verifier=verifier,
        spec_extra={"budget": {"max_tokens": 1000}})
    term = _terminal(events)
    # First verify FAILed and the budget cut self-heal short before any re-verify
    # could flip it; passed is still False → honest failure.
    assert term["type"] == "failed", term
    assert term["result"]["verified"] is False
    assert term["result"]["self_heal_attempts"] == 1
    # verify (PASS-able? no, FAIL) + exactly one fix, then exhausted — no re-verify.
    assert calls["verify_spawns"] == 2
    assert [("fix" in l) for l in calls["labels"]] == [False, True]


def test_r51_no_verify_when_disabled_is_plain_success():
    """Control: verify:false short-circuits the whole trust loop — terminal
    'succeeded' with a plain {output} result and NO verified/evidence bundle,
    and the verifier is never spawned."""
    def executor():
        return _ScriptedProc(stdout=_result_line("done"), returncode=0)

    def verifier(idx, label):
        raise AssertionError("verifier must not run when verify:false")

    events, logs, calls = _run_verify_job(
        executor=executor, verifier=verifier, spec_extra={"verify": False})
    term = _terminal(events)
    assert term["type"] == "succeeded", term
    assert calls["verify_spawns"] == 0
    assert term.get("result") == {"output": "done"}
    assert "verified" not in (term.get("result") or {})


# ---------- [R72] bounded docker-container teardown on a WEDGED daemon ----------
# Promoted from LOOP/repro-a1-hunt8.py (A1 hunt #8, PR #82). `_kill_active_job`'s
# container teardown (`docker kill` / `docker rm -f`) once waited with a bare
# `await k.wait()` — the only subprocess wait in worker.py without a timeout. A
# wedged dockerd made it hang forever, and this teardown sits inside run_job's try
# BEFORE the R25 finally, so the wallclock-timeout path, a server cancel, and
# graceful `_shutdown_jobs` all hung: no terminal event, a permanent `_running`
# leak, and a worker that won't exit on SIGTERM. The fix bounds each wait with
# asyncio.wait_for(timeout=DOCKER_TEARDOWN_TIMEOUT), kills the stuck CLI on expiry,
# and emits a LOUD message naming the container so an operator can kill it by hand.

# A generous-but-finite cap derived from the teardown bound. Two CLIs run in
# sequence, so a sane bounded teardown finishes in ~2x the bound; +2s of headroom
# for overhead. Master's hang was *infinite*, so any cap above the real bound trips
# on it while a bounded fix returns comfortably under this.
_R72_HANG_CAP = 2 * wmod_R72.DOCKER_TEARDOWN_TIMEOUT + 2.0


class _R72DeadClient:
    """The `docker run` client process, already exited (returncode set) — i.e. the
    SIGKILL of its process group has happened. Teardown of the sibling CONTAINER
    (docker kill / docker rm -f) is what remains."""

    def __init__(self, rc: int = 137):
        self.returncode = rc
        self.pid = -1


class _R72WedgedProc:
    """A `docker kill` / `docker rm -f` subprocess against a WEDGED daemon: spawned,
    but wait() never returns on its own (the CLI is blocked talking to the hung
    daemon). Faithful to a real asyncio subprocess: wait() unblocks only once the
    process is killed — so a fix that bounds the wait and then kills the stuck CLI
    sees wait() return, while an unbounded `await k.wait()` hangs forever."""

    def __init__(self):
        self.returncode = None
        self.pid = 999999
        self._killed = asyncio.Event()
        self.kill_calls = 0

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self._killed.set()

    def terminate(self):
        self.kill()

    async def wait(self):
        await self._killed.wait()  # blocks until kill()/terminate() — never on its own
        return self.returncode


@pytest.fixture()
def _r72_wedge_docker(monkeypatch):
    """Stub the subprocess seam so any `docker kill`/`docker rm -f` spawns a wedged
    process. Records the argv attempted. No docker daemon required."""
    attempted: list[list[str]] = []
    spawned: list[_R72WedgedProc] = []

    async def fake_exec(*argv, **kw):
        attempted.append(list(argv))
        p = _R72WedgedProc()
        spawned.append(p)
        return p

    monkeypatch.setattr(wmod_R72.asyncio, "create_subprocess_exec", fake_exec)
    return attempted, spawned


def test_r72_kill_active_job_does_not_hang_when_docker_kill_wedges(_r72_wedge_docker):
    """A docker job's teardown must not block forever on a wedged daemon.

    Hung on master: `await k.wait()` in _kill_active_job was unbounded. A bounded
    teardown kills the stuck CLI and returns fast (well under _R72_HANG_CAP)."""
    attempted, spawned = _r72_wedge_docker

    async def go():
        w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
        w._active = {
            "jD": {"process": _R72DeadClient(), "is_docker": True, "cancelled": None}
        }
        try:
            await asyncio.wait_for(
                w._kill_active_job("jD", "timeout"), timeout=_R72_HANG_CAP)
        except asyncio.TimeoutError:
            pytest.fail(
                "_kill_active_job hung > %.1fs on a wedged `docker kill` "
                "(unbounded `await k.wait()`); attempted=%r"
                % (_R72_HANG_CAP, attempted))
        finally:
            await w.close()
        # BOTH the kill and the rm -f must have been attempted and bounded: an
        # expiry on the first must not skip the second.
        cmds = [a[:3] for a in attempted]
        assert ["docker", "kill", "roost-job-jD"] in cmds, attempted
        assert ["docker", "rm", "-f"] in [a[:3] for a in attempted], attempted
        # Each wedged CLI was killed (that is how wait() was made to return).
        assert spawned and all(p.kill_calls >= 1 for p in spawned), spawned

    asyncio.run(go())


def test_r72_kill_active_job_loud_message_and_completes_despite_wedged_daemon(
    _r72_wedge_docker,
):
    """The expiry path must be LOUD (name the container so an operator can kill it
    by hand) AND let teardown COMPLETE — both CLIs bounded, the active entry marked
    cancelled — even when both `docker kill` and `docker rm -f` wedge."""
    attempted, _spawned = _r72_wedge_docker

    async def go():
        w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
        logs: list[tuple[str, str, str]] = []

        async def fake_send_log(job_id, stream, data):
            logs.append((job_id, stream, data))

        w._send_log = fake_send_log  # type: ignore[assignment]
        w._active = {
            "jD": {"process": _R72DeadClient(), "is_docker": True, "cancelled": None}
        }
        try:
            await asyncio.wait_for(
                w._kill_active_job("jD", "timeout"), timeout=_R72_HANG_CAP)
        finally:
            await w.close()

        # Teardown completed: the entry is marked cancelled with our reason, and
        # both teardown commands were attempted (the first expiry did not skip the
        # second).
        assert w._active["jD"]["cancelled"] == "timeout"
        cmds = [a[:2] for a in attempted]
        assert ["docker", "kill"] in cmds and ["docker", "rm"] in cmds, attempted

        # LOUD: an operator-facing event line per wedged CLI, naming the container
        # (so they can `docker kill roost-job-jD` later) and saying it may still run.
        events = [d for (_jid, stream, d) in logs if stream == "event"]
        assert events, logs
        assert any("roost-job-jD" in d for d in events), events
        assert any(
            "unresponsive" in d and "docker kill roost-job-jD" in d for d in events
        ), events

    asyncio.run(go())


def test_r72_run_job_docker_timeout_teardown_completes_when_daemon_wedged(
    _r72_wedge_docker, monkeypatch
):
    """The wallclock-timeout path for a docker job must still post a terminal event
    and unwind accounting (R25 finally) even if container teardown wedges.

    Hung on master: run_job's timeout branch awaited _kill_active_job (which hung on
    the wedged `docker kill`) BEFORE the R25 finally — no terminal event, _running
    leaked. With the bound, teardown returns and the finally runs."""
    async def go():
        w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
        w.policy = {}
        events: list[dict] = []

        async def fake_post_event(job_id, event):
            events.append(event)

        async def fake_send_log(job_id, stream, data):
            pass

        async def fake_diagnose(spec, **kw):
            return {"summary": "stub"}

        w._post_event = fake_post_event       # type: ignore[assignment]
        w._send_log = fake_send_log           # type: ignore[assignment]
        w._diagnose_failure = fake_diagnose   # type: ignore[assignment]

        # Real `docker run` stand-in: a long sleeper we own, so the wallclock cap
        # fires and the SIGKILL-of-client path runs; the wedged seam is hit only by
        # the subsequent `docker kill`/`docker rm -f` (CONTAINER teardown).
        real_exec = asyncio.subprocess.create_subprocess_exec

        async def routing_exec(*argv, **kw):
            if tuple(argv[:2]) == ("docker", "run"):
                return await real_exec("sleep", "30", **kw)
            return _R72WedgedProc()  # docker kill / docker rm -f -> wedged

        monkeypatch.setattr(wmod_R72.asyncio, "create_subprocess_exec", routing_exec)

        spec = {
            "kind": "docker",
            "image": "alpine",
            "command": "sleep 30",
            "budget": {"max_wallclock_sec": 0.5},  # fires fast
        }
        # run_job increments _running (after "started"); the R25 finally decrements.
        # Start at 0: a clean unwind returns to 0; a hung teardown that skips the
        # finally leaves it at 1.
        w._running = 0
        try:
            await asyncio.wait_for(
                w.run_job({"id": "jD", "spec": spec}), timeout=_R72_HANG_CAP)
        except asyncio.TimeoutError:
            pytest.fail(
                "run_job hung > %.1fs in the docker wallclock-timeout teardown "
                "(unbounded `docker kill`); terminal events=%r, _running=%d "
                "(R25 finally never ran)"
                % (_R72_HANG_CAP, [e.get("type") for e in events], w._running))
        finally:
            await w.close()

        terminal = [e for e in events if e.get("type") in ("succeeded", "failed")]
        assert terminal, (
            "docker timeout produced no terminal event; events=%r"
            % [e.get("type") for e in events])
        assert w._running == 0, "capacity slot leaked (R25 cleanup did not run)"

    asyncio.run(go())


# ---------------------------------------------------------------------------
# [R99] Process-safety branch coverage for the kill paths (worker.py 1205-1310):
#   _kill_aux_procs (sync) — the two-level SIGKILL fallback (os.killpg, then
#     proc.kill() on ProcessLookupError/PermissionError/OSError), the early
#     return when no aux procs are tracked, and the already-exited skip.
#   _kill_active_job (sync seams) — the early return for an unknown/finished
#     job_id, and the main-process killpg→proc.kill() fallback.
#   _kill_active_job (async seam) — the docker spawn-FAILURE path (the outer
#     `except Exception`, distinct from R72's wedged-daemon TIMEOUT path).
# R72's docker-timeout tests above are the adjacent precedent; these add the
# sync fallbacks and the spawn-failure arm. Each stubs the os.killpg / proc.kill
# seams and asserts WHICH calls fired in WHICH order — not just that we returned.


class _R99FakeProc:
    """A fake child process with a real-ish pid so the os.killpg branch runs.

    Records kill() calls. `returncode` defaults to None (alive) so the kill path
    is taken; set it to an int to model an already-exited proc that must be
    SKIPPED. kill() optionally raises to exercise the inner fallback's own except."""

    def __init__(self, *, pid: int = 555001, returncode=None, kill_raises=None):
        self.pid = pid
        self.returncode = returncode
        self.kill_raises = kill_raises
        self.kill_calls = 0

    def kill(self):
        self.kill_calls += 1
        if self.kill_raises is not None:
            raise self.kill_raises


@pytest.fixture()
def _r99_killpg(monkeypatch):
    """Stub os.killpg/os.getpgid on the worker module so no real signal is sent.

    Returns a recorder. By default os.killpg SUCCEEDS (records the call). A test
    can install `recorder.raise_with = <exc instance>` to make every os.killpg
    raise that exception (driving the proc.kill() fallback). os.getpgid is stubbed
    to echo the pid so the killpg argument is the pid we can assert on."""

    class _Rec:
        def __init__(self):
            self.killpg_pids: list[int] = []
            self.raise_with = None

    rec = _Rec()

    def fake_getpgid(pid):
        return pid

    def fake_killpg(pgid, sig):
        rec.killpg_pids.append(pgid)
        if rec.raise_with is not None:
            raise rec.raise_with

    monkeypatch.setattr(wmod_R72.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(wmod_R72.os, "killpg", fake_killpg)
    return rec


def _r99_worker():
    return Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)


# ---- _kill_aux_procs: sync wins first ----


def test_r99_kill_aux_procs_no_procs_is_noop(_r99_killpg):
    """Early return when nothing is tracked for the job: no killpg, no error."""
    w = _r99_worker()
    # job with no entry at all, and a job with an empty set — both early-return.
    w._kill_aux_procs("absent", "cancelled")
    w._aux_procs["empty"] = set()
    w._kill_aux_procs("empty", "cancelled")
    assert _r99_killpg.killpg_pids == []


def test_r99_kill_aux_procs_killpg_path_for_live_proc(_r99_killpg):
    """A live aux proc is SIGKILLed via its process group (os.killpg succeeds);
    proc.kill() is NOT the fallback when killpg works."""
    w = _r99_worker()
    p = _R99FakeProc(pid=600100, returncode=None)
    w._aux_procs["j1"] = {p}
    w._kill_aux_procs("j1", "cancelled")
    assert _r99_killpg.killpg_pids == [600100]  # killpg fired on the proc's pid
    assert p.kill_calls == 0                     # fallback NOT taken on success


def test_r99_kill_aux_procs_skips_already_exited_proc(_r99_killpg):
    """An aux proc that already exited (returncode set) is left alone — neither
    os.killpg nor proc.kill() is called for it. Guards the `returncode is None`
    branch (mutation: dropping the guard would killpg a dead proc)."""
    w = _r99_worker()
    dead = _R99FakeProc(pid=600200, returncode=0)
    w._aux_procs["j2"] = {dead}
    w._kill_aux_procs("j2", "cancelled")
    assert _r99_killpg.killpg_pids == []
    assert dead.kill_calls == 0


@pytest.mark.parametrize(
    "exc", [ProcessLookupError(), PermissionError(), OSError("boom")]
)
def test_r99_kill_aux_procs_falls_back_to_proc_kill_when_killpg_raises(
    _r99_killpg, exc
):
    """When os.killpg raises ProcessLookupError/PermissionError/OSError, the code
    falls back to proc.kill(). Asserts the killpg was ATTEMPTED first, then the
    fallback fired (mutation: removing the except arm would let the exc escape)."""
    w = _r99_worker()
    _r99_killpg.raise_with = exc
    p = _R99FakeProc(pid=600300, returncode=None)
    w._aux_procs["j3"] = {p}
    w._kill_aux_procs("j3", "cancelled")  # must not raise
    assert _r99_killpg.killpg_pids == [600300]  # killpg tried first
    assert p.kill_calls == 1                     # then the fallback


def test_r99_kill_aux_procs_swallows_proc_kill_errors(_r99_killpg):
    """If the fallback proc.kill() ALSO raises (ProcessLookupError/OSError), it is
    swallowed — best-effort teardown never propagates. Guards the inner except."""
    w = _r99_worker()
    _r99_killpg.raise_with = PermissionError()
    p = _R99FakeProc(pid=600400, returncode=None,
                     kill_raises=ProcessLookupError())
    w._aux_procs["j4"] = {p}
    w._kill_aux_procs("j4", "cancelled")  # must not raise despite both raising
    assert _r99_killpg.killpg_pids == [600400]
    assert p.kill_calls == 1


# ---- _kill_active_job: sync seams ----


def test_r99_kill_active_job_unknown_id_early_returns(_r99_killpg):
    """No _active entry → return after the unconditional _kill_aux_procs (which is
    also a no-op here). Nothing is killed. Guards the `if not entry` branch."""
    async def go():
        w = _r99_worker()
        await w._kill_active_job("nope", "cancelled")  # no entry, no aux
        await w.close()
        assert _r99_killpg.killpg_pids == []
    asyncio.run(go())


def test_r99_kill_active_job_skips_already_finished_process(_r99_killpg):
    """An _active entry whose process already exited (returncode set) is marked
    cancelled but NOT killed (the `returncode is None` guard). Non-docker, so no
    container teardown either."""
    async def go():
        w = _r99_worker()
        dead = _R99FakeProc(pid=601000, returncode=0)
        w._active["jf"] = {"process": dead, "is_docker": False, "cancelled": None}
        await w._kill_active_job("jf", "cancelled")
        await w.close()
        assert w._active["jf"]["cancelled"] == "cancelled"  # reason recorded
        assert _r99_killpg.killpg_pids == []                # not killed
        assert dead.kill_calls == 0
    asyncio.run(go())


@pytest.mark.parametrize("exc", [ProcessLookupError(), PermissionError()])
def test_r99_kill_active_job_main_proc_killpg_fallback(_r99_killpg, exc):
    """When os.killpg on the main process raises ProcessLookupError/PermissionError,
    _kill_active_job falls back to proc.kill(). The reason is recorded regardless.
    (mutation: removing the except arm makes killpg's exc escape the coroutine.)"""
    async def go():
        w = _r99_worker()
        _r99_killpg.raise_with = exc
        p = _R99FakeProc(pid=601100, returncode=None)
        w._active["jk"] = {"process": p, "is_docker": False, "cancelled": None}
        await w._kill_active_job("jk", "cancelled")  # must not raise
        await w.close()
        assert _r99_killpg.killpg_pids == [601100]  # killpg tried first
        assert p.kill_calls == 1                     # then proc.kill() fallback
        assert w._active["jk"]["cancelled"] == "cancelled"
    asyncio.run(go())


def test_r99_kill_active_job_swallows_proc_kill_lookup_error(_r99_killpg):
    """The main-process fallback proc.kill() may itself race a ProcessLookupError
    (the child exited between killpg and kill); that is swallowed. Guards the inner
    `except ProcessLookupError` at the proc.kill() site (line 1229-1230)."""
    async def go():
        w = _r99_worker()
        _r99_killpg.raise_with = ProcessLookupError()
        p = _R99FakeProc(pid=601200, returncode=None,
                         kill_raises=ProcessLookupError())
        w._active["jr"] = {"process": p, "is_docker": False, "cancelled": None}
        await w._kill_active_job("jr", "cancelled")  # must not raise
        await w.close()
        assert _r99_killpg.killpg_pids == [601200]
        assert p.kill_calls == 1
    asyncio.run(go())


def test_r99_kill_active_job_also_kills_tracked_aux_procs(_r99_killpg):
    """_kill_active_job ALWAYS tears down aux verify/fix procs first (the [H5]
    contract), even for a job whose main process already finished — a job mid
    verify/self-heal has no live main proc but aux procs burning tokens."""
    async def go():
        w = _r99_worker()
        aux = _R99FakeProc(pid=602000, returncode=None)
        w._aux_procs["jA"] = {aux}
        # main process already exited; only the aux proc should be killed.
        w._active["jA"] = {"process": _R99FakeProc(pid=602001, returncode=0),
                           "is_docker": False, "cancelled": None}
        await w._kill_active_job("jA", "cancelled")
        await w.close()
        assert 602000 in _r99_killpg.killpg_pids  # aux proc was SIGKILLed
        assert aux.kill_calls == 0                 # killpg succeeded, no fallback
    asyncio.run(go())


# ---- _kill_active_job: docker spawn-FAILURE async seam (distinct from R72) ----


def test_r99_kill_active_job_docker_spawn_failure_is_swallowed(monkeypatch):
    """If spawning `docker kill` itself FAILS (docker CLI missing, fork-limit, …),
    the outer `except Exception` swallows it and teardown moves on to `docker rm
    -f` — the failure must not escape or wedge. This is the spawn-failure arm,
    distinct from R72's wedged-daemon timeout arm. Asserts both teardown commands
    were ATTEMPTED despite the first spawn raising."""
    attempted: list[list[str]] = []

    async def boom_exec(*argv, **kw):
        attempted.append(list(argv))
        raise FileNotFoundError("docker: command not found")

    monkeypatch.setattr(wmod_R72.asyncio, "create_subprocess_exec", boom_exec)

    async def go():
        w = _r99_worker()
        # Client already exited; container teardown is what remains.
        w._active["jD"] = {"process": _R72DeadClient(), "is_docker": True,
                           "cancelled": None}
        # Must complete (not raise, not hang) despite the spawn failures.
        await asyncio.wait_for(
            w._kill_active_job("jD", "timeout"), timeout=5.0)
        await w.close()
        # BOTH commands attempted: the first spawn failure did not skip the second.
        cmds = [a[:3] for a in attempted]
        assert ["docker", "kill", "roost-job-jD"] in cmds, attempted
        assert ["docker", "rm", "-f"] in [a[:3] for a in attempted], attempted
        assert w._active["jD"]["cancelled"] == "timeout"
    asyncio.run(go())


def test_r99_kill_active_job_docker_spawn_failure_best_effort_kills_half_started(
    monkeypatch,
):
    """When create_subprocess_exec returns a proc but a LATER step raises, the
    outer except's best-effort `k.kill()` fires (the `if k is not None` arm,
    line 1278-1282). We force the later failure by making wait_for raise."""
    spawned: list[_R72WedgedProc] = []

    async def half_exec(*argv, **kw):
        p = _R72WedgedProc()
        spawned.append(p)
        return p

    async def boom_wait_for(aw=None, *a, **kw):
        # Close the awaitable we were handed (k.wait()) so it isn't left
        # un-awaited, then fail to simulate a post-spawn teardown error.
        if aw is not None and hasattr(aw, "close"):
            aw.close()
        raise RuntimeError("simulated post-spawn failure")

    monkeypatch.setattr(wmod_R72.asyncio, "create_subprocess_exec", half_exec)
    monkeypatch.setattr(wmod_R72.asyncio, "wait_for", boom_wait_for)

    async def go():
        w = _r99_worker()
        w._active["jH"] = {"process": _R72DeadClient(), "is_docker": True,
                           "cancelled": None}
        # No outer asyncio.wait_for here: it is monkeypatched to raise. The except
        # path itself terminates the half-started procs, so there is nothing to
        # hang on — boom_wait_for forces the post-spawn failure synchronously.
        await w._kill_active_job("jH", "timeout")
        await w.close()
        # Each half-started proc was best-effort killed by the outer except.
        assert spawned and all(p.kill_calls >= 1 for p in spawned), spawned
        assert w._active["jH"]["cancelled"] == "timeout"
    asyncio.run(go())


def test_r99_kill_active_job_spawn_failure_swallows_best_effort_kill_error(
    monkeypatch,
):
    """The spawn-failure best-effort `k.kill()` may ITSELF raise (the proc is in a
    bad state); that is swallowed too (line 1281-1282 `except (ProcessLookupError,
    OSError)`). Force it: spawn returns a proc whose kill() raises OSError, then a
    post-spawn failure drives the outer except. Teardown still completes."""

    class _KillRaiser:
        def __init__(self):
            self.returncode = None
            self.pid = 777001
            self.kill_calls = 0

        def kill(self):
            self.kill_calls += 1
            raise OSError("kill failed")

    spawned: list = []

    async def half_exec(*argv, **kw):
        p = _KillRaiser()
        spawned.append(p)
        return p

    async def boom_wait_for(aw=None, *a, **kw):
        if aw is not None and hasattr(aw, "close"):
            aw.close()
        raise RuntimeError("simulated post-spawn failure")

    monkeypatch.setattr(wmod_R72.asyncio, "create_subprocess_exec", half_exec)
    monkeypatch.setattr(wmod_R72.asyncio, "wait_for", boom_wait_for)

    async def go():
        w = _r99_worker()
        w._active["jE"] = {"process": _R72DeadClient(), "is_docker": True,
                           "cancelled": None}
        await w._kill_active_job("jE", "timeout")  # must not raise
        await w.close()
        # kill() was attempted on each spawned proc despite itself raising.
        assert spawned and all(p.kill_calls >= 1 for p in spawned), spawned
        assert w._active["jE"]["cancelled"] == "timeout"
    asyncio.run(go())


def test_r99_kill_active_job_timeout_kill_and_reap_errors_swallowed(monkeypatch):
    """The wedged-daemon TIMEOUT arm's own kill()/reap may raise: `k.kill()` on
    expiry raising (line 1257-1258 `except (ProcessLookupError, OSError)`) and the
    follow-up reap `await k.wait()` raising (line 1262-1263 `except Exception`).
    Both swallowed; teardown still emits its LOUD operator message and completes."""

    class _ReapRaiser:
        """A `docker kill` proc that times out; kill() raises OSError and the
        follow-up wait() raises, exercising both inner excepts on the expiry path."""

        def __init__(self):
            self.returncode = None
            self.pid = 778001
            self.kill_calls = 0

        def kill(self):
            self.kill_calls += 1
            raise OSError("kill raced")

        async def wait(self):
            raise RuntimeError("reap failed")

    spawned: list = []

    async def fake_exec(*argv, **kw):
        p = _ReapRaiser()
        spawned.append(p)
        return p

    # Force the wait_for to TIMEOUT (the expiry branch) without parking real time.
    async def timeout_wait_for(aw=None, *a, **kw):
        if aw is not None and hasattr(aw, "close"):
            aw.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(wmod_R72.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(wmod_R72.asyncio, "wait_for", timeout_wait_for)

    async def go():
        w = _r99_worker()
        logs: list[tuple[str, str, str]] = []

        async def fake_send_log(job_id, stream, data):
            logs.append((job_id, stream, data))

        w._send_log = fake_send_log  # type: ignore[assignment]
        w._active["jT"] = {"process": _R72DeadClient(), "is_docker": True,
                           "cancelled": None}
        await w._kill_active_job("jT", "timeout")  # must not raise
        await w.close()
        # kill() attempted on each timed-out CLI even though it raised.
        assert spawned and all(p.kill_calls >= 1 for p in spawned), spawned
        # LOUD message still emitted despite kill/reap raising.
        events = [d for (_j, s, d) in logs if s == "event"]
        assert any("unresponsive" in d and "roost-job-jT" in d for d in events), events
        assert w._active["jT"]["cancelled"] == "timeout"
    asyncio.run(go())


# ---------------------------------------------------------------------------
# [R96] Pure argv-builder coverage: _build_auto_argv + _build_codex_argv.
# These exercise the bare-worker triage builder and the codex builder directly,
# asserting EXACT argv shape (positions matter — a fixed-index splice is the R30
# bug class) and the raised errors on bad/missing input. No subprocess is spawned.
# ---------------------------------------------------------------------------


def _claude_only(name):
    """shutil.which stub: claude present, everything else (bwrap/codex) absent."""
    return "/usr/bin/claude" if name == "claude" else None


def test_build_auto_argv_missing_task_and_intent_raises(monkeypatch):
    """No `task` and no `intent` → ValueError before any CLI lookup (line 1085)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    with pytest.raises(ValueError, match="auto job requires `task`"):
        _build_auto_argv(
            {"kind": "auto"}, "jobX",
            worker_policy={}, base_url=None, token=None,
            can_dispatch=False, triage_prompt="TRIAGE", tempfiles=[], cwd="/work")


def test_build_auto_argv_falls_back_to_intent_when_no_task(monkeypatch):
    """`task` is absent but `intent` is present → intent is used as the -p prompt
    (line 1083: `spec.get("task") or spec.get("intent")`)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv = _build_auto_argv(
        {"kind": "auto", "intent": "use the intent text"}, "jobI",
        worker_policy={}, base_url=None, token=None,
        can_dispatch=False, triage_prompt="", tempfiles=[], cwd="/work")
    ci = argv.index("claude")
    assert argv[ci:ci + 3] == ["claude", "-p", "use the intent text"]


def test_build_auto_argv_task_wins_over_intent(monkeypatch):
    """When both are present, `task` takes precedence over `intent`."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv = _build_auto_argv(
        {"kind": "auto", "task": "the task", "intent": "the intent"}, "jobT",
        worker_policy={}, base_url=None, token=None,
        can_dispatch=False, triage_prompt="", tempfiles=[], cwd="/work")
    ci = argv.index("claude")
    assert argv[ci:ci + 3] == ["claude", "-p", "the task"]


def test_build_auto_argv_defaults_sandbox_and_model(monkeypatch):
    """Bare spec (no permissions, no model) → auto defaults to sandboxed execution
    and AUTO_DEFAULT_MODEL (lines 1090-1093). With native --sandbox support the
    sandbox default surfaces as the `--sandbox` flag; the model surfaces as
    `--model <AUTO_DEFAULT_MODEL>`."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv = _build_auto_argv(
        {"kind": "auto", "task": "do it"}, "jobD",
        worker_policy={}, base_url=None, token=None,
        can_dispatch=False, triage_prompt="", tempfiles=[], cwd="/work")
    assert "--sandbox" in argv, f"default sandbox not applied: {argv!r}"
    mi = argv.index("--model")
    assert argv[mi + 1] == AUTO_DEFAULT_MODEL


def test_build_auto_argv_respects_explicit_permissions_and_model(monkeypatch):
    """Spec that pins permissions + model must NOT be overwritten by the auto
    defaults (the `if not sub.get(...)` guards at 1090/1092 skip)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv = _build_auto_argv(
        {
            "kind": "auto", "task": "do it",
            "permissions": {"sandbox": False, "mode": "plan"},
            "model": "claude-haiku-4-9",
        },
        "jobP",
        worker_policy={}, base_url=None, token=None,
        can_dispatch=False, triage_prompt="", tempfiles=[], cwd="/work")
    # Explicit model preserved, AUTO_DEFAULT_MODEL not forced.
    mi = argv.index("--model")
    assert argv[mi + 1] == "claude-haiku-4-9"
    assert AUTO_DEFAULT_MODEL not in argv
    # Explicit sandbox:False + mode:plan → permission-mode plan, no --sandbox.
    assert "--sandbox" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_build_auto_argv_splices_triage_prompt_after_claude_p_task(monkeypatch):
    """The triage system prompt is spliced as `--append-system-prompt <prompt>`
    immediately after `claude -p <task>` (lines 1106-1112). Exact positions."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv = _build_auto_argv(
        {"kind": "auto", "task": "build the thing"}, "jobS",
        worker_policy={}, base_url=None, token=None,
        can_dispatch=False, triage_prompt="TRIAGE SYS", tempfiles=[], cwd="/work")
    ci = argv.index("claude")
    assert argv[ci:ci + 3] == ["claude", "-p", "build the thing"]
    assert argv[ci + 3:ci + 5] == ["--append-system-prompt", "TRIAGE SYS"], (
        "triage prompt must be spliced right after `claude -p <task>`, "
        f"not elsewhere: {argv[ci:ci + 6]!r}"
    )


def test_build_auto_argv_no_triage_prompt_no_splice(monkeypatch):
    """Empty triage_prompt → the `if triage_prompt:` guard (1106) is false, so NO
    --append-system-prompt is inserted at all (the 1106->1113 branch)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv = _build_auto_argv(
        {"kind": "auto", "task": "x"}, "jobN",
        worker_policy={}, base_url=None, token=None,
        can_dispatch=False, triage_prompt="", tempfiles=[], cwd="/work")
    assert "--append-system-prompt" not in argv


def test_build_auto_argv_splices_inside_bwrap_jail_not_into_bwrap_flags(monkeypatch):
    """[R30 bug class] When the underlying argv is bwrap-wrapped
    (`bwrap <opts...> -- claude -p <task> ...`), the triage prompt must land
    AFTER `claude -p <task>` inside the jail — NOT spliced into bwrap's own flags
    via a fixed argv[:3]. This is the security-relevant anchoring: `argv.index("claude")`
    (lines 1107-1112) must locate claude past bwrap's `--` separator."""
    import roost.worker as wm

    # claude AND bwrap present; claude lacks --sandbox → bwrap fallback branch.
    monkeypatch.setattr(
        wm.shutil, "which",
        lambda n: "/usr/bin/" + n if n in ("claude", "bwrap") else None)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: False)
    argv = _build_auto_argv(
        {"kind": "auto", "task": "do the thing"}, "jobB",
        worker_policy={"sandbox": "bwrap"},  # NOT trusted; opt-in bwrap policy
        base_url=None, token=None, can_dispatch=False,
        triage_prompt="SYS PROMPT", tempfiles=[], cwd="/work")
    assert argv[0] == "bwrap", f"expected bwrap-wrapped argv, got {argv!r}"
    # bwrap's leading options must be untouched: `--ro-bind / /` intact.
    assert argv[1:4] == ["--ro-bind", "/", "/"], (
        "bwrap options corrupted — splice landed inside bwrap's flags: "
        f"{argv[1:6]!r}"
    )
    sep = argv.index("--")
    ci = argv.index("claude")
    assert ci > sep, "claude must appear after bwrap's `--` separator"
    assert argv[ci:ci + 3] == ["claude", "-p", "do the thing"]
    assert argv[ci + 3:ci + 5] == ["--append-system-prompt", "SYS PROMPT"], (
        "triage prompt must sit inside the jail after `claude -p <task>`, "
        f"not in bwrap's flags: {argv[ci:ci + 6]!r}"
    )


def test_build_auto_argv_passes_args_through(monkeypatch):
    """spec `args` flow through _build_claude_argv into the final argv, and the
    triage splice does not disturb them (they remain after the claude flags)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv = _build_auto_argv(
        {"kind": "auto", "task": "t", "args": ["--max-turns", "7"]}, "jobA",
        worker_policy={}, base_url=None, token=None,
        can_dispatch=False, triage_prompt="SYS", tempfiles=[], cwd="/work")
    assert "--max-turns" in argv
    assert argv[argv.index("--max-turns") + 1] == "7"
    # passthrough args come AFTER the spliced triage prompt
    assert argv.index("--max-turns") > argv.index("--append-system-prompt")


# ---- _build_codex_argv ----


def test_build_codex_argv_missing_intent_raises(monkeypatch):
    """No `intent` → ValueError, before the CLI lookup (lines 1117-1119)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", lambda n: "/usr/bin/codex")
    with pytest.raises(ValueError, match="codex job requires `intent`"):
        _build_codex_argv({"kind": "codex"})


def test_build_codex_argv_missing_cli_raises(monkeypatch):
    """codex not on PATH → FileNotFoundError (lines 1120-1121)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", lambda n: None)
    with pytest.raises(FileNotFoundError, match="`codex` CLI not on PATH"):
        _build_codex_argv({"kind": "codex", "intent": "do it"})


def test_build_codex_argv_basic_shape(monkeypatch):
    """Happy path: `codex exec --skip-git-repo-check <intent>` with no args.

    [R103] --skip-git-repo-check is a flag to `exec` (before the prompt) so codex
    jobs run on a fresh worker whose cwd is not a git repo.
    """
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", lambda n: "/usr/local/bin/codex")
    argv = _build_codex_argv({"kind": "codex", "intent": "fix the bug"})
    assert argv == ["codex", "exec", "--skip-git-repo-check", "fix the bug"]


def test_build_codex_argv_appends_args(monkeypatch):
    """`args` are appended verbatim after `codex exec ... <intent>`."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", lambda n: "/usr/local/bin/codex")
    argv = _build_codex_argv(
        {"kind": "codex", "intent": "fix it", "args": ["--model", "o3", "--full-auto"]})
    assert argv == [
        "codex", "exec", "--skip-git-repo-check", "fix it",
        "--model", "o3", "--full-auto",
    ]


def test_build_codex_argv_includes_skip_git_repo_check(monkeypatch):
    """[R103] codex exec aborts in a non-git cwd unless --skip-git-repo-check is
    passed; a fresh worker's default cwd is a plain home dir. The flag must be a
    flag to `exec` (before the prompt), not appended after the intent.

    Repro promoted from the UAT-triage scratch repro (failed on master 8ab927b).
    """
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", lambda n: "/usr/local/bin/codex")
    argv = _build_codex_argv({"kind": "codex", "intent": "fix the bug"})
    assert "--skip-git-repo-check" in argv, argv
    # It must precede the prompt so it's parsed as an exec flag, not the prompt.
    assert argv.index("--skip-git-repo-check") < argv.index("fix the bug"), argv


# ---------------------------------------------------------------------------
# [R102] build_command kind-dispatch ROUTER coverage (worker.py:502, 507-546).
# The argv-BUILDERS each branch calls were covered in R96/R99/R1; UNcovered is
# the router seam that (a) routes `command` str vs list vs invalid-type, (b)
# picks among the auto/docker/claude/codex builders by `kind`, raising ValueError
# on an unknown kind, and (c) resolves cwd (spec.cwd / default_cwd / os.getcwd()).
# This is the security-relevant seam that decides WHICH builder runs. These tests
# call the REAL build_command directly (never patched) and assert the exact
# returned (argv, cwd, tempfiles) tuple per branch. Each kind branch is pinned by
# patching ONLY its target builder with a sentinel — so a swapped dispatch (e.g.
# claude→codex) makes the matching test fail. No subprocess is ever spawned.
# ---------------------------------------------------------------------------


# ---- `command` str / list / invalid-type routing (worker.py:525-531) ----


def test_build_command_string_wraps_in_sh_c():
    """A string `command` routes to `/bin/sh -c <cmd>` (line 527-528). cwd/tempfiles
    flow through unchanged."""
    argv, cwd, tempfiles = build_command(
        {"command": "echo hi"}, "job1", default_cwd="/work")
    assert argv == ["/bin/sh", "-c", "echo hi"]
    assert cwd == "/work"
    assert tempfiles == []


def test_build_command_list_returns_copy_not_alias():
    """A list `command` routes to a *copy* of the list (line 529-530: `list(cmd)`)
    — the returned argv must equal but NOT be the same object the caller passed
    (a fresh-index splice would otherwise mutate the spec)."""
    cmd = ["ls", "-la", "/data"]
    argv, cwd, tempfiles = build_command(
        {"command": cmd}, "job2", default_cwd="/d")
    assert argv == ["ls", "-la", "/data"]
    assert argv is not cmd, "list command must be copied, not aliased to the spec"
    assert cwd == "/d"
    assert tempfiles == []


@pytest.mark.parametrize("bad", [
    {"a": 1},          # dict
    42,                # int
    3.14,              # float
    ("ls", "-l"),      # tuple is NOT a list
    True,              # bool
])
def test_build_command_invalid_command_type_raises(bad):
    """A `command` that is neither str nor list → ValueError (line 531). Note a
    tuple is rejected too — only `isinstance(..., list)` is accepted."""
    with pytest.raises(ValueError, match=r"`command` must be string or list"):
        build_command({"command": bad}, "jobBad", default_cwd="/x")


def test_build_command_empty_command_falls_through_to_kind(monkeypatch):
    """An empty/falsey `command` ("") fails the `if spec.get("command")` guard
    (line 525) and falls through to the kind dispatch — here defaulting to claude.
    Proves the guard is truthiness, not key-presence."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv, _cwd, _tf = build_command(
        {"command": "", "intent": "do the thing"}, "jobE", default_cwd="/w")
    assert argv[:3] == ["claude", "-p", "do the thing"]


# ---- kind dispatch: the right builder is selected per kind ----


def test_build_command_kind_auto_dispatches_to_auto_builder(monkeypatch):
    """kind=auto routes to _build_auto_argv (line 507-517). Pinned with a sentinel:
    swapping this branch's dispatch makes the test fail."""
    import roost.worker as wm

    sentinel = ["AUTO_ARGV_SENTINEL"]
    monkeypatch.setattr(wm, "_build_auto_argv", lambda *a, **k: sentinel)
    argv, cwd, tempfiles = build_command(
        {"kind": "auto", "task": "t"}, "jobAuto", default_cwd="/auto")
    assert argv is sentinel, "kind=auto must dispatch to _build_auto_argv"
    assert cwd == "/auto"
    assert tempfiles == []


def test_build_command_kind_auto_is_case_insensitive(monkeypatch):
    """The auto check lowercases `kind` (line 507: `.lower() == "auto"`), so AUTO
    / Auto route to the auto builder too."""
    import roost.worker as wm

    sentinel = ["AUTO_SENTINEL"]
    monkeypatch.setattr(wm, "_build_auto_argv", lambda *a, **k: sentinel)
    argv, _cwd, _tf = build_command({"kind": "AUTO", "task": "t"}, "jA", default_cwd="/x")
    assert argv is sentinel


def test_build_command_kind_docker_dispatches_to_docker_builder(monkeypatch):
    """kind=docker routes to _build_docker_argv (line 522-523), checked BEFORE the
    `command` guard so a docker job carrying both image and in-container command
    still goes to the docker builder."""
    import roost.worker as wm

    sentinel = ["DOCKER_ARGV_SENTINEL"]
    monkeypatch.setattr(wm, "_build_docker_argv", lambda *a, **k: sentinel)
    # Carries a `command` too — must NOT be intercepted by the command branch.
    argv, cwd, tempfiles = build_command(
        {"kind": "docker", "image": "alpine", "command": ["ls"]},
        "jobDk", default_cwd="/dk")
    assert argv is sentinel, "kind=docker must dispatch to _build_docker_argv"
    assert cwd == "/dk"
    assert tempfiles == []


def test_build_command_kind_claude_dispatches_to_claude_builder(monkeypatch):
    """kind=claude routes to _build_claude_argv (line 533-543)."""
    import roost.worker as wm

    sentinel = ["CLAUDE_ARGV_SENTINEL"]
    monkeypatch.setattr(wm, "_build_claude_argv", lambda *a, **k: sentinel)
    argv, cwd, tempfiles = build_command(
        {"kind": "claude", "intent": "x"}, "jobCl", default_cwd="/cl")
    assert argv is sentinel, "kind=claude must dispatch to _build_claude_argv"
    assert cwd == "/cl"
    assert tempfiles == []


def test_build_command_default_kind_is_claude(monkeypatch):
    """No `kind` and no `command` → defaults to claude (line 533:
    `spec.get("kind") or "claude"`). Pinned to the claude builder sentinel."""
    import roost.worker as wm

    sentinel = ["DEFAULT_CLAUDE_SENTINEL"]
    monkeypatch.setattr(wm, "_build_claude_argv", lambda *a, **k: sentinel)
    argv, _cwd, _tf = build_command({"intent": "x"}, "jobDef", default_cwd="/d")
    assert argv is sentinel, "absent kind must default to the claude builder"


def test_build_command_kind_codex_dispatches_to_codex_builder(monkeypatch):
    """kind=codex routes to _build_codex_argv (line 544-545)."""
    import roost.worker as wm

    sentinel = ["CODEX_ARGV_SENTINEL"]
    monkeypatch.setattr(wm, "_build_codex_argv", lambda *a, **k: sentinel)
    argv, cwd, tempfiles = build_command(
        {"kind": "codex", "intent": "x"}, "jobCx", default_cwd="/cx")
    assert argv is sentinel, "kind=codex must dispatch to _build_codex_argv"
    assert cwd == "/cx"
    assert tempfiles == []


def test_build_command_unknown_kind_raises(monkeypatch):
    """An unrecognised kind with no `command` → ValueError naming the kind
    (line 546). The message echoes the offending kind."""
    import roost.worker as wm

    # Ensure no builder is reached even if PATH lookups would otherwise succeed.
    monkeypatch.setattr(wm.shutil, "which", lambda n: None)
    with pytest.raises(ValueError, match=r"unknown kind: 'wizard'"):
        build_command({"kind": "wizard"}, "jobU", default_cwd="/u")


def test_build_command_unknown_kind_with_command_does_not_raise():
    """An unknown kind is harmless when a `command` is present: the command branch
    (line 525) wins before the kind dispatch is reached, so no ValueError."""
    argv, _cwd, _tf = build_command(
        {"kind": "wizard", "command": "echo ok"}, "jobUC", default_cwd="/uc")
    assert argv == ["/bin/sh", "-c", "echo ok"]


# ---- real end-to-end dispatch (no builder patched) confirms each real builder ----


def test_build_command_routes_to_real_claude_builder(monkeypatch):
    """End-to-end: with claude on PATH the default/claude branch produces the real
    _build_claude_argv head `claude -p <intent>` — proves the router wires the spec,
    job_id, and cwd through to the genuine builder, not just a sentinel."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", _claude_only)
    monkeypatch.setattr(wm, "_claude_supports_sandbox", lambda: True)
    argv, cwd, tempfiles = build_command(
        {"kind": "claude", "intent": "real intent"}, "jobReal", default_cwd="/real")
    assert argv[:3] == ["claude", "-p", "real intent"]
    assert cwd == "/real"
    assert isinstance(tempfiles, list)


def test_build_command_routes_to_real_codex_builder(monkeypatch):
    """End-to-end: codex on PATH → real `codex exec <intent>`."""
    import roost.worker as wm

    monkeypatch.setattr(wm.shutil, "which", lambda n: "/usr/local/bin/codex")
    argv, _cwd, _tf = build_command(
        {"kind": "codex", "intent": "real codex"}, "jobRc", default_cwd="/rc")
    assert argv == ["codex", "exec", "--skip-git-repo-check", "real codex"]


def test_build_command_routes_to_real_docker_builder():
    """End-to-end: kind=docker → real `docker run --rm --name roost-job-<id>` with
    the image and in-container command after it."""
    argv, _cwd, _tf = build_command(
        {"kind": "docker", "image": "alpine:3.20", "command": ["sh", "-c", "id"]},
        "jobRd", default_cwd="/rd")
    assert argv[:5] == ["docker", "run", "--rm", "--name", "roost-job-jobRd"]
    i = argv.index("alpine:3.20")
    assert argv[i + 1:] == ["sh", "-c", "id"]


# ---- cwd precedence: spec.cwd > default_cwd > os.getcwd() (worker.py:502) ----


def test_build_command_cwd_prefers_spec_cwd():
    """spec.cwd wins over default_cwd (line 502 first disjunct)."""
    _argv, cwd, _tf = build_command(
        {"command": "x", "cwd": "/spec/dir"}, "jobC1", default_cwd="/default/dir")
    assert cwd == "/spec/dir"


def test_build_command_cwd_falls_back_to_default_cwd():
    """No spec.cwd → default_cwd (line 502 second disjunct)."""
    _argv, cwd, _tf = build_command(
        {"command": "x"}, "jobC2", default_cwd="/default/dir")
    assert cwd == "/default/dir"


def test_build_command_cwd_falls_back_to_getcwd(monkeypatch):
    """No spec.cwd and no default_cwd → os.getcwd() (line 502 final disjunct)."""
    import roost.worker as wm

    monkeypatch.setattr(wm.os, "getcwd", lambda: "/the/process/cwd")
    _argv, cwd, _tf = build_command({"command": "x"}, "jobC3")
    assert cwd == "/the/process/cwd"


def test_build_command_empty_spec_cwd_falls_back_to_default():
    """An empty-string spec.cwd is falsey → default_cwd is used (truthiness, not
    key-presence, at line 502)."""
    _argv, cwd, _tf = build_command(
        {"command": "x", "cwd": ""}, "jobC4", default_cwd="/default/dir")
    assert cwd == "/default/dir"


# ============================================================================
# [R106] input-delivery seam ([R38] _deliver_inputs / _ack_input / _send_log
#   413|429 log-drop / _post_event HTTPError) + creds error branches
#   (_claude_creds_path CLAUDE_CONFIG_DIR override; _refresh_claude_creds error
#   arms only — the happy path is already covered by the r43 tests above).
#
# Method: stub `self.client` with a fake async httpx (records every GET/POST,
# serves a scripted response or raises httpx.HTTPError) and a fake `proc`
# exposing `.stdin`/`.returncode` — no real subprocess, no network. Every test
# asserts REAL behavior: which inputs ended `delivered` vs `dropped` and with
# which exact detail string, the 413/429 log-drop line, the CLAUDE_CONFIG_DIR
# override path. Each docstring names the mutation the assertions would catch.
# ============================================================================

import httpx as _r106_httpx  # noqa: E402

from roost.worker import INPUT_DELIVERY_UNSUPPORTED  # noqa: E402


class _R106Resp:
    """Minimal stand-in for an httpx.Response: a status_code and a JSON body.

    `json_body` may be a dict (returned by .json()), or the sentinel _RAISE to
    make .json() raise (exercising the non-JSON error-body fallback in
    _send_log). `.text` is always available."""

    _RAISE = object()

    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is _R106Resp._RAISE:
            raise ValueError("not JSON")
        return self._json_body


class _R106FakeClient:
    """A fake `self.client`. Records every (method, url, json) call. For GETs it
    pops the next scripted item off `get_script`; for POSTs off `post_script`.
    A scripted item is either an _R106Resp (returned) or an Exception instance
    (raised). When a script is empty, returns a bland 200 so a test only has to
    script the calls it cares about."""

    def __init__(self, get_script=None, post_script=None):
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self._get_script = list(get_script or [])
        self._post_script = list(post_script or [])

    async def get(self, url, params=None, timeout=None):
        self.get_calls.append((url, params or {}))
        if self._get_script:
            item = self._get_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return _R106Resp(200, {})

    async def post(self, url, json=None, timeout=None):
        self.post_calls.append((url, json or {}))
        if self._post_script:
            item = self._post_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return _R106Resp(200, {})

    async def aclose(self):
        # so Worker.close() (which awaits self.client.aclose()) works.
        pass


class _R106FakeStdin:
    """A fake StreamWriter for a process's stdin. Records writes; `is_closing()`
    and a failing write are configurable so we can drive the closed-stdin and
    BrokenPipe arms of _deliver_inputs."""

    def __init__(self, *, closing=False, write_raises=None):
        self.buf = b""
        self._closing = closing
        self._write_raises = write_raises
        self.drained = 0

    def is_closing(self):
        return self._closing

    def write(self, data):
        if self._write_raises is not None:
            raise self._write_raises
        self.buf += data

    async def drain(self):
        self.drained += 1


class _R106FakeProc:
    """A fake child process exposing only what _deliver_inputs touches:
    `.stdin` (a writer or None) and `.returncode` (None = alive)."""

    def __init__(self, *, stdin=None, returncode=None):
        self.stdin = stdin
        self.returncode = returncode


def _r106_worker(fake_client):
    w = Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)
    w.client = fake_client  # type: ignore[assignment]
    return w


def _r106_active_entry(*, stdin=None, returncode=None, live_stdin=True,
                       cancelled=None):
    proc = _R106FakeProc(stdin=stdin, returncode=returncode)
    return {"process": proc, "is_docker": False, "cancelled": cancelled,
            "since": 0.0, "live_stdin": live_stdin}


# ---------- _deliver_inputs: structural early returns ----------


def test_r106_deliver_inputs_no_active_entry_is_noop():
    """No _active entry for the job → early return before any HTTP call.
    Mutation guard: dropping the `if not entry: return` would issue a GET."""
    async def go():
        client = _R106FakeClient()
        w = _r106_worker(client)
        # _active is empty → job is unknown.
        await w._deliver_inputs("ghost")
        assert client.get_calls == []   # no inputs fetch happened
        assert client.post_calls == []  # nothing acked
        await w.close()

    asyncio.run(go())


def test_r106_deliver_inputs_http_ge_400_returns_without_acking():
    """A >=400 from the inputs GET → return, no inputs processed, no acks.
    Mutation guard: weakening `>= 400` to `> 400` (or dropping the guard) would
    try to read .json() of an error body and ack."""
    async def go():
        client = _R106FakeClient(get_script=[_R106Resp(503, {"inputs": [
            {"id": "i1", "text": "x"}]})])
        w = _r106_worker(client)
        w._active["jE"] = _r106_active_entry(stdin=_R106FakeStdin())
        await w._deliver_inputs("jE")
        assert len(client.get_calls) == 1            # the fetch was attempted
        assert client.post_calls == []               # but nothing was acked
        await w.close()

    asyncio.run(go())


@pytest.mark.parametrize("exc", [
    _r106_httpx.ConnectError("down"),  # an httpx.HTTPError subclass
    ValueError("bad json"),            # .json() decode failure
])
def test_r106_deliver_inputs_fetch_failure_is_swallowed(exc, capsys):
    """httpx.HTTPError or ValueError while fetching inputs → swallowed, logged,
    no acks. Mutation guard: removing the except arm would let the error escape
    _deliver_inputs (it is called from the heartbeat loop and must not crash)."""
    async def go():
        client = _R106FakeClient(get_script=[exc])
        w = _r106_worker(client)
        w._active["jF"] = _r106_active_entry(stdin=_R106FakeStdin())
        await w._deliver_inputs("jF")   # must not raise
        assert client.post_calls == []  # nothing acked on a fetch failure
        await w.close()

    asyncio.run(go())
    out = capsys.readouterr().out
    assert "input fetch failed for jF" in out


# ---------- _deliver_inputs: per-input outcomes ----------


def test_r106_deliver_inputs_skips_input_without_id():
    """An input item with no id is skipped (continue) — not acked, not delivered.
    Mutation guard: dropping `if not input_id: continue` would NoneType-key the
    ack. We pair it with a valid input to prove the loop continues past the skip."""
    async def go():
        stdin = _R106FakeStdin()
        client = _R106FakeClient(get_script=[_R106Resp(200, {"inputs": [
            {"id": None, "text": "ignored"},
            {"id": "ok", "text": "real"},
        ]})])
        w = _r106_worker(client)
        w._active["jS"] = _r106_active_entry(stdin=stdin)
        await w._deliver_inputs("jS")
        # Exactly one ack: for the valid input only.
        acks = [c for c in client.post_calls if c[0].endswith("/input-ack")]
        assert len(acks) == 1
        assert acks[0][1]["input_id"] == "ok"
        assert acks[0][1]["state"] == "delivered"
        await w.close()

    asyncio.run(go())


def test_r106_deliver_inputs_not_live_is_dropped_unsupported():
    """When the job can't take live input (live_stdin False), the input is acked
    `dropped` with the INPUT_DELIVERY_UNSUPPORTED detail — never silently lost,
    and nothing is written to stdin. Mutation guard: flipping this ack to
    `delivered`, or swapping the detail to the exited-message, fails here."""
    async def go():
        stdin = _R106FakeStdin()
        client = _R106FakeClient(get_script=[_R106Resp(200, {"inputs": [
            {"id": "iN", "text": "hello"}]})])
        w = _r106_worker(client)
        w._active["jN"] = _r106_active_entry(stdin=stdin, live_stdin=False)
        await w._deliver_inputs("jN")
        acks = [c for c in client.post_calls if c[0].endswith("/input-ack")]
        assert len(acks) == 1
        body = acks[0][1]
        assert body == {"input_id": "iN", "state": "dropped",
                        "detail": INPUT_DELIVERY_UNSUPPORTED}
        assert stdin.buf == b""  # nothing written to a non-live stream
        await w.close()

    asyncio.run(go())


@pytest.mark.parametrize("entry_kw", [
    {"returncode": 0},                              # process already exited
    {"stdin": None},                               # no stdin pipe at all
    {"stdin": _R106FakeStdin(closing=True)},       # stdin.is_closing()
    {"live_stdin": True, "cancelled": "lease_lost"},  # cancelled overrides live
])
def test_r106_deliver_inputs_dead_or_closed_is_dropped(entry_kw):
    """A LIVE-kind job whose process exited / has no pipe / has a closing pipe /
    is cancelled → acked `dropped` with the 'no longer accepting input' detail
    (NOT the unsupported-kind message). Mutation guard: dropping any of the
    `proc_alive` / `stdin is None` / `is_closing()` / `cancelled` checks would
    flip this to a delivery attempt."""
    async def go():
        # default live_stdin True unless the param overrides it; ensure a writer
        # exists for the exited / cancelled cases so the *liveness* check (not the
        # missing-pipe check) is what trips.
        kw = dict(entry_kw)
        if "stdin" not in kw:
            kw["stdin"] = _R106FakeStdin()
        client = _R106FakeClient(get_script=[_R106Resp(200, {"inputs": [
            {"id": "iD", "text": "hi"}]})])
        w = _r106_worker(client)
        w._active["jD"] = _r106_active_entry(**kw)
        await w._deliver_inputs("jD")
        acks = [c for c in client.post_calls if c[0].endswith("/input-ack")]
        assert len(acks) == 1
        body = acks[0][1]
        assert body["input_id"] == "iD"
        assert body["state"] == "dropped"
        # For a cancelled job, `not live` is True → unsupported message; for the
        # other three the stream is dead-but-live-kind → exited/closed message.
        if entry_kw.get("cancelled"):
            assert body["detail"] == INPUT_DELIVERY_UNSUPPORTED
        else:
            assert body["detail"] == (
                "process is no longer accepting input (exited/closed)")
        await w.close()

    asyncio.run(go())


@pytest.mark.parametrize("text,expected", [
    ("run this", b"run this\n"),     # newline appended
    ("already\n", b"already\n"),     # already terminated → not doubled
])
def test_r106_deliver_inputs_writes_and_acks_delivered(text, expected, capsys):
    """The happy path: a live stdin gets the (newline-terminated) payload, is
    drained, an event log is emitted, and the input is acked `delivered` with the
    'written to process stdin' detail. Mutation guard: flipping this ack to
    `dropped`, or skipping the newline-append, fails here."""
    async def go():
        stdin = _R106FakeStdin()
        client = _R106FakeClient(get_script=[_R106Resp(200, {"inputs": [
            {"id": "iW", "text": text}]})])
        w = _r106_worker(client)
        w._active["jW"] = _r106_active_entry(stdin=stdin)
        await w._deliver_inputs("jW")
        assert stdin.buf == expected          # exact bytes written
        assert stdin.drained == 1             # flushed so it isn't stuck in buffer
        posts = client.post_calls
        # An event log line AND the delivered ack were both POSTed.
        log_posts = [c for c in posts if c[0].endswith("/logs")]
        ack_posts = [c for c in posts if c[0].endswith("/input-ack")]
        assert len(log_posts) == 1
        assert log_posts[0][1]["stream"] == "event"
        assert "delivered input to process stdin" in log_posts[0][1]["data"]
        assert len(ack_posts) == 1
        assert ack_posts[0][1] == {"input_id": "iW", "state": "delivered",
                                   "detail": "written to process stdin"}
        await w.close()

    asyncio.run(go())
    assert "input delivered to stdin" in capsys.readouterr().out


def test_r106_deliver_inputs_broken_pipe_is_dropped():
    """A BrokenPipeError on stdin.write → acked `dropped` with a 'stdin write
    failed' detail that names the error; no `delivered` ack is sent. Mutation
    guard: removing the write-failure except arm lets the OSError escape the
    heartbeat loop; flipping the ack to `delivered` lies about a lost input."""
    async def go():
        stdin = _R106FakeStdin(write_raises=BrokenPipeError("epipe"))
        client = _R106FakeClient(get_script=[_R106Resp(200, {"inputs": [
            {"id": "iB", "text": "boom"}]})])
        w = _r106_worker(client)
        w._active["jB"] = _r106_active_entry(stdin=stdin)
        await w._deliver_inputs("jB")
        ack_posts = [c for c in client.post_calls if c[0].endswith("/input-ack")]
        assert len(ack_posts) == 1
        body = ack_posts[0][1]
        assert body["input_id"] == "iB"
        assert body["state"] == "dropped"
        assert body["detail"].startswith("stdin write failed:")
        assert "epipe" in body["detail"]
        # No event log claiming delivery on a failed write.
        log_posts = [c for c in client.post_calls if c[0].endswith("/logs")]
        assert log_posts == []
        await w.close()

    asyncio.run(go())


def test_r106_deliver_inputs_acks_to_correct_endpoint():
    """The ack POST targets /workers/<wid>/jobs/<jid>/input-ack — proves the URL
    is built from worker_id and job_id (mutation: a wrong path would 404 the ack
    server-side, silently losing the outcome)."""
    async def go():
        stdin = _R106FakeStdin()
        client = _R106FakeClient(get_script=[_R106Resp(200, {"inputs": [
            {"id": "iU", "text": "x"}]})])
        w = _r106_worker(client)
        w._active["jU"] = _r106_active_entry(stdin=stdin)
        await w._deliver_inputs("jU")
        # The inputs fetch used the same worker/job path.
        assert client.get_calls[0][0] == "/workers/w1/jobs/jU/inputs"
        ack = [c for c in client.post_calls if c[0].endswith("/input-ack")][0]
        assert ack[0] == "/workers/w1/jobs/jU/input-ack"
        await w.close()

    asyncio.run(go())


# ---------- _ack_input: HTTPError swallow ----------


def test_r106_ack_input_swallows_httperror(capsys):
    """A network failure POSTing the ack is swallowed (best-effort) and logged;
    it never propagates. Mutation guard: removing the except arm would crash the
    delivery loop on a transient CP blip."""
    async def go():
        client = _R106FakeClient(post_script=[_r106_httpx.ConnectError("x")])
        w = _r106_worker(client)
        await w._ack_input("jA", "iA", "delivered", detail="d")  # must not raise
        # The POST was attempted with the right shape before it failed.
        assert client.post_calls[0][0] == "/workers/w1/jobs/jA/input-ack"
        assert client.post_calls[0][1] == {
            "input_id": "iA", "state": "delivered", "detail": "d"}
        await w.close()

    asyncio.run(go())
    assert "input ack failed for jA/iA" in capsys.readouterr().out


# ---------- _send_log: real method, 413/429 log-drop + HTTPError ----------


@pytest.mark.parametrize("status", [413, 429])
def test_r106_send_log_413_429_drops_with_json_detail(status, capsys):
    """The REAL _send_log: a 413 (oversize) / 429 (row ceiling) response makes the
    line be dropped LOUDLY with the server's JSON `detail` and the status code —
    the job keeps running (no raise). Mutation guard: weakening `>= 400` or
    dropping the detail would silently swallow why logs vanish."""
    async def go():
        client = _R106FakeClient(post_script=[_R106Resp(
            status, {"detail": "log row ceiling reached"})])
        w = _r106_worker(client)
        await w._send_log("jL", "stdout", "a noisy line")  # must not raise
        assert client.post_calls[0][0] == "/workers/w1/jobs/jL/logs"
        assert client.post_calls[0][1] == {"stream": "stdout",
                                           "data": "a noisy line"}
        await w.close()

    asyncio.run(go())
    out = capsys.readouterr().out
    assert f"log line dropped by server ({status})" in out
    assert "log row ceiling reached" in out


def test_r106_send_log_drop_falls_back_to_text_on_non_json_body(capsys):
    """If the >=400 error body isn't JSON (.json() raises), the drop message uses
    the raw `.text` instead. Mutation guard: removing the try/except around
    .json() would crash _send_log on a plain-text 413."""
    async def go():
        client = _R106FakeClient(post_script=[_R106Resp(
            413, _R106Resp._RAISE, text="Payload Too Large")])
        w = _r106_worker(client)
        await w._send_log("jT", "stderr", "x")
        await w.close()

    asyncio.run(go())
    out = capsys.readouterr().out
    assert "log line dropped by server (413)" in out
    assert "Payload Too Large" in out


def test_r106_send_log_swallows_httperror(capsys):
    """A network failure POSTing a log is swallowed and logged — observability is
    not the work, so the job is unaffected. Mutation guard: removing the except
    would crash on a transient CP blip."""
    async def go():
        client = _R106FakeClient(post_script=[_r106_httpx.ReadTimeout("slow")])
        w = _r106_worker(client)
        await w._send_log("jH", "stdout", "line")  # must not raise
        await w.close()

    asyncio.run(go())
    assert "log POST failed" in capsys.readouterr().out


def test_r106_send_log_2xx_is_quiet(capsys):
    """A normal 200 log POST produces NO drop message. Pins the `>= 400` guard so
    a mutation to `>= 200` (which would log every line as dropped) fails."""
    async def go():
        client = _R106FakeClient(post_script=[_R106Resp(200, {})])
        w = _r106_worker(client)
        await w._send_log("jOK", "stdout", "fine")
        await w.close()

    asyncio.run(go())
    assert "log line dropped" not in capsys.readouterr().out


# ---------- _post_event: HTTPError path ----------


def test_r106_post_event_swallows_httperror(capsys):
    """A network failure POSTing a terminal/lifecycle event is swallowed and
    logged. Mutation guard: removing the except arm would crash the reporting
    path on a transient CP blip."""
    async def go():
        client = _R106FakeClient(post_script=[_r106_httpx.ConnectError("x")])
        w = _r106_worker(client)
        await w._post_event("jP", {"type": "succeeded"})  # must not raise
        assert client.post_calls[0][0] == "/workers/w1/jobs/jP/event"
        assert client.post_calls[0][1] == {"type": "succeeded"}
        await w.close()

    asyncio.run(go())
    assert "event POST failed" in capsys.readouterr().out


# ---------- _claude_creds_path: CLAUDE_CONFIG_DIR override vs default ----------


def test_r106_claude_creds_path_uses_config_dir_override(monkeypatch):
    """With CLAUDE_CONFIG_DIR set (the shared-box case), creds live under that
    isolated dir, NOT under ~/.claude. Mutation guard: ignoring the env var would
    point a shared box at the wrong (host) credentials file."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/iso/cfg")
    w = _r106_worker(_R106FakeClient())
    assert w._claude_creds_path() == Path("/iso/cfg/.credentials.json")
    asyncio.run(w.close())


def test_r106_claude_creds_path_defaults_to_home_dot_claude(monkeypatch):
    """With CLAUDE_CONFIG_DIR unset, creds default to ~/.claude/.credentials.json.
    Mutation guard: hard-coding the env path would break the unshared default."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/op")))
    w = _r106_worker(_R106FakeClient())
    assert w._claude_creds_path() == Path("/home/op/.claude/.credentials.json")
    asyncio.run(w.close())


def test_r106_claude_creds_path_expands_user_in_config_dir(monkeypatch):
    """A ~-relative CLAUDE_CONFIG_DIR is expanded (expanduser). Pins the
    `.expanduser()` call so dropping it would yield a literal '~' path."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/myconf")
    monkeypatch.setenv("HOME", "/home/op")  # expanduser reads $HOME
    w = _r106_worker(_R106FakeClient())
    assert w._claude_creds_path() == Path("/home/op/myconf/.credentials.json")
    asyncio.run(w.close())


# ---------- _refresh_claude_creds: ERROR branches only ----------
# (the happy / unchanged / disabled paths are covered by the r43 tests above)


def test_r106_refresh_creds_httperror_on_get_is_noop(tmp_path, monkeypatch):
    """If GET /claude-creds raises httpx.HTTPError, refresh returns early and does
    NOT touch the local creds file. Mutation guard: removing the except would
    crash the refresh-forever loop when the CP is briefly unreachable."""
    async def go():
        creds = tmp_path / ".credentials.json"
        client = _R106FakeClient(get_script=[_r106_httpx.ConnectError("down")])
        w = _r106_worker(client)
        monkeypatch.setattr(w, "_claude_creds_path", lambda: creds)
        await w._refresh_claude_creds()  # must not raise
        assert not creds.exists()        # nothing written on a fetch failure
        await w.close()

    asyncio.run(go())


def test_r106_refresh_creds_non_200_leaves_file_alone(tmp_path, monkeypatch):
    """A non-200 (e.g. 404 provisioning-disabled) → leave the local copy alone.
    Here the file pre-exists with sentinel content; refresh must NOT overwrite it.
    Mutation guard: dropping `if r.status_code != 200: return` would parse an
    error body and clobber the file."""
    async def go():
        creds = tmp_path / ".credentials.json"
        creds.write_text("KEEP-ME")
        client = _R106FakeClient(get_script=[_R106Resp(404, {})])
        w = _r106_worker(client)
        monkeypatch.setattr(w, "_claude_creds_path", lambda: creds)
        await w._refresh_claude_creds()
        assert creds.read_text() == "KEEP-ME"  # untouched
        await w.close()

    asyncio.run(go())


def test_r106_refresh_creds_empty_creds_is_noop(tmp_path, monkeypatch):
    """A 200 whose body has no/blank credentials_json → no write. Mutation guard:
    dropping `if not creds: return` would write an empty creds file over a good
    one, breaking the worker's claude auth."""
    async def go():
        creds = tmp_path / ".credentials.json"
        creds.write_text("KEEP-ME")
        client = _R106FakeClient(get_script=[
            _R106Resp(200, {"credentials_json": ""})])
        w = _r106_worker(client)
        monkeypatch.setattr(w, "_claude_creds_path", lambda: creds)
        await w._refresh_claude_creds()
        assert creds.read_text() == "KEEP-ME"
        await w.close()

    asyncio.run(go())


def test_r106_refresh_creds_oserror_on_write_is_logged(tmp_path, monkeypatch,
                                                       capsys):
    """If the atomic creds write fails with OSError, it is caught and printed
    (not raised). We force the failure by pointing the creds path at a location
    whose PARENT is a regular file (so mkdir/open both fail). Mutation guard:
    removing the OSError handler would crash refresh-forever on a read-only or
    full disk."""
    async def go():
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a dir")
        creds = blocker / "sub" / ".credentials.json"  # parent is a file
        client = _R106FakeClient(get_script=[
            _R106Resp(200, {"credentials_json": "NEWCREDS"})])
        w = _r106_worker(client)
        monkeypatch.setattr(w, "_claude_creds_path", lambda: creds)
        await w._refresh_claude_creds()  # must not raise
        assert not creds.exists()
        await w.close()

    asyncio.run(go())
    assert "creds refresh write failed" in capsys.readouterr().out
