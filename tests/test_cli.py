"""Tests for the `roost do` router's verdict parsing (pure part)."""
from __future__ import annotations

import json
import time

import click
import pytest
from click.testing import CliRunner

from roost import cli as roost_cli
from roost.cli import (
    _classify_failed,
    _extract_json_object,
    _needs_confirm,
    _parse_classification,
)

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
                 "destructive": False, "simple": False, "restated": GOAL,
                 "classify_failed": False}


def test_parse_simple_flag():
    d = _parse_classification('{"mode":"single","simple":true}', GOAL)
    assert d["simple"] is True
    # default is conservative: not simple unless explicitly true
    assert _parse_classification('{"mode":"single"}', GOAL)["simple"] is False


def test_parse_empty():
    d = _parse_classification("", GOAL)
    assert d["mode"] == "single" and d["restated"] == GOAL
    assert d["classify_failed"] is False


# ---------- fail-CLOSED classification (security-critical) ----------


def test_extract_json_object_balanced_braces():
    # Greedy `{.*}` would grab too much/too little; the balanced scanner gets the
    # first complete object even with nested braces and trailing prose.
    obj = _extract_json_object('noise {"a": {"b": 1}} trailing {"c": 2}')
    assert obj == {"a": {"b": 1}}
    assert _extract_json_object("no json here") is None
    assert _extract_json_object("") is None


def test_classify_failed_demands_confirmation():
    f = _classify_failed(GOAL)
    assert f["classify_failed"] is True
    # Fail-closed: must be treated like destructive so the run is gated.
    assert _needs_confirm(f) is True
    assert f["restated"] == GOAL


def test_needs_confirm_true_for_destructive_and_failed_only():
    assert _needs_confirm({"destructive": True}) is True
    assert _needs_confirm({"classify_failed": True}) is True
    assert _needs_confirm({"destructive": False, "classify_failed": False}) is False


def test_classify_goal_missing_claude_fails_closed(monkeypatch, capsys):
    # No `claude` on PATH → must NOT silently return a safe default.
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda _name: None)
    plan = roost_cli._classify_goal("rm everything")
    assert plan["classify_failed"] is True
    assert _needs_confirm(plan) is True
    err = capsys.readouterr().err
    assert "could not classify" in err and "needs-confirmation" in err


def test_classify_goal_subprocess_error_fails_closed(monkeypatch, capsys):
    import shutil as _sh
    import subprocess as _sp
    monkeypatch.setattr(_sh, "which", lambda _name: "/usr/bin/claude")

    def _boom(*a, **k):
        raise _sp.TimeoutExpired(cmd="claude", timeout=60)

    monkeypatch.setattr(_sp, "run", _boom)
    plan = roost_cli._classify_goal("rm everything")
    assert plan["classify_failed"] is True
    assert "needs-confirmation" in capsys.readouterr().err


def test_classify_goal_non_json_result_fails_closed(monkeypatch, capsys):
    import shutil as _sh
    import subprocess as _sp

    class _P:
        stdout = '{"result": "I am not sure, here is prose with no verdict"}'

    monkeypatch.setattr(_sh, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _P())
    plan = roost_cli._classify_goal("do something")
    assert plan["classify_failed"] is True
    assert "could not classify" in capsys.readouterr().err


def test_classify_goal_verdictless_json_fails_closed(monkeypatch, capsys):
    # A parseable JSON object that LACKS the verdict shape (no mode/destructive/
    # ambiguous/...) must fail closed — not be treated as a safe verdict, which
    # would let a destructive goal run unconfirmed.
    import shutil as _sh
    import subprocess as _sp

    class _P:
        stdout = '{"result": "{\\"status\\":\\"done\\",\\"note\\":\\"ok\\"}"}'

    monkeypatch.setattr(_sh, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _P())
    plan = roost_cli._classify_goal("delete the prod db")
    assert plan["classify_failed"] is True
    assert _needs_confirm(plan) is True
    assert "could not classify" in capsys.readouterr().err


def test_classify_goal_valid_verdict_is_parsed(monkeypatch):
    import shutil as _sh
    import subprocess as _sp

    class _P:
        stdout = '{"result": "{\\"mode\\":\\"single\\",\\"destructive\\":true}"}'

    monkeypatch.setattr(_sh, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _P())
    plan = roost_cli._classify_goal("delete the prod db")
    assert plan["classify_failed"] is False
    assert plan["destructive"] is True


# ---------- `roost do` flow: confirm gating + non-TTY abort ----------


def _run_do(monkeypatch, plan, *, isatty, args, runs=None):
    """Invoke `roost do` with a stubbed classifier + run/dispatch sinks. Returns the
    click Result; `runs` (if a list) collects ('run'|'dispatch', goal) calls."""
    monkeypatch.setattr(roost_cli, "_classify_goal", lambda _g: plan)
    monkeypatch.setattr(roost_cli.sys.stdin, "isatty", lambda: isatty)
    calls = runs if runs is not None else []

    def _fake_run(ctx, *, task, **k):
        calls.append(("run", " ".join(task)))

    def _fake_dispatch(ctx, *, goal, **k):
        calls.append(("dispatch", goal))

    # ctx.invoke(run/dispatch, ...) → route to the sinks.
    real_invoke = click.Context.invoke

    def _invoke(self, cmd, *a, **kw):
        if cmd is roost_cli.run:
            return _fake_run(self, **kw)
        if cmd is roost_cli.dispatch:
            return _fake_dispatch(self, **kw)
        return real_invoke(self, cmd, *a, **kw)

    monkeypatch.setattr(click.Context, "invoke", _invoke)
    return CliRunner().invoke(roost_cli.do_, args), calls


def test_do_nontty_destructive_aborts_without_yes(monkeypatch):
    plan = {"mode": "single", "ambiguous": False, "clarifying_question": None,
            "destructive": True, "simple": False, "restated": "rm -rf /",
            "classify_failed": False}
    res, calls = _run_do(monkeypatch, plan, isatty=False, args=["nuke it"])
    assert res.exit_code != 0
    assert "needs confirmation" in res.output
    assert calls == []  # nothing ran


def test_do_nontty_classify_failed_aborts(monkeypatch):
    plan = _classify_failed("ambiguous risky thing")
    res, calls = _run_do(monkeypatch, plan, isatty=False, args=["risky thing"])
    assert res.exit_code != 0
    assert calls == []


def test_do_nontty_destructive_with_yes_proceeds(monkeypatch):
    plan = {"mode": "single", "ambiguous": False, "clarifying_question": None,
            "destructive": True, "simple": False, "restated": "rm -rf /",
            "classify_failed": False}
    res, calls = _run_do(monkeypatch, plan, isatty=False, args=["--yes", "nuke it"])
    assert res.exit_code == 0
    assert calls == [("run", "nuke it")]


def test_do_yes_destructive_still_warns(monkeypatch):
    # --yes skips the confirmation PROMPT, but the destructive warning must still
    # be emitted — a destructive goal must never run silently.
    plan = {"mode": "single", "ambiguous": False, "clarifying_question": None,
            "destructive": True, "simple": False, "restated": "rm -rf /data",
            "classify_failed": False}
    res, calls = _run_do(monkeypatch, plan, isatty=True, args=["--yes", "nuke it"])
    assert res.exit_code == 0
    assert calls == [("run", "nuke it")]  # it DID run
    assert "looks destructive" in res.output
    assert "rm -rf /data" in res.output


def test_do_nontty_ambiguous_aborts_with_question(monkeypatch):
    plan = {"mode": "single", "ambiguous": True,
            "clarifying_question": "which repo?", "destructive": False,
            "simple": False, "restated": GOAL, "classify_failed": False}
    res, calls = _run_do(monkeypatch, plan, isatty=False, args=["lint it"])
    assert res.exit_code != 0
    assert "which repo?" in res.output
    assert calls == []


def test_do_tty_destructive_declined_aborts(monkeypatch):
    plan = {"mode": "single", "ambiguous": False, "clarifying_question": None,
            "destructive": True, "simple": False, "restated": "rm -rf /",
            "classify_failed": False}
    monkeypatch.setattr(roost_cli, "_classify_goal", lambda _g: plan)
    monkeypatch.setattr(roost_cli.sys.stdin, "isatty", lambda: True)
    # Answer "n" to the confirm prompt → Abort.
    res = CliRunner().invoke(roost_cli.do_, ["nuke it"], input="n\n")
    assert res.exit_code != 0


def test_do_multi_routes_to_dispatch(monkeypatch):
    plan = {"mode": "multi", "ambiguous": False, "clarifying_question": None,
            "destructive": False, "simple": False, "restated": GOAL,
            "classify_failed": False}
    res, calls = _run_do(monkeypatch, plan, isatty=False, args=["a and b and c"])
    assert res.exit_code == 0
    assert calls == [("dispatch", "a and b and c")]


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


def test_history_row_prefers_goal_display():
    # R94: a command job carries a glanceable goal_display summary; the row
    # renders it instead of the raw shell text in `goal`.
    run = _run(goal="ROOST_FOO=1 python -m train --epochs 100 --lr 0.001",
               goal_display="train")
    *_, goal = _history_row(run)
    assert goal == "train"


def test_history_row_falls_back_to_goal_when_no_display():
    # Old-CP safe: a run without goal_display falls back to the raw goal.
    run = _run(goal="report the OS")
    run.pop("goal_display", None)
    *_, goal = _history_row(run)
    assert goal == "report the OS"


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


def test_recent_successes_prefers_goal_display():
    # R94: capabilities examples read the glanceable summary for command jobs,
    # falling back to the raw goal when goal_display is absent (old CP).
    with_display = _run(state="succeeded",
                        goal="ROOST_X=1 python -m foo --bar baz",
                        goal_display="foo")
    without = _run(state="succeeded", goal="lint the repo")
    without.pop("goal_display", None)
    assert _recent_successes([with_display, without]) == ["foo", "lint the repo"]


# ---------- `roost exec`: run a command on one pinned worker ----------

from roost.cli import _resolve_target


def _wk(id_, name, status="idle"):
    return {"id": id_, "name": name, "status": status}


class _ExecResp:
    """Stub httpx response for the exec flow (GET /workers, POST /jobs)."""
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "err"

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _exec_client(workers, job):
    """A fake httpx client usable as `_client(url, token)` context manager that
    serves GET /workers and records the POSTed job body in `job`."""
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, path, **k):
            assert path == "/workers"
            return _ExecResp(workers)
        def post(self, path, json):
            assert path == "/jobs"
            job["body"] = json
            return _ExecResp({"id": "job-x", "state": "queued"})
    return _C()


