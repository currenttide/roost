"""On-node steward tests — capacity judgment + failure diagnosis.

The steward agent (haiku `claude -p`) is NEVER invoked here: we mock the worker's
subprocess call (`_run_steward_agent`) and exercise the pure parsing / fail-safe /
deterministic-fallback logic. Confirms the pinned wire contract:
  * load.capacity is a steward-judged int >= 1, fail-safe = 1
  * a FAILED terminal event carries a short `diagnosis` string
"""
from __future__ import annotations

import asyncio

import pytest

from roost import steward
from roost.worker import Worker


# ---------- pure capacity parsing ----------


def test_parse_capacity_good_json():
    raw = '{"max_concurrent": 4, "reason": "8 idle cores, 30GB free"}'
    assert steward.parse_capacity(raw) == 4


def test_parse_capacity_embedded_in_text():
    raw = 'Here is my answer:\n{"max_concurrent": 3, "reason": "ok"}\nThanks!'
    assert steward.parse_capacity(raw) == 3


def test_parse_capacity_rejects_below_one():
    assert steward.parse_capacity('{"max_concurrent": 0}') is None
    assert steward.parse_capacity('{"max_concurrent": -2}') is None


def test_parse_capacity_rejects_garbage():
    assert steward.parse_capacity(None) is None
    assert steward.parse_capacity("") is None
    assert steward.parse_capacity("not json at all") is None
    assert steward.parse_capacity('{"max_concurrent": "lots"}') is None


def test_capacity_prompt_mentions_facts_and_contract():
    facts = {"cpus": 8, "running_jobs": 1}
    p = steward.capacity_prompt(facts)
    assert "max_concurrent" in p
    assert "running_jobs" in p  # facts JSON embedded


# ---------- machine facts ----------


def test_machine_facts_no_gpu():
    facts = steward.machine_facts(
        {"cpus": 4, "ram_gb": 16}, running_jobs=2, find_nvidia_smi=lambda: None)
    assert facts["cpus"] == 4
    assert facts["running_jobs"] == 2
    assert "gpus" not in facts


# ---------- deterministic diagnosis fallback ----------


def test_deterministic_diagnosis_uses_last_stderr_line():
    d = steward.deterministic_diagnosis(
        exit_code=2, stderr_tail="warming up\nModuleNotFoundError: No module named 'foo'")
    assert d == "exit_code=2 — ModuleNotFoundError: No module named 'foo'"


def test_deterministic_diagnosis_falls_back_to_stdout_then_error():
    assert steward.deterministic_diagnosis(
        exit_code=1, stderr_tail="", stdout_tail="boom here").endswith("boom here")
    assert steward.deterministic_diagnosis(
        exit_code=None, stderr_tail="", error="spawn failed") == "exit_code=? — spawn failed"


def test_deterministic_diagnosis_is_bounded():
    long = "x" * 5000
    d = steward.deterministic_diagnosis(exit_code=1, stderr_tail=long)
    assert len(d) <= steward.DIAGNOSIS_MAX


def test_clean_diagnosis_single_line_bounded():
    assert steward.clean_diagnosis("first line\nsecond line") == "first line"
    assert steward.clean_diagnosis("   ") is None
    assert len(steward.clean_diagnosis("y" * 1000)) <= steward.DIAGNOSIS_MAX


def test_spec_summary_compact():
    s = steward.spec_summary({"kind": "docker", "image": "alpine", "command": "true"})
    assert s.startswith("kind=docker")


# ---------- Worker-level fail-safe / agentic paths (subprocess mocked) ----------


def _mk_worker():
    return Worker("http://127.0.0.1:9", "tok", "w1", self_test=False)


def test_capacity_failsafe_is_one_when_steward_absent(monkeypatch):
    w = _mk_worker()

    async def _none(*a, **k):
        return None  # claude absent / call failed → parse fails → fail-safe

    monkeypatch.setattr(w, "_run_steward_agent", _none)
    cap = asyncio.run(w._judge_capacity())
    assert cap == steward.FALLBACK_CAPACITY == 1
    assert w._capacity == 1
    asyncio.run(w.close())


def test_capacity_uses_good_steward_response(monkeypatch):
    w = _mk_worker()

    async def _ok(*a, **k):
        return '{"max_concurrent": 6, "reason": "idle 32-core box"}'

    monkeypatch.setattr(w, "_run_steward_agent", _ok)
    cap = asyncio.run(w._judge_capacity())
    assert cap == 6
    assert w._capacity == 6
    asyncio.run(w.close())


def test_diagnosis_deterministic_fallback_when_steward_absent(monkeypatch):
    w = _mk_worker()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(w, "_run_steward_agent", _none)
    d = asyncio.run(w._diagnose_failure(
        {"kind": "command", "command": "pytest"}, exit_code=1,
        stdout_tail="", stderr_tail="AssertionError: 2 != 3"))
    assert d == "exit_code=1 — AssertionError: 2 != 3"
    asyncio.run(w.close())


def test_diagnosis_uses_agent_when_available(monkeypatch):
    w = _mk_worker()

    async def _agent(prompt, *, label, timeout_s):
        return "Missing dependency: numpy is not installed in the venv."

    monkeypatch.setattr(w, "_run_steward_agent", _agent)
    d = asyncio.run(w._diagnose_failure(
        {"kind": "command", "command": "python x.py"}, exit_code=1,
        stdout_tail="", stderr_tail="ModuleNotFoundError: numpy"))
    assert d == "Missing dependency: numpy is not installed in the venv."
    asyncio.run(w.close())


# ---------- [BUG3b] steward subprocess env is sanitized ----------


def test_steward_subprocess_env_is_sanitized(monkeypatch):
    """A job can pre-seed ANTHROPIC_*/*_PROXY/etc. into the worker's environment; the
    steward `claude -p` call must run with those stripped (so an attacker-controlled
    diagnosis prompt can't redirect the steward's creds or inject code)."""
    w = _mk_worker()
    monkeypatch.setattr("roost.worker.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://evil")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil:8080")
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/x.js")
    monkeypatch.setenv("PATH", "/usr/bin")  # ordinary key must survive

    captured = {}

    async def fake_exec(*argv, env=None, **kwargs):
        captured["env"] = env

        class FakeProc:
            returncode = 0
            pid = -1

            async def communicate(self):
                return (b'{"type":"result","result":"ok"}', b"")

        return FakeProc()

    monkeypatch.setattr("roost.worker.asyncio.create_subprocess_exec", fake_exec)
    out = asyncio.run(w._run_steward_agent("prompt", label="capacity", timeout_s=5.0))
    assert out == "ok"
    env = captured["env"]
    assert "ANTHROPIC_BASE_URL" not in env
    assert "HTTPS_PROXY" not in env
    assert "NODE_OPTIONS" not in env
    assert env.get("PATH") == "/usr/bin"
    asyncio.run(w.close())
