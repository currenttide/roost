"""Tests for the `roost do` router's verdict parsing (pure part)."""
from __future__ import annotations

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