def test_resolve_target_by_id_wins():
    workers = [_wk("aaa111", "box"), _wk("bbb222", "box")]
    # an exact id match is unambiguous even when names collide
    assert _resolve_target(workers, "aaa111")["id"] == "aaa111"


def test_resolve_target_by_name():
    workers = [_wk("aaa111", "gpu-box"), _wk("bbb222", "cpu-box")]
    assert _resolve_target(workers, "gpu-box")["id"] == "aaa111"


def test_resolve_target_unknown_errors():
    with pytest.raises(click.ClickException) as ei:
        _resolve_target([_wk("aaa111", "box")], "ghost")
    assert "no worker named/ided 'ghost'" in str(ei.value)


def test_resolve_target_ambiguous_online_name_errors():
    workers = [_wk("aaa111", "box", "idle"), _wk("bbb222", "box", "busy")]
    with pytest.raises(click.ClickException) as ei:
        _resolve_target(workers, "box")
    assert "matches 2 workers" in str(ei.value)
    assert "use a worker id" in str(ei.value)


def test_resolve_target_offline_duplicate_does_not_block():
    # one online + one offline of the same name → the online one wins
    workers = [_wk("aaa111", "box", "idle"), _wk("dead", "box", "offline")]
    assert _resolve_target(workers, "box")["id"] == "aaa111"


def test_exec_detach_submits_command_job_with_target(monkeypatch):
    job: dict = {}
    workers = [_wk("aaa111", "gpu-box")]
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://cp", "tok", None))
    monkeypatch.setattr(roost_cli, "_client",
                        lambda url, token: _exec_client(workers, job))
    res = CliRunner().invoke(roost_cli.exec_,
                             ["gpu-box", "--detach", "--", "nvidia-smi"])
    assert res.exit_code == 0
    assert "submitted: job-x" in res.output
    body = job["body"]
    assert body["kind"] == "command"
    assert body["command"] == "nvidia-smi"
    assert body["target"] == "gpu-box"  # PINNED CONTRACT
    assert body["budget"]["max_wallclock_min"] == 2.0


def test_exec_streams_and_exits_with_job_code(monkeypatch):
    job: dict = {}
    workers = [_wk("aaa111", "gpu-box")]
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://cp", "tok", None))
    monkeypatch.setattr(roost_cli, "_client",
                        lambda url, token: _exec_client(workers, job))
    # reuse the submit-and-follow plumbing → _stream returns the exit code
    monkeypatch.setattr(roost_cli, "_stream", lambda url, token, jid, **k: 3)
    res = CliRunner().invoke(roost_cli.exec_, ["gpu-box", "uptime"])
    assert res.exit_code == 3
    assert job["body"]["target"] == "gpu-box"
    assert job["body"]["command"] == "uptime"


def test_exec_unknown_worker_errors_before_submit(monkeypatch):
    job: dict = {}
    workers = [_wk("aaa111", "gpu-box")]
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://cp", "tok", None))
    monkeypatch.setattr(roost_cli, "_client",
                        lambda url, token: _exec_client(workers, job))
    res = CliRunner().invoke(roost_cli.exec_, ["ghost", "--", "ls"])
    assert res.exit_code != 0
    assert "no worker named/ided 'ghost'" in res.output
    assert job == {}  # never submitted


def test_exec_ambiguous_name_errors_before_submit(monkeypatch):
    job: dict = {}
    workers = [_wk("aaa111", "box", "idle"), _wk("bbb222", "box", "busy")]
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://cp", "tok", None))
    monkeypatch.setattr(roost_cli, "_client",
                        lambda url, token: _exec_client(workers, job))
    res = CliRunner().invoke(roost_cli.exec_, ["box", "--", "ls"])
    assert res.exit_code != 0
    assert "use a worker id" in res.output
    assert job == {}


# ---------- roost pair: pairing URI encoding ----------


def test_pair_uri_roundtrips():
    import base64
    import json as _json

    from roost.cli import _pair_uri

    uri = _pair_uri("http://192.168.1.193:8787", "rst-mob-abc123", "yang-iphone")
    assert uri.startswith("roost://pair?d=")
    b64 = uri.split("d=", 1)[1]
    b64 += "=" * (-len(b64) % 4)  # restore stripped padding
    payload = _json.loads(base64.urlsafe_b64decode(b64))
    assert payload == {"v": 1, "url": "http://192.168.1.193:8787",
                       "token": "rst-mob-abc123", "name": "yang-iphone"}


def test_pair_uri_omits_empty_label():
    import base64
    import json as _json

    from roost.cli import _pair_uri

    uri = _pair_uri("http://h:8787", "t", None)
    b64 = uri.split("d=", 1)[1]
    b64 += "=" * (-len(b64) % 4)
    assert "name" not in _json.loads(base64.urlsafe_b64decode(b64))


# ---------- `roost tree`: captain plan reasoning (R33) ----------

import httpx

from roost.cli import _plan_reason


def test_plan_reason_extracts_and_normalizes():
    # Present → returned, stripped and collapsed to one line.
    assert _plan_reason({"spec": {"reason": "  lint first\n(cheap gate)  "}}) == "lint first (cheap gate)"


def test_plan_reason_absent_is_graceful():
    # Older / non-captain jobs: no `reason` key, blank reason, or no/non-dict spec
    # all yield None so the tree renders exactly as before.
    assert _plan_reason({"spec": {"intent": "x"}}) is None
    assert _plan_reason({"spec": {"reason": "   "}}) is None
    assert _plan_reason({"id": "x"}) is None
    assert _plan_reason({"spec": None}) is None


def _tree_via_mock(monkeypatch, jobs: list[dict]):
    """Invoke `roost tree <root>` with the CP `/tree` response stubbed."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/tree")
        return httpx.Response(200, json=jobs)

    monkeypatch.setattr(
        roost_cli, "_ctx_client",
        lambda _ctx: httpx.Client(base_url="http://cp", transport=httpx.MockTransport(handler)),
    )
    return CliRunner().invoke(roost_cli.tree, ["root1"], obj={})


def test_tree_renders_per_child_reason_when_plan_present(monkeypatch):
    jobs = [
        {"id": "root1", "state": "running", "worker_id": None, "parent_job_id": None,
         "intent": "split a goal", "spec": {"intent": "split a goal"}, "tokens_used": 0},
        {"id": "childA", "state": "succeeded", "worker_id": "w-cpu", "parent_job_id": "root1",
         "intent": "lint the repo", "tokens_used": 0,
         "spec": {"command": "ruff .", "reason": "cheap CPU gate, run first"}},
        {"id": "childB", "state": "running", "worker_id": "w-gpu", "parent_job_id": "root1",
         "intent": "run the eval", "tokens_used": 0,
         "spec": {"command": "python eval.py", "reason": "needs the GPU box"}},
    ]
    res = _tree_via_mock(monkeypatch, jobs)
    assert res.exit_code == 0, res.output
    # Each child shows its one-line WHY under the job line.
    assert "↳ why: cheap CPU gate, run first" in res.output
    assert "↳ why: needs the GPU box" in res.output
    # The reason is attached to the right child (appears after its id).
    a_idx = res.output.index("childA")
    assert res.output.index("cheap CPU gate", a_idx) < res.output.index("childB")


def test_tree_without_plan_renders_exactly_as_before(monkeypatch):
    """Graceful absence: no job carries a reason → not a single `why:` line, and
    the existing id/state/worker line is unchanged."""
    jobs = [
        {"id": "root1", "state": "running", "worker_id": None, "parent_job_id": None,
         "intent": "old plan", "spec": {"intent": "old plan"}, "tokens_used": 0},
        {"id": "childA", "state": "succeeded", "worker_id": "w1", "parent_job_id": "root1",
         "intent": "do a thing", "spec": {"command": "true"}, "tokens_used": 0},
    ]
    res = _tree_via_mock(monkeypatch, jobs)
    assert res.exit_code == 0, res.output
    assert "why:" not in res.output
    assert "childA" in res.output and "succeeded" in res.output


def test_tree_mixed_plan_only_annotates_jobs_with_reason(monkeypatch):
    """A partially-annotated tree: only the child that has a reason gets a why line."""
    jobs = [
        {"id": "root1", "state": "running", "worker_id": None, "parent_job_id": None,
         "intent": "plan", "spec": {"intent": "plan"}, "tokens_used": 0},
        {"id": "childA", "state": "succeeded", "worker_id": "w1", "parent_job_id": "root1",
         "intent": "annotated", "spec": {"command": "a", "reason": "the annotated one"},
         "tokens_used": 0},
        {"id": "childB", "state": "succeeded", "worker_id": "w2", "parent_job_id": "root1",
         "intent": "plain", "spec": {"command": "b"}, "tokens_used": 0},
    ]
    res = _tree_via_mock(monkeypatch, jobs)
    assert res.exit_code == 0, res.output
    assert res.output.count("why:") == 1
    assert "↳ why: the annotated one" in res.output


def _tree_health_via_mock(monkeypatch, jobs: list[dict]):
    """`roost tree <root> --health` with the CP `/tree` response stubbed."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/tree")
        return httpx.Response(200, json=jobs)

    monkeypatch.setattr(
        roost_cli, "_ctx_client",
        lambda _ctx: httpx.Client(base_url="http://cp", transport=httpx.MockTransport(handler)),
    )
    return CliRunner().invoke(roost_cli.tree, ["root1", "--health"], obj={})


def test_tree_health_renders_input_counts_for_node_with_input(monkeypatch):
    """`tree --health` shows `inputs N/N/N` for a child that has received input,
    and NOT for a sibling without any (R59, only-when-nonzero)."""
    jobs = [
        {"id": "root1", "state": "running", "worker_id": None, "parent_job_id": None,
         "intent": "plan", "spec": {"intent": "plan"}, "tokens_used": 0},
        {"id": "childA", "state": "running", "worker_id": "w1", "parent_job_id": "root1",
         "intent": "steered", "spec": {"command": "cat"}, "tokens_used": 0,
         "inputs": {"queued": 1, "delivered": 2, "dropped": 0}},
        {"id": "childB", "state": "running", "worker_id": "w2", "parent_job_id": "root1",
         "intent": "plain", "spec": {"command": "true"}, "tokens_used": 0},
    ]
    res = _tree_health_via_mock(monkeypatch, jobs)
    assert res.exit_code == 0, res.output
    assert "inputs 1/2/0" in res.output
    # Exactly one node prints the inputs bit (the sibling without input omits it).
    assert res.output.count("inputs ") == 1


