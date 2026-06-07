"""Narration watcher tests (ease-of-use-plan D2).

Covers the pure helpers thoroughly (prompt builder, tolerant parser, selector)
and ``watch_once`` end-to-end with a stubbed ``run_claude`` + a recording
``store``. The live ``default_claude_runner`` agent path is validated on the
fleet, not here.
"""
from __future__ import annotations

import asyncio

import pytest

from roost import watcher
from roost.watcher import (
    DEFAULT_MIN_INTERVAL,
    MIN_INTERVAL_FLOOR,
    jobs_needing_narration,
    narrate_job,
    parse_narration,
    render_narration_prompt,
    resolve_min_interval,
    watch_once,
)


def run(coro):
    return asyncio.run(coro)


# ---------- render_narration_prompt ----------


def test_prompt_includes_goal_and_activity():
    p = render_narration_prompt(
        goal="train a resnet on imagenet",
        last_activity="epoch 3/10 loss=0.4",
        log_tail="step 1200 ...",
        elapsed_sec=125,
    )
    assert "train a resnet on imagenet" in p
    assert "epoch 3/10 loss=0.4" in p
    assert "step 1200" in p
    assert "125s" in p
    assert "JSON" in p  # asks for structured output


def test_prompt_handles_missing_fields():
    p = render_narration_prompt(goal="", last_activity=None, log_tail=None, elapsed_sec=None)
    assert "(no goal recorded)" in p
    assert "(no recent activity line)" in p
    assert "(no log output yet)" in p
    assert "unknown" in p  # elapsed unknown


def test_prompt_forbids_speculation_and_invention():
    p = render_narration_prompt(
        goal="do a thing",
        last_activity="step 5",
        log_tail="working...",
        elapsed_sec=10,
    )
    low = p.lower()
    # The prompt must instruct the model not to invent facts and to use only
    # the provided input (grounding against the rate-limit hallucination bug).
    assert "invent" in low
    assert "rate limit" in low  # explicitly called out as a forbidden fabrication
    assert ("only what" in low) or ("only" in low and "input" in low)


# ---------- parse_narration ----------


def test_parse_clean_json():
    out = parse_narration(
        '{"narration": "Training is progressing normally, on epoch 3.", '
        '"progress": 30, "eta_sec": 600}'
    )
    assert out["narration"] == "Training is progressing normally, on epoch 3."
    assert out["progress"] == 30
    assert out["eta_sec"] == 600


def test_parse_json_in_fences():
    text = (
        "Here is my assessment:\n"
        "```json\n"
        '{"narration": "Cloning the repo and installing deps.", '
        '"progress": 10, "eta_sec": 120}\n'
        "```\n"
        "Hope that helps!"
    )
    out = parse_narration(text)
    assert out["narration"] == "Cloning the repo and installing deps."
    assert out["progress"] == 10
    assert out["eta_sec"] == 120


def test_parse_json_embedded_in_prose():
    text = 'The job looks fine. {"narration": "Looks healthy.", "progress": 50, "eta_sec": null} done.'
    out = parse_narration(text)
    assert out["narration"] == "Looks healthy."
    assert out["progress"] == 50
    assert out["eta_sec"] is None


def test_parse_prose_only_salvages_a_line():
    out = parse_narration("The job seems to be stuck waiting on a network call.")
    assert "stuck" in out["narration"]
    assert out["progress"] is None
    assert out["eta_sec"] is None


def test_parse_garbage_returns_safe_defaults():
    for junk in ["", "   ", "}}}{{{", "\x00\x01", "```\n```"]:
        out = parse_narration(junk)
        assert set(out.keys()) == {"narration", "progress", "eta_sec"}
        assert out["progress"] is None
        assert out["eta_sec"] is None
        assert isinstance(out["narration"], str)


