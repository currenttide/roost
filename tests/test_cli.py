"""Tests for the `roost do` router's verdict parsing (pure part)."""
from __future__ import annotations

from roost.cli import _parse_classification

GOAL = "do the thing"


def test_parse_single_clean():
    d = _parse_classification('{"mode":"single","ambiguous":false,"destructive":false,"restated":"X"}', GOAL)
    assert d["mode"] == "single" and d["restated"] == "X"
    assert d["ambiguous"] is False and d["destructive"] is False


def test_parse_multi():
    d = _parse_classification('{"mode":"multi"}', GOAL)
    assert d["mode"] == "multi"


def test_parse_ambiguous_with_question():
    d = _parse_classification(
        '{"mode":"single","ambiguous":true,"clarifying_question":"which repo?"}', GOAL)
    assert d["ambiguous"] is True
    assert d["clarifying_question"] == "which repo?"


def test_parse_destructive():
    d = _parse_classification('{"mode":"single","destructive":true,"restated":"rm -rf X"}', GOAL)
    assert d["destructive"] is True


def test_parse_json_embedded_in_prose():
    d = _parse_classification('Here is my verdict:\n{"mode":"multi","destructive":false}\nDone.', GOAL)
    assert d["mode"] == "multi"


def test_parse_garbage_falls_back_to_safe_default():
    d = _parse_classification("I think you should probably do it, sounds fine", GOAL)
    assert d == {"mode": "single", "ambiguous": False, "clarifying_question": None,
                 "destructive": False, "simple": False, "restated": GOAL}


def test_parse_simple_flag():
    d = _parse_classification('{"mode":"single","simple":true}', GOAL)
    assert d["simple"] is True
    # default is conservative: not simple unless explicitly true
    assert _parse_classification('{"mode":"single"}', GOAL)["simple"] is False


def test_parse_empty():
    d = _parse_classification("", GOAL)
    assert d["mode"] == "single" and d["restated"] == GOAL


# ---------- `roost up` bootstrap helpers (pure) ----------

from roost import bootstrap as boot


def test_build_url_translates_wildcard_bind():
    # 0.0.0.0 isn't a connectable address; advertise loopback instead.
    assert boot.build_url("0.0.0.0", 8787) == "http://127.0.0.1:8787"
    assert boot.build_url("127.0.0.1", 8788) == "http://127.0.0.1:8788"
    assert boot.build_url("192.168.1.5", 9000) == "http://192.168.1.5:9000"


def test_panel_url_includes_token():
    assert boot.panel_url("http://127.0.0.1:8787", "abc") == \
        "http://127.0.0.1:8787/panel?token=abc"
    # trailing slash is normalized, no token → bare panel
    assert boot.panel_url("http://h:1/", "") == "http://h:1/panel"


def test_is_loopback():
    assert boot.is_loopback("127.0.0.1")
    assert boot.is_loopback("0.0.0.0")
    assert boot.is_loopback("localhost")
    assert not boot.is_loopback("192.168.1.5")


def test_config_payload_maps_token_to_credential():
    p = boot.config_payload("http://x:1", "tok", worker_id="w1", name="box")
    # `credential` is what the resolver reads as the bearer token.
    assert p == {"url": "http://x:1", "credential": "tok",
                 "worker_id": "w1", "name": "box"}
    # empty token / no worker → omitted, not stored as empty strings
    assert boot.config_payload("http://x:1", "") == {"url": "http://x:1"}


def test_env_file_text_is_sourceable():
    txt = boot.env_file_text("http://x:1", "tok")
    assert "export ROOST_URL=http://x:1" in txt
    assert "export ROOST_TOKEN=tok" in txt


def test_write_env_file_is_private(tmp_path):
    import os
    p = boot.write_env_file("http://x:1", "tok", path=tmp_path / "env")
    assert p.read_text() == boot.env_file_text("http://x:1", "tok")
    assert (os.stat(p).st_mode & 0o777) == 0o600


# ---------- `roost history` / capabilities discovery (pure) ----------

from roost.cli import (
    _history_outcome,
    _history_row,
    _history_runs,
    _recent_successes,
    _rel_time,
)