def test_tree_health_omits_inputs_when_zero(monkeypatch):
    """A node carrying an all-zero `inputs` object still omits the bit — the
    only-when-nonzero rule guards the CLI render, not just the server payload."""
    jobs = [
        {"id": "root1", "state": "running", "worker_id": "w1", "parent_job_id": None,
         "intent": "plan", "spec": {"command": "cat"}, "tokens_used": 0,
         "inputs": {"queued": 0, "delivered": 0, "dropped": 0}},
    ]
    res = _tree_health_via_mock(monkeypatch, jobs)
    assert res.exit_code == 0, res.output
    assert "inputs " not in res.output


# ---------- roost send: interactive follow-up (R38) ----------


def _send_client(calls, *, post_status=200, inputs_state="delivered",
                 inputs_detail="written to process stdin"):
    """Fake `_client(url, token)` for the send flow: records the POST /input body
    and serves GET /inputs with a scripted terminal state for --wait."""
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): pass

        def post(self, path, json):
            calls.append(("POST", path, json))
            return _ExecResp({"input_id": "i-123", "job_id": "j1",
                              "state": "queued"}, status_code=post_status)

        def get(self, path, **k):
            calls.append(("GET", path))
            return _ExecResp({"job_id": "j1", "state": "running", "inputs": [
                {"id": "i-123", "state": inputs_state, "detail": inputs_detail}]})
    return _C()


def test_send_queues_input(monkeypatch):
    calls: list = []
    monkeypatch.setattr(roost_cli, "_resolve", lambda ctx: ("http://cp", "tok", None))
    monkeypatch.setattr(roost_cli, "_client", lambda url, token: _send_client(calls))
    res = CliRunner().invoke(roost_cli.send, ["j1", "hello", "there"])
    assert res.exit_code == 0, res.output
    assert "queued input i-123 for job j1" in res.output
    # Words are joined into one message.
    post = [c for c in calls if c[0] == "POST"][0]
    assert post[1] == "/jobs/j1/input"
    assert post[2] == {"text": "hello there"}


def test_send_wait_reports_delivered(monkeypatch):
    calls: list = []
    monkeypatch.setattr(roost_cli, "_resolve", lambda ctx: ("http://cp", "tok", None))
    monkeypatch.setattr(roost_cli, "_client",
                        lambda url, token: _send_client(calls, inputs_state="delivered"))
    monkeypatch.setattr(roost_cli.time, "sleep", lambda *_a: None)  # no real wait
    res = CliRunner().invoke(roost_cli.send, ["j1", "hi", "--wait"])
    assert res.exit_code == 0, res.output
    assert "delivered" in res.output


def test_send_wait_reports_dropped_as_error(monkeypatch):
    calls: list = []
    monkeypatch.setattr(roost_cli, "_resolve", lambda ctx: ("http://cp", "tok", None))
    monkeypatch.setattr(
        roost_cli, "_client",
        lambda url, token: _send_client(calls, inputs_state="dropped",
                                        inputs_detail="kind cannot take stdin"))
    monkeypatch.setattr(roost_cli.time, "sleep", lambda *_a: None)
    res = CliRunner().invoke(roost_cli.send, ["j1", "hi", "--wait"])
    assert res.exit_code != 0
    assert "dropped" in res.output and "kind cannot take stdin" in res.output


def test_send_terminal_job_errors(monkeypatch):
    calls: list = []
    monkeypatch.setattr(roost_cli, "_resolve", lambda ctx: ("http://cp", "tok", None))
    monkeypatch.setattr(roost_cli, "_client",
                        lambda url, token: _send_client(calls, post_status=409))
    res = CliRunner().invoke(roost_cli.send, ["j1", "too late"])
    assert res.exit_code != 0
    assert "terminal" in res.output.lower()


# ---------- `roost status` job-id prefix lookup (R79) ----------


def _status_via_mock(monkeypatch, status_code: int, body: dict):
    """Invoke `roost status <id>` with the CP `GET /jobs/{id}` response stubbed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    monkeypatch.setattr(
        roost_cli, "_ctx_client",
        lambda _ctx: httpx.Client(base_url="http://cp",
                                  transport=httpx.MockTransport(handler)),
    )
    return CliRunner().invoke(roost_cli.status, ["abc123"], obj={})


def test_status_prefix_resolves(monkeypatch):
    """A prefix that the server resolves comes back 200 and renders the full id."""
    res = _status_via_mock(
        monkeypatch, 200, {"id": "abc123def456", "state": "running"})
    assert res.exit_code == 0, res.output
    assert "abc123def456" in res.output


def test_status_ambiguous_prefix_shows_candidates(monkeypatch):
    """A 409 from the server (ambiguous prefix) surfaces the actionable detail
    text — the colliding ids — not a raw httpx traceback."""
    res = _status_via_mock(monkeypatch, 409, {
        "detail": "job id prefix 'abc123' is ambiguous — matches 2 jobs: "
                  "abc123000001, abc123000002. Use more characters."})
    assert res.exit_code != 0
    assert "ambiguous" in res.output
    assert "abc123000001" in res.output and "abc123000002" in res.output
    # No raw exception class leaked.
    assert "HTTPStatusError" not in res.output and "Traceback" not in res.output


def test_status_too_short_prefix_shows_friendly_error(monkeypatch):
    """A 400 (prefix below the floor) surfaces the server's guidance cleanly."""
    res = _status_via_mock(monkeypatch, 400, {
        "detail": "job id prefix too short: need at least 6 chars (got 3); "
                  "paste more of the id"})
    assert res.exit_code != 0
    assert "too short" in res.output
    assert "HTTPStatusError" not in res.output and "Traceback" not in res.output


def test_status_unknown_stays_terse_job_not_found(monkeypatch):
    res = _status_via_mock(monkeypatch, 404, {"detail": "job not found"})
    assert res.exit_code != 0
    assert "job not found" in res.output


# ---------- `roost backup` (R39) ----------


def _backup_factory(app, bearer):
    """Route the backup command's internally-built httpx.Client at an in-process
    control plane via a sync TestClient (an httpx.Client subclass), carrying the
    given bearer token. A fresh TestClient per call matches the command opening
    its client inside a `with` block."""
    from fastapi.testclient import TestClient

    def _factory(*args, **kwargs):
        c = TestClient(app)
        if bearer:
            c.headers.update({"Authorization": f"Bearer {bearer}"})
        return c

    return _factory


def test_backup_writes_db_file(monkeypatch, tmp_path):
    from roost import server

    token = "admin-tok"
    app = server.create_app(
        db_path=tmp_path / "roost.db", token=token, run_sweeper=False
    )
    monkeypatch.setattr(roost_cli, "_resolve", lambda ctx: ("http://cp", token, None))
    monkeypatch.setattr(roost_cli.httpx, "Client", _backup_factory(app, token))

    dest = tmp_path / "out" / "snapshot.db"
    dest.parent.mkdir()
    res = CliRunner().invoke(roost_cli.backup, [str(dest)])
    assert res.exit_code == 0, res.output
    assert "wrote" in res.output and "snapshot.db" in res.output
    # A real, readable SQLite snapshot landed at the destination.
    assert dest.exists() and dest.stat().st_size > 0
    import sqlite3
    conn = sqlite3.connect(dest)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    # No partial-download temp file left behind.
    assert not dest.with_name(dest.name + ".part").exists()


def test_backup_non_admin_token_errors(monkeypatch, tmp_path):
    """A worker/non-admin token is rejected with a clear, actionable message and
    no destination file is created."""
    from fastapi.testclient import TestClient

    from roost import server

    token = "admin-tok"
    app = server.create_app(
        db_path=tmp_path / "roost.db", token=token, run_sweeper=False
    )
    # Mint a worker credential against the same DB, then drive the CLI with it.
    with TestClient(app) as admin:
        admin.headers.update({"Authorization": f"Bearer {token}"})
        etok = admin.post("/enroll-tokens", json={"label": "w"}).json()["token"]
        cred = admin.post(
            "/enroll", json={"token": etok, "name": "w", "capabilities": {}},
            headers={"Authorization": ""},
        ).json()["credential"]

    monkeypatch.setattr(roost_cli, "_resolve", lambda ctx: ("http://cp", cred, None))
    monkeypatch.setattr(roost_cli.httpx, "Client", _backup_factory(app, cred))

    dest = tmp_path / "denied.db"
    res = CliRunner().invoke(roost_cli.backup, [str(dest)])
    assert res.exit_code != 0
    assert "admin token" in res.output.lower()
    assert not dest.exists()


# ======================================================================
# R54 — cli.py coverage lift. Commands that grew since the R16 measure
# with uneven test reach. Style: click runner + httpx MockTransport, no
# real process/socket. `_ctx_client` is stubbed (so config files are
# never read — the R16 isolation lesson) and the recorded requests pin
# the wire shape, not just the rendered output.
# ======================================================================