def test_parse_never_raises_on_weird_input():
    # Including non-str-ish shapes routed through str() upstream; here ensure no raise.
    for v in [None, "null", "[]", "true", '{"narration": 123}']:
        out = parse_narration(v if isinstance(v, str) else str(v))
        assert isinstance(out, dict)


def test_parse_collapses_multiline_narration():
    out = parse_narration('{"narration": "line one\\nline two", "progress": 5, "eta_sec": 1}')
    assert "\n" not in out["narration"]
    assert out["narration"] == "line one line two"


def test_parse_clamps_and_coerces_numbers():
    assert parse_narration('{"narration":"x","progress":150,"eta_sec":5}')["progress"] == 100
    assert parse_narration('{"narration":"x","progress":-5,"eta_sec":5}')["progress"] == 0
    assert parse_narration('{"narration":"x","progress":"42","eta_sec":"90"}') == {
        "narration": "x",
        "progress": 42,
        "eta_sec": 90,
    }
    # bad numerics -> None, narration still recovered
    out = parse_narration('{"narration":"x","progress":"abc","eta_sec":-9}')
    assert out["progress"] is None
    assert out["eta_sec"] is None
    # bool must not be treated as a number
    assert parse_narration('{"narration":"x","progress":true,"eta_sec":false}')["progress"] is None


def test_parse_prefers_later_valid_object_after_bad_brace():
    text = '{ not json at all ::: } and then {"narration": "Recovered.", "progress": 20, "eta_sec": 30}'
    out = parse_narration(text)
    assert out["narration"] == "Recovered."
    assert out["progress"] == 20


# ---------- jobs_needing_narration ----------


def test_select_picks_running_missing_narration():
    jobs = [{"id": "a", "state": "running"}]
    sel = jobs_needing_narration(jobs, now=1000.0)
    assert [j["id"] for j in sel] == ["a"]


def test_select_skips_fresh_narration():
    jobs = [{"id": "a", "state": "running", "narrated_at": 995.0}]
    sel = jobs_needing_narration(jobs, now=1000.0, min_interval=20.0)
    assert sel == []  # only 5s old


def test_select_picks_stale_narration():
    jobs = [{"id": "a", "state": "running", "narrated_at": 900.0}]
    sel = jobs_needing_narration(jobs, now=1000.0, min_interval=20.0)
    assert [j["id"] for j in sel] == ["a"]


def test_select_includes_assigned_skips_terminal_and_queued():
    jobs = [
        {"id": "run", "state": "running"},
        {"id": "asg", "state": "assigned"},
        {"id": "q", "state": "queued"},
        {"id": "ok", "state": "succeeded"},
        {"id": "bad", "state": "failed"},
        {"id": "x", "state": "cancelled"},
    ]
    sel = {j["id"] for j in jobs_needing_narration(jobs, now=1000.0)}
    assert sel == {"run", "asg"}


def test_select_handles_bad_timestamp_and_none_jobs():
    jobs = [None, {"id": "a", "state": "running", "narrated_at": "oops"}]
    sel = jobs_needing_narration(jobs, now=1000.0)
    assert [j["id"] for j in sel] == ["a"]  # bad ts -> treated as needing


# ---------- narrate_job ----------


def test_narrate_job_uses_stub_and_parses():
    async def stub(prompt):
        assert "my-goal" in prompt
        return '{"narration": "Doing my-goal.", "progress": 40, "eta_sec": 60}'

    job = {"id": "j1", "state": "running", "spec": {"task": "my-goal"},
           "last_activity": "working"}
    out = run(narrate_job(job, stub))
    assert out == {"narration": "Doing my-goal.", "progress": 40, "eta_sec": 60}


def test_narrate_job_swallows_runner_errors():
    async def boom(prompt):
        raise RuntimeError("claude exploded")

    out = run(narrate_job({"id": "j", "state": "running", "spec": {}}, boom))
    assert out == {"narration": "", "progress": None, "eta_sec": None}