def _run(**kw):
    """Minimal derived-run dict with sane defaults, overridable per test."""
    base = {
        "run_id": "abcdef1234567890",
        "goal": "report the OS and free memory",
        "state": "succeeded",
        "health": {"status": "verified"},
        "worker": "w-box",
        "verified": True,
        "cost": {"cost_est_usd": 0.0123, "tokens_used": 1234},
        "created_at": 1000.0,
        "finished_at": 1000.0,
    }
    base.update(kw)
    return base


def test_rel_time_buckets():
    now = 100000.0
    assert _rel_time(now - 5, now) == "5s"
    assert _rel_time(now - 120, now) == "2m"
    assert _rel_time(now - 7200, now) == "2h"
    assert _rel_time(now - 3 * 86400, now) == "3d"
    # missing / unparsable / future
    assert _rel_time(None, now) == "-"
    assert _rel_time("nope", now) == "-"
    assert _rel_time(now + 50, now) == "0s"


def test_history_outcome_verified_failed_done():
    assert _history_outcome(_run())[0] == "verified ✓"
    assert _history_outcome(_run())[1] == "green"
    failed = _run(state="failed", health={"status": "failed"}, verified=None)
    assert _history_outcome(failed) == ("failed ✗", "red")
    cancelled = _run(state="cancelled", health={"status": "cancelled"}, verified=None)
    assert _history_outcome(cancelled) == ("cancelled", "yellow")
    unver = _run(state="succeeded", health={"status": "unverified"}, verified=False)
    assert _history_outcome(unver) == ("unverified ✗", "red")
    done = _run(state="succeeded", health={"status": "done"}, verified=None)
    assert _history_outcome(done) == ("done", None)


def test_history_row_fields():
    now = 2000.0
    row = _history_row(_run(finished_at=now - 60), now)
    short_id, label, color, worker, cost_str, age, goal = row
    assert short_id == "abcdef12"  # truncated to 8
    assert label == "verified ✓" and color == "green"
    assert worker == "w-box"
    assert cost_str == "$0.01"
    assert age == "1m"
    assert goal == "report the OS and free memory"


def test_history_row_no_cost_and_truncation():
    long_goal = "x" * 80
    row = _history_row(_run(cost={"cost_est_usd": 0.0}, goal=long_goal))
    _, _, _, _, cost_str, _, goal = row
    assert cost_str == ""  # zero cost → blank, not "$0.00"
    assert goal.endswith("...") and len(goal) == 52


def test_history_row_missing_fields_never_crashes():
    row = _history_row({})  # everything absent
    short_id, label, color, worker, cost_str, age, goal = row
    assert short_id == "-" and worker == "-" and cost_str == "" and age == "-"
    assert goal == "" and label == "done"


def test_history_runs_filters_terminal_and_goal():
    runs = [
        _run(run_id="a", state="running"),                 # not terminal → out
        _run(run_id="b", state="succeeded", goal=""),      # no goal → out
        _run(run_id="c", state="succeeded"),               # kept
        _run(run_id="d", state="failed", health={"status": "failed"}, verified=None),
    ]
    kept = _history_runs(runs)
    assert [r["run_id"] for r in kept] == ["c", "d"]


def test_history_runs_failed_only():
    runs = [
        _run(run_id="ok", state="succeeded"),
        _run(run_id="bad", state="failed", health={"status": "failed"}, verified=None),
        _run(run_id="unver", state="succeeded", health={"status": "unverified"}, verified=False),
        _run(run_id="cxl", state="cancelled", health={"status": "cancelled"}, verified=None),
    ]
    kept = _history_runs(runs, failed_only=True)
    assert {r["run_id"] for r in kept} == {"bad", "unver", "cxl"}


def test_recent_successes_examples_and_empty():
    runs = [
        _run(run_id="1", state="succeeded", goal="train a model"),
        _run(run_id="2", state="failed", goal="this failed"),
        _run(run_id="3", state="succeeded", goal=""),
        _run(run_id="4", state="succeeded", goal="lint the repo"),
    ]
    assert _recent_successes(runs) == ["train a model", "lint the repo"]
    assert _recent_successes([]) == []
    # truncation of long goals
    long = _recent_successes([_run(goal="y" * 90)])[0]
    assert long.endswith("...") and len(long) == 70