class _Recorder:
    """A captured HTTP exchange: the request + the canned response to send."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def last(self, method: str | None = None) -> httpx.Request:
        reqs = self.requests if method is None else \
            [r for r in self.requests if r.method == method]
        assert reqs, f"no {method or 'any'} request was made"
        return reqs[-1]


def _mock_ctx(monkeypatch, routes: dict, rec: _Recorder | None = None,
              *, base="http://cp"):
    """Route `roost_cli._ctx_client(ctx)` at an httpx.MockTransport.

    `routes` maps "<METHOD> <path>" → an httpx.Response OR a callable
    (request) -> httpx.Response. Every request is recorded. Because we
    replace `_ctx_client` wholesale, `_resolve`/`config.load()` never run,
    so no real config file or ROOST_URL/ROOST_TOKEN env is ever read.
    """
    rec = rec if rec is not None else _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        key = f"{request.method} {request.url.path}"
        if key not in routes:
            raise AssertionError(f"unexpected request: {key}")
        spec = routes[key]
        return spec(request) if callable(spec) else spec

    monkeypatch.setattr(
        roost_cli, "_ctx_client",
        lambda _ctx: httpx.Client(base_url=base,
                                  transport=httpx.MockTransport(handler)),
    )
    return rec


# ---------- `roost prune-workers` (admin ghost-row cleanup) ----------


def _worker_row(id_, name, *, last_seen, status="offline"):
    return {"id": id_, "name": name, "status": status, "last_seen": last_seen,
            "capabilities": {}}


def test_prune_workers_lists_victims_and_posts_after_confirm(monkeypatch):
    now = time.time()
    workers = [
        _worker_row("live1", "alive", last_seen=now, status="idle"),  # recent → spared
        _worker_row("dead1", "ghost-a", last_seen=now - 30 * 86400),  # stale → victim
        _worker_row("dead2", "ghost-b", last_seen=now - 10 * 86400),  # stale → victim
    ]
    rec = _mock_ctx(monkeypatch, {
        "GET /workers": httpx.Response(200, json=workers),
        "POST /workers/prune": httpx.Response(
            200, json={"pruned": 2, "names": ["ghost-a", "ghost-b"]}),
    })
    # confirm "y" → it proceeds and POSTs.
    res = CliRunner().invoke(roost_cli.prune_workers_cmd, ["--days", "7"],
                             input="y\n", obj={})
    assert res.exit_code == 0, res.output
    assert "will prune 2 worker row(s)" in res.output
    # The recent node must NOT be listed; both ghosts must be.
    assert "alive" not in res.output
    assert "ghost-a" in res.output and "ghost-b" in res.output
    assert "pruned 2 worker(s): ghost-a, ghost-b" in res.output
    # The cutoff is passed through as older_than_days (the load-bearing param).
    prune = rec.last("POST")
    assert dict(prune.url.params)["older_than_days"] == "7.0"


def test_prune_workers_nothing_to_prune_skips_post(monkeypatch):
    now = time.time()
    rec = _mock_ctx(monkeypatch, {
        "GET /workers": httpx.Response(
            200, json=[_worker_row("live", "fresh", last_seen=now, status="idle")]),
    })
    res = CliRunner().invoke(roost_cli.prune_workers_cmd, [], obj={})
    assert res.exit_code == 0, res.output
    assert "nothing to prune" in res.output
    # The POST must never be issued when there are no victims.
    assert [r for r in rec.requests if r.method == "POST"] == []


def test_prune_workers_decline_at_prompt_aborts_without_post(monkeypatch):
    now = time.time()
    rec = _mock_ctx(monkeypatch, {
        "GET /workers": httpx.Response(
            200, json=[_worker_row("dead", "ghost", last_seen=now - 99 * 86400)]),
    })
    res = CliRunner().invoke(roost_cli.prune_workers_cmd, [], input="n\n", obj={})
    assert res.exit_code != 0  # click.confirm(abort=True)
    assert [r for r in rec.requests if r.method == "POST"] == []


def test_prune_workers_yes_skips_prompt(monkeypatch):
    now = time.time()
    rec = _mock_ctx(monkeypatch, {
        "GET /workers": httpx.Response(
            200, json=[_worker_row("dead", "ghost", last_seen=now - 99 * 86400)]),
        "POST /workers/prune": httpx.Response(
            200, json={"pruned": 1, "names": ["ghost"]}),
    })
    # No input supplied: with --yes the prompt must be skipped entirely.
    res = CliRunner().invoke(roost_cli.prune_workers_cmd, ["--yes"], obj={})
    assert res.exit_code == 0, res.output
    assert "pruned 1 worker(s): ghost" in res.output
    assert rec.last("POST").url.path == "/workers/prune"


def test_prune_workers_403_is_actionable(monkeypatch):
    now = time.time()
    _mock_ctx(monkeypatch, {
        "GET /workers": httpx.Response(
            200, json=[_worker_row("dead", "ghost", last_seen=now - 99 * 86400)]),
        "POST /workers/prune": httpx.Response(403, json={"detail": "nope"}),
    })
    res = CliRunner().invoke(roost_cli.prune_workers_cmd, ["--yes"], obj={})
    assert res.exit_code != 0
    assert "admin auth required" in res.output


# ---------- `roost capabilities` (plain-language fleet discovery) ----------


def _cap_worker(name, caps, status="idle"):
    return {"id": f"id-{name}", "name": name, "status": status,
            "capabilities": caps}


def test_capabilities_no_online_workers(monkeypatch):
    _mock_ctx(monkeypatch, {
        # one offline node → not "live", so the empty-fleet hint shows.
        "GET /workers": httpx.Response(
            200, json=[_cap_worker("box", {"cpus": 4}, status="offline")]),
        "GET /derived": httpx.Response(200, json={"runs": []}),
    })
    res = CliRunner().invoke(roost_cli.capabilities, [], obj={})
    assert res.exit_code == 0, res.output
    assert "No online workers" in res.output


def test_capabilities_summarizes_cpu_gpu_docker_and_examples(monkeypatch):
    workers = [
        _cap_worker("cpu-box", {"cpus": 8}),
        _cap_worker("gpu-box", {"cpus": 16, "gpu_count": 2,
                                "gpu": ["NVIDIA A100"], "gpu_vram_gb": 80,
                                "docker": True}),
    ]
    runs = [
        {"run_id": "r1", "goal": "train a tiny model", "state": "succeeded",
         "health": {"status": "verified"}, "verified": True},
    ]
    _mock_ctx(monkeypatch, {
        "GET /workers": httpx.Response(200, json=workers),
        "GET /derived": httpx.Response(200, json={"runs": runs}),
    })
    res = CliRunner().invoke(roost_cli.capabilities, [], obj={})
    assert res.exit_code == 0, res.output
    # cores summed across live nodes; GPU node counted.
    assert "2 nodes" in res.output and "24 CPU cores" in res.output
    assert "1 GPU node(s)" in res.output
    # The "NVIDIA " prefix is stripped; count + VRAM rendered.
    assert "gpu-box: 2× A100 (80GB)" in res.output
    # docker present → the container clause is shown (not the bare period one).
    assert "in isolated containers." in res.output
    # GPU example line only appears when there's a GPU node.
    assert 'report the GPU model and free VRAM' in res.output
    # real recent successes are surfaced.
    assert "train a tiny model" in res.output


def test_capabilities_flags_gpu_detection_failed(monkeypatch):
    # [R41] nvidia-smi present but probe errored → an operator must see a BROKEN
    # node, distinct from a genuinely bare one.
    workers = [
        _cap_worker("bare", {"cpus": 4}),
        _cap_worker("broken", {"cpus": 4, "gpu_detection": "failed"}),
    ]
    _mock_ctx(monkeypatch, {
        "GET /workers": httpx.Response(200, json=workers),
        "GET /derived": httpx.Response(200, json={"runs": []}),
    })
    res = CliRunner().invoke(roost_cli.capabilities, [], obj={})
    assert res.exit_code == 0, res.output
    assert "GPU detection FAILED on 1 node(s): broken" in res.output
    assert "check the driver" in res.output
    # A pure-CPU fleet shows the no-container clause.
    assert "GPU / training jobs." in res.output


def test_capabilities_unreachable_cp_errors(monkeypatch):
    def boom(_req):
        raise httpx.ConnectError("refused")
    _mock_ctx(monkeypatch, {"GET /workers": boom})
    res = CliRunner().invoke(roost_cli.capabilities, [], obj={})
    assert res.exit_code != 0
    assert "cannot reach control plane" in res.output


# ---------- `roost history` (what the fleet has run) ----------


def _drow(**kw):
    base = {"run_id": "deadbeefcafef00d", "goal": "lint the repo",
            "state": "succeeded", "health": {"status": "verified"},
            "worker": "w-box", "verified": True,
            "cost": {"cost_est_usd": 0.02, "tokens_used": 500},
            "created_at": 1000.0, "finished_at": 1000.0}
    base.update(kw)
    return base


def test_history_renders_table(monkeypatch):
    runs = [_drow(run_id="aaaaaaaa1111", goal="lint the repo")]
    rec = _mock_ctx(monkeypatch, {
        "GET /derived": httpx.Response(200, json={"runs": runs}),
    })
    res = CliRunner().invoke(roost_cli.history_cmd, [], obj={})
    assert res.exit_code == 0, res.output
    assert "aaaaaaaa" in res.output       # id truncated to 8
    assert "verified" in res.output
    assert "w-box" in res.output
    assert "lint the repo" in res.output
    # Over-fetch contract: history asks for more than the display limit so
    # filtering to real/terminal runs still fills the page.
    assert int(dict(rec.last("GET").url.params)["limit"]) > 20


def test_history_failed_filter_passthrough(monkeypatch):
    runs = [
        _drow(run_id="ok", goal="that worked", state="succeeded"),
        _drow(run_id="bad", goal="that broke", state="failed",
              health={"status": "failed"}, verified=None),
    ]
    _mock_ctx(monkeypatch, {
        "GET /derived": httpx.Response(200, json={"runs": runs}),
    })
    res = CliRunner().invoke(roost_cli.history_cmd, ["--failed"], obj={})
    assert res.exit_code == 0, res.output
    # --failed keeps only the failed run; the succeeded one is filtered out.
    assert "that broke" in res.output
    assert "that worked" not in res.output


def test_history_failed_empty_message(monkeypatch):
    _mock_ctx(monkeypatch, {
        "GET /derived": httpx.Response(
            200, json={"runs": [_drow(state="succeeded")]}),
    })
    res = CliRunner().invoke(roost_cli.history_cmd, ["--failed"], obj={})
    assert res.exit_code == 0, res.output
    assert "(none failed)" in res.output


def test_history_limit_caps_rows(monkeypatch):
    runs = [_drow(run_id=f"r{i}", goal=f"goal number {i}") for i in range(10)]
    _mock_ctx(monkeypatch, {
        "GET /derived": httpx.Response(200, json={"runs": runs}),
    })
    res = CliRunner().invoke(roost_cli.history_cmd, ["--limit", "3"], obj={})
    assert res.exit_code == 0, res.output
    # Exactly 3 run rows are printed despite 10 available.
    assert sum(1 for ln in res.output.splitlines() if "goal number" in ln) == 3


def test_history_json_emits_selected(monkeypatch):
    runs = [_drow(run_id="aaaaaaaa1111", goal="json me")]
    _mock_ctx(monkeypatch, {
        "GET /derived": httpx.Response(200, json={"runs": runs}),
    })
    res = CliRunner().invoke(roost_cli.history_cmd, ["--json"], obj={})
    assert res.exit_code == 0, res.output
    parsed = json.loads(res.output)
    assert isinstance(parsed, list) and parsed[0]["run_id"] == "aaaaaaaa1111"


def test_history_cp_error_is_actionable(monkeypatch):
    _mock_ctx(monkeypatch, {
        "GET /derived": httpx.Response(500, text="boom"),
    })
    res = CliRunner().invoke(roost_cli.history_cmd, [], obj={})
    assert res.exit_code != 0
    assert "control plane error" in res.output


# ---------- `roost workers` (fleet listing, incl. R41 detection-failed) ----------


def test_workers_lists_with_summary(monkeypatch):
    now = time.time()
    workers = [{
        "id": "w1", "name": "gpu-box", "status": "busy", "last_seen": now,
        "capabilities": {"hostname": "host-1", "gpu_vram_gb": 80, "arch": "x86_64",
                         "load": {"running": 2, "free_vram_gb": 40, "loadavg1": 1.5}},
    }]
    _mock_ctx(monkeypatch, {"GET /workers": httpx.Response(200, json=workers)})
    res = CliRunner().invoke(roost_cli.list_workers_cmd, [], obj={})
    assert res.exit_code == 0, res.output
    assert "gpu-box" in res.output and "busy" in res.output
    assert "host-1" in res.output and "gpu:80GB" in res.output
    assert "x86_64" in res.output
    assert "running:2" in res.output and "vramfree:40GB" in res.output
    assert "load:1.5" in res.output


def test_workers_renders_detection_failed_flag(monkeypatch):
    # [R41] gpu_detection=failed and NO gpu_vram_gb → the DETECTION-FAILED flag
    # is shown (the elif branch), not a silent bare node.
    now = time.time()
    workers = [{
        "id": "w1", "name": "broken", "status": "idle", "last_seen": now,
        "capabilities": {"hostname": "h", "gpu_detection": "failed"},
    }]
    _mock_ctx(monkeypatch, {"GET /workers": httpx.Response(200, json=workers)})
    res = CliRunner().invoke(roost_cli.list_workers_cmd, [], obj={})
    assert res.exit_code == 0, res.output
    assert "gpu:DETECTION-FAILED" in res.output


def test_workers_vram_present_suppresses_detection_failed(monkeypatch):
    # When VRAM IS known, the working GPU summary wins — the failed flag is the
    # elif, so it must NOT also appear.
    now = time.time()
    workers = [{
        "id": "w1", "name": "ok-gpu", "status": "idle", "last_seen": now,
        "capabilities": {"gpu_vram_gb": 24, "gpu_detection": "failed"},
    }]
    _mock_ctx(monkeypatch, {"GET /workers": httpx.Response(200, json=workers)})
    res = CliRunner().invoke(roost_cli.list_workers_cmd, [], obj={})
    assert res.exit_code == 0, res.output
    assert "gpu:24GB" in res.output
    assert "DETECTION-FAILED" not in res.output


# ---------- `roost schedule` + _fmt_interval ----------


def test_fmt_interval_units():
    from roost.cli import _fmt_interval
    assert _fmt_interval(30) == "30s"
    assert _fmt_interval(300) == "5m"
    assert _fmt_interval(6 * 3600) == "6h"
    assert _fmt_interval(86400) == "1d"
    # Not a whole multiple of a larger unit → falls through to seconds.
    assert _fmt_interval(90) == "90s"
    assert _fmt_interval(3601) == "3601s"


def _sched_row(**kw):
    base = {"id": "sch-1", "name": "nightly", "spec": {"task": "check disk"},
            "interval_sec": 21600, "enabled": True,
            "next_run_at": time.time() + 3600, "last_run_at": None,
            "last_job_id": None, "created_at": time.time()}
    base.update(kw)
    return base


def test_schedule_list_renders_rows(monkeypatch):
    rows = [
        _sched_row(id="sch-on", enabled=True, interval_sec=21600,
                   spec={"task": "check disk space"}),
        _sched_row(id="sch-off", name="weekly", enabled=False,
                   interval_sec=86400, spec={"command": "backup.sh"}),
    ]
    _mock_ctx(monkeypatch, {"GET /schedules": httpx.Response(200, json=rows)})
    res = CliRunner().invoke(roost_cli.schedule, ["--list"], obj={})
    assert res.exit_code == 0, res.output
    assert "sch-on  [on ] every 6h" in res.output
    assert "sch-off  [OFF] every 1d" in res.output
    assert "check disk space" in res.output
    assert "backup.sh" in res.output  # command spec used when no task/intent


def test_schedule_list_empty(monkeypatch):
    _mock_ctx(monkeypatch, {"GET /schedules": httpx.Response(200, json=[])})
    res = CliRunner().invoke(roost_cli.schedule, ["--list"], obj={})
    assert res.exit_code == 0, res.output
    assert "no schedules" in res.output


def test_schedule_create_from_goal_posts_auto_task(monkeypatch):
    rec = _mock_ctx(monkeypatch, {
        "POST /schedules": httpx.Response(
            200, json=_sched_row(id="sch-new", interval_sec=1800)),
    })
    res = CliRunner().invoke(
        roost_cli.schedule, ["check disk space", "--every", "30m"], obj={})
    assert res.exit_code == 0, res.output
    assert "scheduled sch-new: every 30m, first run in 30m" in res.output
    body = json.loads(rec.last("POST").content)
    # The roost-do shape: a kind:auto task carrying the goal + the interval.
    assert body["spec"] == {"kind": "auto", "task": "check disk space"}
    assert body["every"] == "30m"


def test_schedule_create_requires_every(monkeypatch):
    _mock_ctx(monkeypatch, {})  # no request should be made
    res = CliRunner().invoke(roost_cli.schedule, ["do a thing"], obj={})
    assert res.exit_code != 0
    assert "give --every" in res.output


def test_schedule_create_requires_goal_or_spec(monkeypatch):
    _mock_ctx(monkeypatch, {})
    res = CliRunner().invoke(roost_cli.schedule, ["--every", "30m"], obj={})
    assert res.exit_code != 0
    assert "give a goal or --spec" in res.output


def test_schedule_rm_deletes(monkeypatch):
    rec = _mock_ctx(monkeypatch, {
        "DELETE /schedules/sch-x": httpx.Response(200, json={"ok": True}),
    })
    res = CliRunner().invoke(roost_cli.schedule, ["--rm", "sch-x"], obj={})
    assert res.exit_code == 0, res.output
    assert "deleted sch-x" in res.output
    assert rec.last("DELETE").url.path == "/schedules/sch-x"


def test_schedule_rm_missing_404(monkeypatch):
    _mock_ctx(monkeypatch, {
        "DELETE /schedules/ghost": httpx.Response(404, json={"detail": "no"}),
    })
    res = CliRunner().invoke(roost_cli.schedule, ["--rm", "ghost"], obj={})
    assert res.exit_code != 0
    assert "schedule not found" in res.output


def test_schedule_enable_patches_true(monkeypatch):
    rec = _mock_ctx(monkeypatch, {
        "PATCH /schedules/sch-x": httpx.Response(
            200, json=_sched_row(id="sch-x", enabled=True, interval_sec=3600)),
    })
    res = CliRunner().invoke(roost_cli.schedule, ["--enable", "sch-x"], obj={})
    assert res.exit_code == 0, res.output
    assert "sch-x enabled — next run in 1h" in res.output
    assert json.loads(rec.last("PATCH").content) == {"enabled": True}


def test_schedule_disable_patches_false(monkeypatch):
    rec = _mock_ctx(monkeypatch, {
        "PATCH /schedules/sch-x": httpx.Response(
            200, json=_sched_row(id="sch-x", enabled=False)),
    })
    res = CliRunner().invoke(roost_cli.schedule, ["--disable", "sch-x"], obj={})
    assert res.exit_code == 0, res.output
    assert "sch-x disabled" in res.output
    # disable → enabled:false must be sent (mutation: a swap would send true).
    assert json.loads(rec.last("PATCH").content) == {"enabled": False}


def test_schedule_create_server_error(monkeypatch):
    _mock_ctx(monkeypatch, {
        "POST /schedules": httpx.Response(400, text="bad interval"),
    })
    res = CliRunner().invoke(
        roost_cli.schedule, ["a goal", "--every", "1s"], obj={})
    assert res.exit_code != 0
    assert "schedule failed: HTTP 400" in res.output


# R77: non-2xx on the read/mutate subverbs must surface a friendly one-line
# error (parity with the create path's wording) — never a raw httpx traceback.
# The motivating case: --list against a control plane without /schedules.


def _assert_clean_error(res, code: str) -> None:
    # A friendly ClickException → non-zero exit, no leaked traceback class name.
    assert res.exit_code != 0
    assert f"schedule failed: HTTP {code}" in res.output
    assert "Traceback" not in res.output
    assert "HTTPStatusError" not in res.output


def test_schedule_list_404_friendly_error(monkeypatch):
    # The verbatim user-test case: old CP with no /schedules route → 404.
    _mock_ctx(monkeypatch, {
        "GET /schedules": httpx.Response(404, text="Not Found"),
    })
    res = CliRunner().invoke(roost_cli.schedule, ["--list"], obj={})
    _assert_clean_error(res, "404")


def test_schedule_list_500_friendly_error(monkeypatch):
    _mock_ctx(monkeypatch, {
        "GET /schedules": httpx.Response(500, text="boom"),
    })
    res = CliRunner().invoke(roost_cli.schedule, ["--list"], obj={})
    _assert_clean_error(res, "500")


def test_schedule_rm_500_friendly_error(monkeypatch):
    # 404 stays the dedicated "schedule not found"; other non-2xx → friendly.
    _mock_ctx(monkeypatch, {
        "DELETE /schedules/sch-x": httpx.Response(500, text="boom"),
    })
    res = CliRunner().invoke(roost_cli.schedule, ["--rm", "sch-x"], obj={})
    _assert_clean_error(res, "500")


def test_schedule_enable_500_friendly_error(monkeypatch):
    _mock_ctx(monkeypatch, {
        "PATCH /schedules/sch-x": httpx.Response(500, text="boom"),
    })
    res = CliRunner().invoke(roost_cli.schedule, ["--enable", "sch-x"], obj={})
    _assert_clean_error(res, "500")


def test_schedule_disable_500_friendly_error(monkeypatch):
    _mock_ctx(monkeypatch, {
        "PATCH /schedules/sch-x": httpx.Response(500, text="boom"),
    })
    res = CliRunner().invoke(roost_cli.schedule, ["--disable", "sch-x"], obj={})
    _assert_clean_error(res, "500")


# ---------- `roost publish --list` (R-era pagination via X-Total-Count) ----------


def _site(slug, files=3, size=2048):
    return {"slug": slug, "files": files, "size": size,
            "url": f"http://cp/pub/{slug}/", "created_at": 0, "updated_at": 0}


def test_publish_list_renders_sites(monkeypatch):
    rec = _mock_ctx(monkeypatch, {
        "GET /publish": httpx.Response(
            200, json=[_site("demo", files=4, size=4096)]),
    })
    res = CliRunner().invoke(roost_cli.publish, ["--list"], obj={})
    assert res.exit_code == 0, res.output
    assert "demo" in res.output
    assert "4 files" in res.output
    assert "4 KB" in res.output                       # 4096/1024
    assert "http://cp/pub/demo/" in res.output


def test_publish_list_empty(monkeypatch):
    _mock_ctx(monkeypatch, {"GET /publish": httpx.Response(200, json=[])})
    res = CliRunner().invoke(roost_cli.publish, ["--list"], obj={})
    assert res.exit_code == 0, res.output
    assert "no published sites" in res.output


def test_publish_list_limit_offset_passthrough_and_more_hint(monkeypatch):
    rec = _Recorder()

    def handler(req):
        # X-Total-Count says there are 100; we returned 2 from offset 5 → the
        # "more" hint should advise --offset 7.
        return httpx.Response(200, json=[_site("a"), _site("b")],
                              headers={"X-Total-Count": "100"})
    _mock_ctx(monkeypatch, {"GET /publish": handler}, rec)
    res = CliRunner().invoke(
        roost_cli.publish, ["--list", "--limit", "2", "--offset", "5"], obj={})
    assert res.exit_code == 0, res.output
    params = dict(rec.last("GET").url.params)
    # Both pagination params reach the server (load-bearing passthrough).
    assert params["limit"] == "2" and params["offset"] == "5"
    assert "showing 2 of 100" in res.output
    assert "--offset 7 for more" in res.output  # 5 offset + 2 shown


def test_publish_list_no_more_hint_when_all_shown(monkeypatch):
    rec = _Recorder()

    def handler(req):
        return httpx.Response(200, json=[_site("only")],
                              headers={"X-Total-Count": "1"})
    _mock_ctx(monkeypatch, {"GET /publish": handler}, rec)
    res = CliRunner().invoke(roost_cli.publish, ["--list"], obj={})
    assert res.exit_code == 0, res.output
    # total == shown → no pagination hint.
    assert "for more" not in res.output
    # Default list omits limit/offset params (server applies its own default).
    params = dict(rec.last("GET").url.params)
    assert "limit" not in params and "offset" not in params


def test_publish_list_403_is_actionable(monkeypatch):
    _mock_ctx(monkeypatch, {
        "GET /publish": httpx.Response(403, json={"detail": "no"}),
    })
    res = CliRunner().invoke(roost_cli.publish, ["--list"], obj={})
    assert res.exit_code != 0
    assert "admin auth required to list sites" in res.output


# ---------- `roost publish <dir>`: R78 cross-version compatibility ----------
# The one-shot raw-tar POST /publish is new (R7). Against an OLDER control
# plane (the deployed 0.1.0) the raw body is parsed as JSON and the server
# 500s; the CLI must degrade to the two-step blob flow on ANY non-2xx from
# the one-shot — not only on 422 — and must NOT silently mask a genuine
# new-CP error (if the fallback also fails, the original error is surfaced).


def _site_full(slug="hello", files=1, size=512):
    return {"slug": slug, "files": files, "size": size,
            "url": f"http://cp/pub/{slug}/", "created_at": 0, "updated_at": 0}


def _publish_dir(tmp_path):
    """A minimal built site directory to publish."""
    d = tmp_path / "site"
    d.mkdir()
    (d / "index.html").write_text("<h1>hi</h1>")
    return d


def _mock_publish(monkeypatch, routes, rec=None):
    """Like `_mock_ctx` but also stubs `_resolve` so the publish command's
    loopback-note lookup doesn't read real config/env."""
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda _ctx: ("http://cp", "tok", None))
    return _mock_ctx(monkeypatch, routes, rec)