# ---------- watch_once ----------


def _canned_runner(output):
    async def run_claude(prompt):
        return output

    return run_claude


def test_watch_once_narrates_running_skips_others():
    calls = []

    def store(job_id, payload):
        calls.append((job_id, payload))

    jobs = [
        {"id": "run", "state": "running", "spec": {"task": "do x"}},
        {"id": "q", "state": "queued", "spec": {"task": "later"}},
        {"id": "done", "state": "succeeded", "spec": {"task": "old"}},
    ]
    runner = _canned_runner('{"narration": "Running fine.", "progress": 25, "eta_sec": 99}')
    n = run(watch_once(jobs, runner, store, now=1000.0))

    assert n == 1
    assert len(calls) == 1
    jid, payload = calls[0]
    assert jid == "run"
    assert payload["narration"] == "Running fine."
    assert payload["progress"] == 25
    assert payload["eta_sec"] == 99
    assert "narrated_at" in payload and isinstance(payload["narrated_at"], float)


def test_watch_once_skips_fresh_jobs():
    calls = []
    jobs = [{"id": "run", "state": "running", "spec": {}, "narrated_at": 995.0}]
    n = run(watch_once(jobs, _canned_runner("{}"), lambda j, p: calls.append(j), now=1000.0))
    assert n == 0
    assert calls == []


def test_watch_once_supports_async_store():
    recorded = []

    async def store(job_id, payload):
        recorded.append(job_id)

    jobs = [{"id": "a", "state": "running", "spec": {}}]
    n = run(watch_once(jobs, _canned_runner('{"narration":"ok","progress":1,"eta_sec":1}'),
                        store, now=1000.0))
    assert n == 1
    assert recorded == ["a"]


def test_watch_once_continues_when_one_store_fails():
    ok = []

    def store(job_id, payload):
        if job_id == "bad":
            raise RuntimeError("db locked")
        ok.append(job_id)

    jobs = [
        {"id": "bad", "state": "running", "spec": {}},
        {"id": "good", "state": "running", "spec": {}},
    ]
    n = run(watch_once(jobs, _canned_runner('{"narration":"x","progress":1,"eta_sec":1}'),
                       store, now=1000.0))
    assert n == 1  # only 'good' counted
    assert ok == ["good"]


def test_watch_once_empty_when_nothing_to_do():
    n = run(watch_once([], _canned_runner("{}"), lambda j, p: None))
    assert n == 0


# ---------- resolve_min_interval (ROOST_NARRATE_INTERVAL) ----------


def test_resolve_interval_default_is_pinned_at_20():
    # Pins today's value EXACTLY: unset must keep the historical 20s cadence.
    assert DEFAULT_MIN_INTERVAL == 20.0
    assert resolve_min_interval(None) == 20.0
    assert resolve_min_interval(None) == DEFAULT_MIN_INTERVAL


def test_resolve_interval_blank_falls_back_to_default():
    assert resolve_min_interval("") == DEFAULT_MIN_INTERVAL
    assert resolve_min_interval("   ") == DEFAULT_MIN_INTERVAL


def test_resolve_interval_accepts_override_above_floor():
    assert resolve_min_interval("90") == 90.0
    assert resolve_min_interval("12.5") == 12.5


def test_resolve_interval_clamps_to_floor():
    # Demos want it fast, but never faster than the sweep cadence.
    assert resolve_min_interval("1") == MIN_INTERVAL_FLOOR
    assert resolve_min_interval("0") == MIN_INTERVAL_FLOOR
    assert resolve_min_interval("-30") == MIN_INTERVAL_FLOOR
    assert MIN_INTERVAL_FLOOR == 5.0


def test_resolve_interval_garbage_falls_back_to_default():
    for bad in ("fast", "20s", "abc", "1e", "nan", "inf", "-inf"):
        assert resolve_min_interval(bad) == DEFAULT_MIN_INTERVAL