def test_publish_oneshot_500_falls_back_to_blob_flow(tmp_path, monkeypatch):
    # Old CP (0.1.0): the raw-tar one-shot POST 500s (it tries to parse the
    # gzip body as JSON). The CLI must fall back to staging a blob then
    # publishing by blob_id, and succeed.
    rec = _Recorder()
    state = {"oneshot_seen": False}

    def publish_handler(req):
        # JSON body → two-step path (success); raw body → old-CP 500.
        ctype = req.headers.get("content-type", "")
        if "application/json" in ctype:
            return httpx.Response(200, json=_site_full())
        state["oneshot_seen"] = True
        return httpx.Response(500, text="Internal Server Error")

    _mock_publish(monkeypatch, {
        "POST /publish": publish_handler,
        "POST /blobs": httpx.Response(200, json={"id": "blob-1"}),
    }, rec)
    res = CliRunner().invoke(
        roost_cli.publish, [str(_publish_dir(tmp_path)), "--name", "hello"],
        obj={})
    assert res.exit_code == 0, res.output
    assert state["oneshot_seen"]                       # one-shot was tried first
    assert "live:" in res.output
    # The blob flow ran: a /blobs upload then a JSON /publish.
    assert any(r.method == "POST" and r.url.path == "/blobs"
               for r in rec.requests)


def test_publish_oneshot_404_falls_back_to_blob_flow(tmp_path, monkeypatch):
    # Endpoint shape differs on an old CP / missing route → still recover.
    rec = _Recorder()

    def publish_handler(req):
        if "application/json" in req.headers.get("content-type", ""):
            return httpx.Response(200, json=_site_full())
        return httpx.Response(404, text="Not Found")

    _mock_publish(monkeypatch, {
        "POST /publish": publish_handler,
        "POST /blobs": httpx.Response(200, json={"id": "blob-2"}),
    }, rec)
    res = CliRunner().invoke(
        roost_cli.publish, [str(_publish_dir(tmp_path)), "--name", "hello"],
        obj={})
    assert res.exit_code == 0, res.output
    assert "live:" in res.output


def test_publish_oneshot_500_then_fallback_also_fails_surfaces_both(
        tmp_path, monkeypatch):
    # A GENUINE new-CP failure: the one-shot 500s AND the blob flow also
    # fails. The CLI must not pretend it worked — it surfaces the original
    # one-shot error, not just the fallback's, so a real server bug isn't
    # masked behind a confusing blob-flow message.
    def publish_handler(req):
        # Both paths fail (same broken server).
        return httpx.Response(500, text="boom-oneshot")

    _mock_publish(monkeypatch, {
        "POST /publish": publish_handler,
        "POST /blobs": httpx.Response(500, text="boom-blob"),
    })
    res = CliRunner().invoke(
        roost_cli.publish, [str(_publish_dir(tmp_path)), "--name", "hello"],
        obj={})
    assert res.exit_code != 0
    # The original one-shot failure is reported (not silently swallowed).
    assert "500" in res.output
    assert "boom-oneshot" in res.output
    # And it's clear no path worked.
    assert "publish failed" in res.output.lower()


def test_publish_oneshot_500_blob_ok_but_publish_json_fails_surfaces_both(
        tmp_path, monkeypatch):
    # Fallback gets further — the blob upload succeeds — but the second,
    # JSON /publish step still fails. The original one-shot error must still
    # lead, with the fallback's failure appended (no masking, no success claim).
    def publish_handler(req):
        # Both the raw one-shot and the JSON two-step return non-2xx here.
        return httpx.Response(500, text="boom-oneshot")

    _mock_publish(monkeypatch, {
        "POST /publish": publish_handler,
        "POST /blobs": httpx.Response(200, json={"id": "blob-3"}),
    })
    res = CliRunner().invoke(
        roost_cli.publish, [str(_publish_dir(tmp_path)), "--name", "hello"],
        obj={})
    assert res.exit_code != 0
    assert "boom-oneshot" in res.output            # original one-shot error leads
    assert "publish failed" in res.output.lower()
    assert "blob flow" in res.output.lower()       # fallback failure noted


def test_publish_loses_oneshot_err_on_fallback_connect_error(
        tmp_path, monkeypatch):
    # R90: the one-shot 500s (a real, diagnostic server error) and the blob
    # fallback POST then raises a TRANSPORT exception (connection refused
    # mid-publish) — NOT an HTTP status. The R78 contract (cli docstring:
    # "if both paths fail you see the original error") must hold even when the
    # fallback dies at the transport layer: the original one-shot diagnosis
    # must still surface in a clean ClickException, not a bare httpx traceback.
    rec = _Recorder()

    def blobs_handler(req):
        raise httpx.ConnectError("Connection refused", request=req)

    _mock_publish(monkeypatch, {
        "POST /publish": httpx.Response(500, text="ONESHOT-BOOM: schema mismatch"),
        "POST /blobs": blobs_handler,
    }, rec)
    res = CliRunner().invoke(
        roost_cli.publish, [str(_publish_dir(tmp_path)), "--name", "hello"],
        obj={})
    assert res.exit_code != 0
    # The original one-shot error must still be visible (not lost behind the
    # transport exception), and the fallback failure noted — no raw traceback.
    assert "ONESHOT-BOOM" in res.output, (
        "publish lost the original one-shot error when the blob fallback "
        f"raised a transport exception. Output was:\n{res.output}\n"
        f"Exception: {res.exception!r}")
    assert "publish failed" in res.output.lower()
    assert "blob flow" in res.output.lower()       # fallback failure noted


def test_publish_loses_oneshot_err_on_publish_json_connect_error(
        tmp_path, monkeypatch):
    # R90 (second fallback leg): the blob upload succeeds but the second,
    # JSON /publish POST raises a transport exception. The one-shot error must
    # still surface (same contract as the blob-leg transport failure above).
    rec = _Recorder()

    def publish_handler(req):
        if "application/json" in req.headers.get("content-type", ""):
            raise httpx.ConnectError("Connection refused", request=req)
        return httpx.Response(500, text="ONESHOT-BOOM: schema mismatch")

    _mock_publish(monkeypatch, {
        "POST /publish": publish_handler,
        "POST /blobs": httpx.Response(200, json={"id": "blob-9"}),
    }, rec)
    res = CliRunner().invoke(
        roost_cli.publish, [str(_publish_dir(tmp_path)), "--name", "hello"],
        obj={})
    assert res.exit_code != 0
    assert "ONESHOT-BOOM" in res.output, (
        "publish lost the original one-shot error when the JSON /publish "
        f"fallback raised a transport exception. Output was:\n{res.output}\n"
        f"Exception: {res.exception!r}")
    assert "publish failed" in res.output.lower()
    assert "blob flow" in res.output.lower()


def test_publish_oneshot_403_is_actionable_no_fallback(tmp_path, monkeypatch):
    # A 403 is an auth problem, not a version mismatch — surface the admin
    # hint directly; the blob flow would just 403 again.
    called = {"blobs": False}

    def blobs_handler(req):
        called["blobs"] = True
        return httpx.Response(403, json={"detail": "no"})

    _mock_publish(monkeypatch, {
        "POST /publish": httpx.Response(403, json={"detail": "no"}),
        "POST /blobs": blobs_handler,
    })
    res = CliRunner().invoke(
        roost_cli.publish, [str(_publish_dir(tmp_path)), "--name", "hello"],
        obj={})
    assert res.exit_code != 0
    assert "admin" in res.output.lower() or "token" in res.output.lower()
    # No blob-flow attempt for an auth failure.
    assert not called["blobs"]


def test_publish_oneshot_success_no_fallback(tmp_path, monkeypatch):
    # New CP, happy path: the one-shot 2xx is used as-is; the blob flow is
    # never touched.
    called = {"blobs": False}

    def blobs_handler(req):
        called["blobs"] = True
        return httpx.Response(200, json={"id": "blob-x"})

    _mock_publish(monkeypatch, {
        "POST /publish": httpx.Response(200, json=_site_full()),
        "POST /blobs": blobs_handler,
    })
    res = CliRunner().invoke(
        roost_cli.publish, [str(_publish_dir(tmp_path)), "--name", "hello"],
        obj={})
    assert res.exit_code == 0, res.output
    assert "live:" in res.output
    assert not called["blobs"]


# ---------- `roost tree`: R33 ↳why + --health rendering ----------


def test_tree_health_flag_appends_liveness(monkeypatch):
    jobs = [
        {"id": "root1", "state": "running", "worker_id": None,
         "parent_job_id": None, "intent": "plan", "spec": {"intent": "plan"},
         "tokens_used": 0},
        {"id": "childA", "state": "running", "worker_id": "w1",
         "parent_job_id": "root1", "intent": "do it",
         "spec": {"command": "x"}, "tokens_used": 0,
         "idle_sec": 12, "queued_sec": 3, "capable_workers": 2,
         "last_activity": "writing file"},
    ]

    def handler(request):
        assert request.url.path.endswith("/tree")
        return httpx.Response(200, json=jobs)
    monkeypatch.setattr(
        roost_cli, "_ctx_client",
        lambda _ctx: httpx.Client(
            base_url="http://cp", transport=httpx.MockTransport(handler)),
    )
    res = CliRunner().invoke(roost_cli.tree, ["root1", "--health"], obj={})
    assert res.exit_code == 0, res.output
    assert "idle 12s" in res.output
    assert "queued 3s" in res.output
    assert "capable=2" in res.output
    assert "writing file" in res.output


def test_tree_not_found_404(monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"detail": "no"})
    monkeypatch.setattr(
        roost_cli, "_ctx_client",
        lambda _ctx: httpx.Client(
            base_url="http://cp", transport=httpx.MockTransport(handler)),
    )
    res = CliRunner().invoke(roost_cli.tree, ["ghost"], obj={})
    assert res.exit_code != 0
    assert "job not found" in res.output


def test_tree_json_emits_raw(monkeypatch):
    jobs = [{"id": "root1", "state": "running", "worker_id": None,
             "parent_job_id": None, "intent": "x", "spec": {}, "tokens_used": 0}]

    def handler(request):
        return httpx.Response(200, json=jobs)
    monkeypatch.setattr(
        roost_cli, "_ctx_client",
        lambda _ctx: httpx.Client(
            base_url="http://cp", transport=httpx.MockTransport(handler)),
    )
    res = CliRunner().invoke(roost_cli.tree, ["root1", "--json"], obj={})
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)[0]["id"] == "root1"
    # JSON mode must not also render the human tree (no `↳`/state padding noise).
    assert "↳" not in res.output


def test_tree_empty_result(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[])
    monkeypatch.setattr(
        roost_cli, "_ctx_client",
        lambda _ctx: httpx.Client(
            base_url="http://cp", transport=httpx.MockTransport(handler)),
    )
    res = CliRunner().invoke(roost_cli.tree, ["root1"], obj={})
    assert res.exit_code == 0, res.output
    assert "(empty)" in res.output


# ---------- `roost send`: error branches + --wait still-queued path ----------


def test_send_job_not_found_404(monkeypatch):
    calls: list = []
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://cp", "tok", None))

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, path, json):
            calls.append(("POST", path))
            return _ExecResp({"detail": "no"}, status_code=404)
    monkeypatch.setattr(roost_cli, "_client", lambda url, token: _C())
    res = CliRunner().invoke(roost_cli.send, ["ghost", "hi"])
    assert res.exit_code != 0
    assert "job not found" in res.output


def test_send_message_too_large_413(monkeypatch):
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://cp", "tok", None))

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, path, json):
            return _ExecResp({"detail": "too big"}, status_code=413)
    monkeypatch.setattr(roost_cli, "_client", lambda url, token: _C())
    res = CliRunner().invoke(roost_cli.send, ["j1", "x" * 10])
    assert res.exit_code != 0
    assert "message too large" in res.output


def test_send_wait_still_queued_message(monkeypatch):
    """--wait but the input never leaves the queue before the deadline → a
    'still queued' note (not delivered, not an error exit)."""
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://cp", "tok", None))

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, path, json):
            return _ExecResp({"input_id": "i-1", "state": "queued"})
        def get(self, path, **k):
            # Always reports the input as still queued.
            return _ExecResp({"inputs": [{"id": "i-1", "state": "queued"}]})
    monkeypatch.setattr(roost_cli, "_client", lambda url, token: _C())
    # Make the poll loop fall through immediately: sleep is a no-op and the
    # deadline is already in the past.
    monkeypatch.setattr(roost_cli.time, "sleep", lambda *_a: None)
    times = iter([1000.0, 9999.0, 9999.0])  # start, then past the 30s deadline
    monkeypatch.setattr(roost_cli.time, "time", lambda: next(times))
    res = CliRunner().invoke(roost_cli.send, ["j1", "hi", "--wait"])
    assert res.exit_code == 0, res.output
    assert "still queued" in res.output


# ---------- `roost backup`: directory + transport-error branches ----------


def test_backup_missing_dest_dir_errors_before_request(monkeypatch):
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://cp", "admin", None))
    called = {"n": 0}

    def _never(*a, **k):
        called["n"] += 1
        raise AssertionError("must not open a client")
    monkeypatch.setattr(roost_cli.httpx, "Client", _never)
    res = CliRunner().invoke(
        roost_cli.backup, ["/no/such/dir/snapshot.db"])
    assert res.exit_code != 0
    assert "destination directory does not exist" in res.output
    assert called["n"] == 0  # bailed before any HTTP


def test_backup_transport_error_cleans_up_partfile(monkeypatch, tmp_path):
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://cp", "admin", None))

    class _Stream:
        def __enter__(self):
            raise httpx.ConnectError("refused")
        def __exit__(self, *a): pass

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def stream(self, *a, **k): return _Stream()
    monkeypatch.setattr(roost_cli.httpx, "Client", _C)
    dest = tmp_path / "snap.db"
    res = CliRunner().invoke(roost_cli.backup, [str(dest)])
    assert res.exit_code != 0
    assert "backup failed" in res.output
    # No partial file is left behind on a transport error.
    assert not dest.with_name(dest.name + ".part").exists()
    assert not dest.exists()


# ======================================================================
# R95 — client-token surface coverage (cli.py:941-1052). `pair`/`token`/
# `revoke` + helpers `_list_client_tokens`/`_revoke_client_token`/
# `_mint_client_token`. Style: CliRunner + stubbed `_ctx_client`/`_resolve`
# + httpx.MockTransport (R16/R54 idiom, _mock_ctx above) — no real
# process/socket/config. Each test pins ONE branch to observable output,
# an exit code, or a raised exception type.
# ======================================================================


def _ptok(id_, *, revoked=False, scope="mobile", last_used_at=None, label=None):
    """A `/pair-tokens` row as the server returns it (the shape cli.py reads)."""
    return {"id": id_, "revoked": revoked, "scope": scope,
            "last_used_at": last_used_at, "label": label}


# ---------- _list_client_tokens (via `roost token --list`) ----------


def test_list_client_tokens_empty_says_none(monkeypatch):
    _mock_ctx(monkeypatch, {"GET /pair-tokens": httpx.Response(200, json=[])})
    res = CliRunner().invoke(roost_cli.token, ["--list"], obj={})
    assert res.exit_code == 0, res.output
    assert res.output.strip() == "no client tokens"


def test_list_client_tokens_formats_active_revoked_and_lastused(monkeypatch):
    now = time.time()
    rows = [
        # active, used recently → relative time + label shown
        _ptok("tok-a", revoked=False, scope="mobile",
              last_used_at=now - 120, label="yang-iphone"),
        # revoked, never used → "revoked" state + "never" + blank label
        _ptok("tok-b", revoked=True, scope="agent",
              last_used_at=None, label=None),
    ]
    _mock_ctx(monkeypatch, {"GET /pair-tokens": httpx.Response(200, json=rows)})
    res = CliRunner().invoke(roost_cli.token, ["--list"], obj={})
    assert res.exit_code == 0, res.output
    lines = res.output.strip().splitlines()
    assert len(lines) == 2
    # Active row: state word, scope, a relative "last used" (2m), and the label.
    assert "tok-a" in lines[0]
    assert "active" in lines[0] and "revoked" not in lines[0]
    assert "scope=mobile" in lines[0]
    assert "last used 2m" in lines[0]
    assert "yang-iphone" in lines[0]
    # Revoked + never-used row: "revoked" state, "never", no stray label text.
    assert "tok-b" in lines[1]
    assert "revoked" in lines[1] and "active" not in lines[1]
    assert "scope=agent" in lines[1]
    assert "last used never" in lines[1]
    assert lines[1].rstrip().endswith("never")  # blank label → trailing nothing


def test_list_client_tokens_403_is_admin_message(monkeypatch):
    _mock_ctx(monkeypatch, {"GET /pair-tokens": httpx.Response(403, json={})})
    res = CliRunner().invoke(roost_cli.token, ["--list"], obj={})
    assert res.exit_code != 0
    # _admin_403("list tokens") wording is the load-bearing, actionable hint.
    assert "admin auth required to list tokens" in res.output


def test_list_client_tokens_server_error_raises(monkeypatch):
    # A non-403 >=400 must hit raise_for_status, not be swallowed.
    _mock_ctx(monkeypatch, {"GET /pair-tokens": httpx.Response(500, json={})})
    res = CliRunner().invoke(roost_cli.token, ["--list"], obj={})
    assert res.exit_code != 0
    assert isinstance(res.exception, httpx.HTTPStatusError)
    assert res.exception.response.status_code == 500


# ---------- _revoke_client_token (via `roost token --revoke`) ----------


def test_revoke_client_token_success_echoes_id(monkeypatch):
    rec = _mock_ctx(monkeypatch, {
        "DELETE /pair-tokens/tok-x": httpx.Response(200, json={})})
    res = CliRunner().invoke(roost_cli.token, ["--revoke", "tok-x"], obj={})
    assert res.exit_code == 0, res.output
    assert "revoked tok-x" in res.output
    # It issues a DELETE to the per-id path (no mint POST happens).
    assert rec.last("DELETE").url.path == "/pair-tokens/tok-x"
    assert [r for r in rec.requests if r.method == "POST"] == []


def test_revoke_client_token_404_says_not_found(monkeypatch):
    _mock_ctx(monkeypatch, {
        "DELETE /pair-tokens/ghost": httpx.Response(404, json={})})
    res = CliRunner().invoke(roost_cli.token, ["--revoke", "ghost"], obj={})
    assert res.exit_code != 0
    assert "token not found (or already revoked)" in res.output


def test_revoke_client_token_403_is_admin_message(monkeypatch):
    _mock_ctx(monkeypatch, {
        "DELETE /pair-tokens/tok-x": httpx.Response(403, json={})})
    res = CliRunner().invoke(roost_cli.token, ["--revoke", "tok-x"], obj={})
    assert res.exit_code != 0
    assert "admin auth required to revoke a token" in res.output


def test_revoke_client_token_server_error_raises(monkeypatch):
    _mock_ctx(monkeypatch, {
        "DELETE /pair-tokens/tok-x": httpx.Response(500, json={})})
    res = CliRunner().invoke(roost_cli.token, ["--revoke", "tok-x"], obj={})
    assert res.exit_code != 0
    assert isinstance(res.exception, httpx.HTTPStatusError)
    assert res.exception.response.status_code == 500


# ---------- _mint_client_token (via `roost token` mint path) ----------


def test_mint_client_token_403_is_admin_message(monkeypatch):
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://192.168.1.5:8787", "tok", None))
    _mock_ctx(monkeypatch, {"POST /pair-tokens": httpx.Response(403, json={})})
    res = CliRunner().invoke(roost_cli.token, [], obj={})
    assert res.exit_code != 0
    assert "admin auth required to mint a token" in res.output


def test_mint_client_token_400_reports_status_and_body(monkeypatch):
    # A non-403 >=400 surfaces the HTTP code AND the server's body text.
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://192.168.1.5:8787", "tok", None))
    _mock_ctx(monkeypatch, {
        "POST /pair-tokens": httpx.Response(422, text="bad scope")})
    res = CliRunner().invoke(roost_cli.token, [], obj={})
    assert res.exit_code != 0
    assert "mint failed: HTTP 422: bad scope" in res.output


# ---------- `roost token` mint success: env snippet + scope passthrough ----------


def test_token_mint_prints_env_snippet_non_loopback(monkeypatch):
    url = "http://192.168.1.5:8787"  # not loopback → no warning
    monkeypatch.setattr(roost_cli, "_resolve", lambda ctx: (url, "tok", None))
    rec = _mock_ctx(monkeypatch, {
        "POST /pair-tokens": httpx.Response(
            200, json={"id": "ct-1", "scope": "agent", "token": "rst-agt-SECRET"})})
    res = CliRunner().invoke(roost_cli.token, ["--label", "ci"], obj={})
    assert res.exit_code == 0, res.output
    # No loopback warning for a LAN url.
    assert "loopback" not in res.output
    # The one-shot secret + ready-to-paste env snippet are printed.
    assert "token id: ct-1  scope=agent" in res.output
    assert f"export ROOST_URL={url}" in res.output
    assert "export ROOST_TOKEN=rst-agt-SECRET" in res.output
    # The mint request carries the label and default scope (agent).
    body = json.loads(rec.last("POST").content)
    assert body == {"label": "ci", "scope": "agent"}


def test_token_mint_scope_mobile_passthrough(monkeypatch):
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://192.168.1.5:8787", "tok", None))
    rec = _mock_ctx(monkeypatch, {
        "POST /pair-tokens": httpx.Response(
            200, json={"id": "ct-2", "scope": "mobile", "token": "rst-mob-X"})})
    res = CliRunner().invoke(roost_cli.token, ["--scope", "mobile"], obj={})
    assert res.exit_code == 0, res.output
    body = json.loads(rec.last("POST").content)
    assert body["scope"] == "mobile"
    assert body["label"] is None  # no --label → null


def test_token_mint_loopback_warns_script_wording(monkeypatch):
    # Loopback url → the SCRIPT-flavored warning ("a remote client can't reach it"),
    # distinct from `pair`'s phone wording.
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://127.0.0.1:8787", "tok", None))
    _mock_ctx(monkeypatch, {
        "POST /pair-tokens": httpx.Response(
            200, json={"id": "ct-3", "scope": "agent", "token": "rst-agt-Y"})})
    res = CliRunner().invoke(roost_cli.token, [], obj={})
    assert res.exit_code == 0, res.output
    assert "control plane URL is loopback — a remote client can't reach it." in res.output
    assert "the phone can't reach it" not in res.output  # not the pair wording


def test_token_list_dispatch_short_circuits_mint(monkeypatch):
    # --list takes the list branch and returns before minting (no POST issued).
    rec = _mock_ctx(monkeypatch, {"GET /pair-tokens": httpx.Response(200, json=[])})
    res = CliRunner().invoke(roost_cli.token, ["--list"], obj={})
    assert res.exit_code == 0, res.output
    assert [r for r in rec.requests if r.method == "POST"] == []


# ---------- `roost pair`: QR, loopback (phone wording), dispatch ----------


def test_pair_mint_prints_uri_and_qr_when_qrcode_present(monkeypatch):
    url = "http://192.168.1.193:8787"
    monkeypatch.setattr(roost_cli, "_resolve", lambda ctx: (url, "tok", None))
    _mock_ctx(monkeypatch, {
        "POST /pair-tokens": httpx.Response(
            200, json={"id": "pt-1", "scope": "mobile", "token": "rst-mob-ABC"})})
    # Force the QR-success branch: _print_qr returns True (renders something).
    qr_calls: list = []
    monkeypatch.setattr(roost_cli, "_print_qr",
                        lambda data: (qr_calls.append(data), True)[1])
    res = CliRunner().invoke(roost_cli.pair, ["--label", "yang-iphone"], obj={})
    assert res.exit_code == 0, res.output
    # QR rendered → the "install qrcode" fallback hint must NOT appear.
    assert "install `qrcode`" not in res.output
    # The QR was fed the pairing URI (the load-bearing data).
    assert qr_calls and qr_calls[0].startswith("roost://pair?d=")
    assert "pairing uri: roost://pair?d=" in res.output
    assert "token id:    pt-1" in res.output
    assert "roost pair --revoke pt-1" in res.output
    assert "scan the QR with the Roost app" in res.output


def test_pair_mint_qr_fallback_hint_when_qrcode_missing(monkeypatch):
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://192.168.1.193:8787", "tok", None))
    _mock_ctx(monkeypatch, {
        "POST /pair-tokens": httpx.Response(
            200, json={"id": "pt-2", "scope": "mobile", "token": "rst-mob-D"})})
    # Force the fallback branch: _print_qr returns False (no qrcode package).
    monkeypatch.setattr(roost_cli, "_print_qr", lambda data: False)
    res = CliRunner().invoke(roost_cli.pair, [], obj={})
    assert res.exit_code == 0, res.output
    # Fallback hint shown, but the URI is still printed so pairing still works.
    assert "install `qrcode` for a scannable QR" in res.output
    assert "pairing uri: roost://pair?d=" in res.output


def test_pair_loopback_warns_phone_wording(monkeypatch):
    monkeypatch.setattr(roost_cli, "_resolve",
                        lambda ctx: ("http://localhost:8787", "tok", None))
    _mock_ctx(monkeypatch, {
        "POST /pair-tokens": httpx.Response(
            200, json={"id": "pt-3", "scope": "mobile", "token": "rst-mob-E"})})
    monkeypatch.setattr(roost_cli, "_print_qr", lambda data: True)
    res = CliRunner().invoke(roost_cli.pair, [], obj={})
    assert res.exit_code == 0, res.output
    # Phone-flavored warning (distinct from token's "remote client" wording).
    assert "control plane URL is loopback — the phone can't reach it." in res.output
    assert "a remote client can't reach it" not in res.output


def test_pair_list_dispatch_uses_shared_helper(monkeypatch):
    rec = _mock_ctx(monkeypatch, {
        "GET /pair-tokens": httpx.Response(
            200, json=[_ptok("pt-9", revoked=False, scope="mobile",
                             last_used_at=None, label="dev")])})
    res = CliRunner().invoke(roost_cli.pair, ["--list"], obj={})
    assert res.exit_code == 0, res.output
    assert "pt-9" in res.output and "active" in res.output
    # No mint when --list is given.
    assert [r for r in rec.requests if r.method == "POST"] == []


def test_pair_revoke_dispatch_calls_delete(monkeypatch):
    rec = _mock_ctx(monkeypatch, {
        "DELETE /pair-tokens/pt-9": httpx.Response(200, json={})})
    res = CliRunner().invoke(roost_cli.pair, ["--revoke", "pt-9"], obj={})
    assert res.exit_code == 0, res.output
    assert "revoked pt-9" in res.output
    assert rec.last("DELETE").url.path == "/pair-tokens/pt-9"


# ---------- _print_qr: package present vs absent ----------


def test_print_qr_returns_false_without_qrcode(monkeypatch):
    # Simulate the package being absent → graceful False, nothing rendered.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "qrcode":
            raise ImportError("no qrcode")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert roost_cli._print_qr("roost://pair?d=x") is False


# ---------- `roost revoke <worker_id>` (worker credential revocation) ----------


def test_revoke_worker_success(monkeypatch):
    rec = _mock_ctx(monkeypatch, {
        "DELETE /workers/w-1": httpx.Response(200, json={})})
    res = CliRunner().invoke(roost_cli.revoke, ["w-1"], obj={})
    assert res.exit_code == 0, res.output
    assert "revoked w-1" in res.output
    assert rec.last("DELETE").url.path == "/workers/w-1"


def test_revoke_worker_404_says_worker_not_found(monkeypatch):
    _mock_ctx(monkeypatch, {
        "DELETE /workers/ghost": httpx.Response(404, json={})})
    res = CliRunner().invoke(roost_cli.revoke, ["ghost"], obj={})
    assert res.exit_code != 0
    assert "worker not found" in res.output


def test_revoke_worker_403_is_admin_message(monkeypatch):
    _mock_ctx(monkeypatch, {
        "DELETE /workers/w-1": httpx.Response(403, json={})})
    res = CliRunner().invoke(roost_cli.revoke, ["w-1"], obj={})
    assert res.exit_code != 0
    assert "admin auth required" in res.output


def test_revoke_worker_server_error_raises(monkeypatch):
    _mock_ctx(monkeypatch, {
        "DELETE /workers/w-1": httpx.Response(500, json={})})
    res = CliRunner().invoke(roost_cli.revoke, ["w-1"], obj={})
    assert res.exit_code != 0
    assert isinstance(res.exception, httpx.HTTPStatusError)
    assert res.exception.response.status_code == 500
