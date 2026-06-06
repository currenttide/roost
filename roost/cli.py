"""Unified CLI for Roost.

The trust loop (the front door — just say what you want):

    roost do <goal>          classify → ask only if ambiguous, confirm if
                             destructive, then run (single) or dispatch (multi)
    roost run <task>         run one plain-language task on the best-fit node
    roost up                 zero to a running single-node fleet on this machine
    roost dispatch <goal>    hand a multi-part goal to the captain (splits it)
    roost mcp                run the Roost MCP server (drive the fleet by talking)
    roost capabilities       what this fleet can do, in plain language

Control plane / fleet:

    roost serve              run the control plane
    roost enroll-token       (admin) mint a single-use enrollment token
    roost revoke <worker>    (admin) revoke a worker's credential
    roost pair               (admin) pair a phone: scoped mobile token + QR
    roost token              (admin) mint a scoped client token (Codex/scripts)

This machine:

    roost enroll <token>     join the fleet; writes ~/.config/roost/config.toml
    roost worker             run a worker daemon (uses enrolled config if present)
    roost service ...        install/start/stop the supervised worker unit

Jobs:

    roost submit <spec>      submit a job (yaml/json file or - for stdin)
    roost jobs               list jobs
    roost status <id>        show job details
    roost tree <id>          show a job's lineage tree
    roost logs <id>          dump or follow a job's logs
    roost cancel <id>        cancel a job (or its whole subtree with --tree)
    roost workers            list registered workers
    roost ping               check the control plane is reachable

URL/token resolution everywhere: explicit flag → env (ROOST_URL/ROOST_TOKEN) →
~/.config/roost/config.toml → built-in default.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import click
import httpx
import yaml

from . import config as roost_config


def _client(url: str, token: str) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=30.0)


def _resolve(ctx: click.Context) -> tuple[str, str, Optional[str]]:
    """Resolve (url, token, worker_id) from flags → env → config → default."""
    cfg = roost_config.load()
    return roost_config.resolve_url_token(
        ctx.obj.get("url"), ctx.obj.get("token"), cfg
    )


def _ctx_client(ctx: click.Context) -> httpx.Client:
    url, token, _ = _resolve(ctx)
    return _client(url, token)


_HAIKU_MODEL = "claude-haiku-4-5-20251001"  # only for tasks the router flags trivial
_CAPTAIN_MODEL = "claude-sonnet-4-6"  # captain default — "Sonnet by default everywhere"

_CLASSIFY_PROMPT = (
    "You route requests for a fleet orchestrator. Read the GOAL and respond with ONLY a "
    "JSON object (no prose) with keys:\n"
    '  "mode": "single" if it is one task, "multi" if it needs several steps or several machines;\n'
    '  "ambiguous": true ONLY if you genuinely cannot tell what is wanted;\n'
    '  "clarifying_question": one short question to ask, or null;\n'
    '  "destructive": true if it deletes/overwrites data, affects production, or sends data externally;\n'
    '  "simple": true ONLY for a TRIVIAL one-step task where a cheap, less-reliable model is '
    'safe (e.g. print/echo a value, read a small file, report hostname/uname). Default false '
    'when unsure — anything involving correctness, computation, code, or judgement is NOT simple;\n'
    '  "restated": a one-line restatement of what you will do.\n'
    "GOAL:\n"
)


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first plausible JSON object out of arbitrary model output.

    Tolerant of clean JSON, JSON inside ``` fences, and JSON embedded in prose.
    Returns the parsed dict, or None if nothing parseable is found. Never raises.
    Mirrors `roost.watcher._extract_json_object` (balanced-brace scan) rather than
    a fragile greedy `{.*}` regex.
    """
    if not text:
        return None
    s = text.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break
    return None


def _parse_classification(result_text: str, goal: str) -> dict:
    """Pure: extract the router's JSON verdict from the model's result text, with a
    safe default (treat as a single, non-destructive task) if no JSON is present."""
    default = {"mode": "single", "ambiguous": False, "clarifying_question": None,
               "destructive": False, "simple": False, "restated": goal,
               "classify_failed": False}
    d = _extract_json_object(result_text or "")
    if d is None:
        return default
    return {
        "mode": "multi" if str(d.get("mode")).lower() == "multi" else "single",
        "ambiguous": bool(d.get("ambiguous")),
        "clarifying_question": d.get("clarifying_question") or None,
        "destructive": bool(d.get("destructive")),
        "simple": bool(d.get("simple")),
        "restated": d.get("restated") or goal,
        "classify_failed": False,
    }


def _classify_failed(goal: str) -> dict:
    """Fail-CLOSED classification: when the classifier is unavailable or its output
    can't be trusted, we MUST NOT silently treat the goal as safe. Mark it so the
    `do`/MCP flows demand explicit human confirmation before running anything."""
    return {"mode": "single", "ambiguous": False, "clarifying_question": None,
            "destructive": True, "simple": False, "restated": goal,
            "classify_failed": True}


def _needs_confirm(plan: dict) -> bool:
    """Does this verdict require an explicit human OK before we run? True when the
    classifier flagged it destructive OR couldn't classify it at all (fail-closed)."""
    return bool(plan.get("destructive") or plan.get("classify_failed"))


def _classify_goal(goal: str) -> dict:
    """Run a quick local classifier (Sonnet) to route + flag ambiguity/risk.

    Security: this FAILS CLOSED. If claude is missing, the call errors/times out,
    or the output isn't valid JSON we can parse, we return a needs-confirmation
    verdict (not a safe default) and warn on stderr — so a destructive goal can't
    slip through unconfirmed just because the classifier was unavailable."""
    import shutil
    import subprocess
    if not shutil.which("claude"):
        click.echo("⚠ could not classify goal (classifier unavailable: `claude` not "
                   "found) — treating as needs-confirmation", err=True)
        return _classify_failed(goal)
    try:
        p = subprocess.run(
            ["claude", "-p", _CLASSIFY_PROMPT + goal,
             "--model", _CAPTAIN_MODEL, "--output-format", "json"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        obj = json.loads(p.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        click.echo(f"⚠ could not classify goal (classifier unavailable: "
                   f"{type(e).__name__}) — treating as needs-confirmation", err=True)
        return _classify_failed(goal)
    result_text = obj.get("result", "")
    verdict = _extract_json_object(result_text)
    # Fail closed on no JSON at all, AND on JSON that lacks the verdict shape: a
    # parseable object like {"note":"ok"} (no `mode`/`destructive`/`ambiguous`)
    # must NOT be treated as a safe verdict — otherwise a destructive goal could
    # run unconfirmed just because the classifier returned the wrong shape.
    _verdict_keys = ("mode", "destructive", "ambiguous", "clarifying_question",
                     "simple", "restated")
    if verdict is None or not any(k in verdict for k in _verdict_keys):
        click.echo("⚠ could not classify goal (classifier returned no verdict) — "
                   "treating as needs-confirmation", err=True)
        return _classify_failed(goal)
    return _parse_classification(result_text, goal)


def _load_spec(path: str) -> dict[str, Any]:
    if path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).read_text()
    if path.endswith(".json"):
        return json.loads(text)
    return yaml.safe_load(text) or {}


def _print_job(job: dict, verbose: bool = False) -> None:
    click.echo(f"id:       {job['id']}")
    click.echo(f"state:    {job['state']}")
    if job.get("worker_id"):
        click.echo(f"worker:   {job['worker_id']}")
    if job.get("intent"):
        click.echo(f"intent:   {job['intent']}")
    if job.get("depth"):
        click.echo(f"depth:    {job['depth']}")
    if job.get("parent_job_id"):
        click.echo(f"parent:   {job['parent_job_id']}")
    if job.get("tokens_used"):
        click.echo(f"tokens:   {job['tokens_used']}")
    # Liveness facts (V4) — present when the control plane annotated them.
    if job.get("last_activity"):
        click.echo(f"activity: {job['last_activity']}")
    if job.get("idle_sec") is not None:
        click.echo(f"idle:     {job['idle_sec']}s since last sign of life")
    if job.get("queued_sec") is not None:
        click.echo(f"queued:   {job['queued_sec']}s")
    if job.get("capable_workers") is not None:
        click.echo(f"capable:  {job['capable_workers']} online worker(s) satisfy requires")
    if job.get("exit_code") is not None:
        click.echo(f"exit:     {job['exit_code']}")
    if job.get("error"):
        click.echo(f"error:    {job['error']}")
    if verbose:
        click.echo("spec:")
        click.echo(json.dumps(job.get("spec", {}), indent=2, sort_keys=True))


# ---------- history / discovery (pure formatting helpers) ----------

_TERMINAL_STATES = ("succeeded", "failed", "cancelled")


def _rel_time(epoch: Optional[float], now: Optional[float] = None) -> str:
    """Compact 'how long ago' for an epoch timestamp ('3m', '2h', '4d'). '-' if
    missing/unparsable."""
    if epoch in (None, ""):
        return "-"
    try:
        delta = (now if now is not None else time.time()) - float(epoch)
    except (TypeError, ValueError):
        return "-"
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def _history_outcome(run: dict) -> tuple[str, Optional[str]]:
    """(label, click-color) for a derived run's outcome — green verified, red
    failed/unverified, plain otherwise. Pure: reads the run's state + health."""
    state = run.get("state")
    health = (run.get("health") or {}).get("status")
    if run.get("verified") is True or health == "verified":
        return "verified ✓", "green"
    if state == "failed" or health == "failed":
        return "failed ✗", "red"
    if state == "cancelled" or health == "cancelled":
        return "cancelled", "yellow"
    if run.get("verified") is False or health == "unverified":
        return "unverified ✗", "red"
    return "done", None


def _history_row(run: dict, now: Optional[float] = None) -> tuple[str, str, str, Optional[str], str, str, str]:
    """One derived run → display fields for the history table. Pure; never raises.

    Returns (short_id, outcome_label, outcome_color, worker, cost_str, age, goal).
    """
    run_id = run.get("run_id") or run.get("id") or "-"
    short_id = str(run_id)[:8] if run_id != "-" else "-"
    label, color = _history_outcome(run)
    worker = run.get("worker") or "-"
    cost = run.get("cost") or {}
    usd = cost.get("cost_est_usd")
    cost_str = f"${usd:.2f}" if isinstance(usd, (int, float)) and usd else ""
    age = _rel_time(run.get("finished_at") or run.get("created_at"), now)
    goal = (run.get("goal") or "").replace("\n", " ").strip()
    if len(goal) > 52:
        goal = goal[:49] + "..."
    return (short_id, label, color, worker, cost_str, age, goal)


def _history_runs(runs: list[dict], *, failed_only: bool = False) -> list[dict]:
    """Filter derived runs to the 'what have I run' view: terminal states only,
    with a real goal, newest first as returned. `failed_only` keeps just the runs
    that failed / were not verified. Pure; tolerates missing fields."""
    out = []
    for r in runs:
        if r.get("state") not in _TERMINAL_STATES:
            continue
        if not (r.get("goal") or "").strip():
            continue
        if failed_only:
            label, _ = _history_outcome(r)
            if "✓" in label or label == "done":
                continue
        out.append(r)
    return out


def _recent_successes(runs: list[dict], limit: int = 5) -> list[str]:
    """Goals of the most recent verified/done runs — examples of what the fleet
    has actually done (for `capabilities`). Pure; best-effort, may be empty."""
    goals: list[str] = []
    for r in runs:
        if r.get("state") != "succeeded":
            continue
        goal = (r.get("goal") or "").replace("\n", " ").strip()
        if not goal:
            continue
        goals.append(goal if len(goal) <= 70 else goal[:67] + "...")
        if len(goals) >= limit:
            break
    return goals


def _iter_sse(resp: httpx.Response):
    """Yield (event, data_dict) for each SSE message in an httpx streaming response."""
    event = None
    data_lines: list[str] = []
    for raw in resp.iter_lines():
        if raw is None:
            continue
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    parsed = {"raw": data}
                yield event or "message", parsed
            event = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))


# ---------- click setup ----------


@click.group()
@click.option("--url", default=None,
              help="Control plane base URL (env: ROOST_URL, else config)")
@click.option("--token", default=None,
              help="Bearer token (env: ROOST_TOKEN, else config)")
@click.pass_context
def cli(ctx: click.Context, url: Optional[str], token: Optional[str]) -> None:
    """Roost: pull-based agent job orchestrator."""
    ctx.ensure_object(dict)
    ctx.obj["url"] = url
    ctx.obj["token"] = token


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8787, show_default=True, type=int)
@click.option("--db", "db_path", default=None, type=click.Path(),
              help="SQLite path (default: ~/.roost/roost.db)")
@click.option("--token", "serve_token", default=None,
              help="Required bearer token (default: env ROOST_TOKEN; empty disables auth)")
@click.option("--provision-auth/--no-provision-auth", default=True, show_default=True,
              help="On enroll, install Claude Code on the worker and provision auth "
                   "by copying this host's credentials (v1 default).")
def serve(host: str, port: int, db_path: Optional[str], serve_token: Optional[str],
          provision_auth: bool) -> None:
    """Run the control plane."""
    from . import server as _server

    db = Path(db_path) if db_path else None
    token = serve_token if serve_token is not None else os.environ.get("ROOST_TOKEN", "")
    if not token:
        click.echo("[roost] WARNING: starting with no auth token; "
                   "anyone who can reach this port can submit jobs.", err=True)
    _server.run(host=host, port=port, db_path=db, token=token,
                provision_claude_auth=provision_auth)


# ---------- one-command on-ramp ----------


def _spawn_detached(args: list[str], log_path: Path,
                    extra_env: Optional[dict[str, str]] = None):
    """Start `args` fully detached (own session, output → log_path). Returns the
    Popen handle. Used for the background control plane and worker so `roost up`
    returns instead of blocking on a long-lived process."""
    import subprocess
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    logf = open(log_path, "ab")  # noqa: SIM115 — kept open for the child's lifetime
    return subprocess.Popen(
        args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )


def _worker_already_running(config_dir: Optional[str]) -> bool:
    """Best-effort: is a worker ALREADY serving THIS control plane (so `up` doesn't
    double-start one)? We scope by the worker's CLAUDE/ROOST config dir: a worker
    spawned by `up` inherits our env (incl. ROOST_CONFIG_DIR), so a running worker
    that shares our config_dir is serving our CP; one with a different (or no)
    config_dir belongs to a different fleet and must not count. This keeps isolated
    `roost up --port N` runs from being fooled by the host's production worker."""
    import getpass
    import subprocess
    try:
        out = subprocess.run(
            ["pgrep", "-fa", "-u", getpass.getuser(), "roost worker"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return False
    pids = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        cmd = parts[1] if len(parts) > 1 else ""
        if "roost worker" not in cmd or " up" in cmd:
            continue  # skip non-worker lines and our own `roost up`
        pids.append(parts[0])
    if not pids:
        return False
    # Compare each candidate worker's ROOST_CONFIG_DIR (from /proc) to ours, so we
    # only treat it as "already running" when it serves the SAME config (CP).
    want = config_dir or ""
    for pid in pids:
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            # No /proc (e.g. macOS): fall back to the conservative "already running"
            # answer — on a single-fleet box this is the common, safe case.
            return True
        their_dir = ""
        for kv in environ:
            if kv.startswith(b"ROOST_CONFIG_DIR="):
                their_dir = kv[len(b"ROOST_CONFIG_DIR="):].decode("utf-8", "replace")
                break
        if their_dir == want:
            return True
    return False


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Host to bind a new control plane to (loopback is safest).")
@click.option("--port", default=8787, show_default=True, type=int,
              help="Port for a new control plane.")
@click.option("--url", "url_opt", default=None,
              help="Point at an EXISTING control plane instead of starting one.")
@click.option("--db", "db_path", default=None, type=click.Path(),
              help="SQLite path for a new control plane (default: ~/.roost/roost.db).")
@click.pass_context
def up(ctx: click.Context, host: str, port: int, url_opt: Optional[str],
       db_path: Optional[str]) -> None:
    """Zero to a running single-node fleet on THIS machine.

    Starts (or reuses) a control plane, enrolls this machine as a worker, starts
    the worker, smoke-tests it, and prints the panel URL + next steps. Idempotent
    and sudo-free.

      roost up                       # local fleet-of-one on 127.0.0.1:8787
      roost up --port 8788 --db /tmp/r.db   # isolated, e.g. alongside another CP

    Test in isolation (won't touch an existing CP) by pointing ROOST_CONFIG_DIR at
    a scratch dir and choosing a free port + db:
      ROOST_CONFIG_DIR=/tmp/roost-test roost up --port 8788 --db /tmp/roost-test/r.db
    """
    from . import bootstrap as boot

    # Resolve where the CP should live. An explicit --url (or ROOST_URL / config)
    # means "use that one"; otherwise we build a URL from --host/--port.
    cfg = roost_config.load()
    resolved_url, resolved_token, _ = roost_config.resolve_url_token(
        url_opt or ctx.obj.get("url"), ctx.obj.get("token"), cfg
    )
    explicit_url = url_opt or ctx.obj.get("url") or os.environ.get("ROOST_URL")
    url = explicit_url or boot.build_url(host, port)
    admin_token = resolved_token  # may be "" — only used if a CP is already up

    roost_home = Path(os.environ.get("ROOST_HOME") or (Path.home() / ".roost"))
    log_dir = roost_home / "logs"

    # --- 1. control plane: reuse if reachable, else start one detached ----------
    if boot.ping_ok(url, admin_token):
        click.echo(f"[up] control plane already reachable at {url} — reusing it")
        started_cp = False
    elif explicit_url:
        # User pointed us at a CP that isn't answering — don't silently start a
        # different one on top of their URL; fail clearly.
        raise click.ClickException(
            f"no control plane reachable at {url} (you passed --url/ROOST_URL). "
            f"Start it there, or drop --url to launch a local one.")
    else:
        # Fresh CP. Mint an admin token unless one is bound in env (so a
        # non-loopback bind isn't refused, and the fleet is authenticated).
        admin_token = os.environ.get("ROOST_TOKEN") or boot.gen_admin_token()
        db_arg = db_path or str(roost_home / "roost.db")
        Path(db_arg).parent.mkdir(parents=True, exist_ok=True)
        cp_log = log_dir / "control-plane.log"
        roost_bin = _roost_argv()
        serve_args = [*roost_bin, "serve", "--host", host, "--port", str(port),
                      "--db", db_arg, "--token", admin_token]
        click.echo(f"[up] starting control plane on {host}:{port} (logs: {cp_log})")
        try:
            _spawn_detached(serve_args, cp_log)
        except OSError as e:
            raise click.ClickException(f"could not launch control plane: {e}")
        if not boot.wait_for_health(url, admin_token, timeout=15.0):
            raise click.ClickException(
                f"control plane did not come up within 15s — check {cp_log} "
                f"(is port {port} already in use?)")
        click.echo(f"[up] control plane healthy at {url}")
        started_cp = True

    # --- 2. persist config so later `roost` commands + the panel work flag-free -
    cfg.update(boot.config_payload(url, admin_token))
    cfg_path = roost_config.save(cfg)
    env_path = boot.write_env_file(url, admin_token)
    click.echo(f"[up] config written to {cfg_path} (and {env_path})")

    # --- 3. enroll THIS machine as a worker ------------------------------------
    enrolled = bool(cfg.get("worker_id") and cfg.get("credential")
                    and cfg.get("url") == url)
    if not enrolled:
        click.echo("[up] enrolling this machine as a worker")
        try:
            with _client(url, admin_token) as c:
                # This is the operator's OWN machine — enroll it trusted so agent jobs
                # (roost do / kind: auto) can actually act (write files, run commands)
                # without per-tool permission prompts that auto-deny when headless.
                rt = c.post("/enroll-tokens",
                            json={"label": "roost up", "policy": {"trust_skip_perms": True}})
                if rt.status_code == 403:
                    raise click.ClickException(
                        "admin auth required to mint an enroll token — the CP at "
                        f"{url} has a different token than we have. Pass --token or "
                        "set ROOST_TOKEN to its admin token.")
                if rt.status_code >= 400:
                    raise click.ClickException(
                        f"could not mint enroll token: HTTP {rt.status_code}: {rt.text}")
                enroll_tok = rt.json()["token"]
        except httpx.HTTPError as e:
            raise click.ClickException(f"cannot reach control plane at {url}: {e}")
        # Reuse the enroll flow (writes worker_id+credential into the same config).
        try:
            ctx.invoke(enroll, enroll_token=enroll_tok, url_opt=url)
        except click.ClickException as e:
            raise click.ClickException(f"enrollment failed: {e.message}")
        cfg = roost_config.load()  # reload to pick up worker_id/credential
    else:
        click.echo(f"[up] already enrolled as worker {cfg.get('worker_id')} — reusing")

    # --- 4. start the worker ----------------------------------------------------
    # For the DEFAULT install we use the durable supervised service (survives
    # logout/reboot). For an ISOLATED run (a custom ROOST_CONFIG_DIR — e.g. testing
    # alongside another CP) we must NOT use the global `roost-worker.service`: that
    # single shared unit wouldn't carry our ROOST_CONFIG_DIR and would serve the
    # default fleet instead. So we spawn a detached worker that inherits our env
    # (incl. ROOST_CONFIG_DIR) and thus polls OUR control plane.
    worker_name = cfg.get("name") or boot.default_worker_name()
    isolated = bool(os.environ.get("ROOST_CONFIG_DIR"))
    wlog = log_dir / "worker.log"
    if _worker_already_running(os.environ.get("ROOST_CONFIG_DIR")):
        click.echo("[up] a worker is already serving this control plane — not starting another")
    elif isolated:
        click.echo("[up] isolated config dir → starting a detached worker "
                   f"(logs: {wlog})")
        try:
            _spawn_detached([*_roost_argv(), "worker"], wlog)
        except OSError as e:
            raise click.ClickException(f"could not start worker: {e}")
    else:
        from . import service as _svc
        rc, msg = _svc.install(start=True)
        if rc == 0:
            click.echo(f"[up] worker service installed and started ({msg})")
        else:
            # No supervisor (or it failed) — fall back to a detached background worker.
            click.echo(f"[up] supervised service unavailable ({msg.strip()}); "
                       "starting a detached worker")
            try:
                _spawn_detached([*_roost_argv(), "worker"], wlog)
                click.echo(f"[up] worker started (logs: {wlog})")
            except OSError as e:
                raise click.ClickException(f"could not start worker: {e}")

    # --- 5. smoke test: worker registers, then a trivial command job succeeds ---
    click.echo("[up] waiting for the worker to register…")
    w = boot.wait_for_worker(url, admin_token, worker_id=cfg.get("worker_id"),
                             timeout=20.0)
    if not w:
        raise click.ClickException(
            "worker did not register within 20s. Check the worker logs "
            f"({log_dir}/worker.log) or `roost service logs`. The control plane "
            f"is up at {url} — re-run `roost up` once the worker is healthy.")
    click.echo(f"[up] worker online: {w['name']} ({w['id']})")

    smoke_ok = _smoke_test(url, admin_token)
    if smoke_ok:
        click.echo("[up] smoke test passed — a job ran end-to-end ✓")
    else:
        click.echo("[up] WARNING: worker registered but the smoke-test job did not "
                   "complete; the fleet is up — inspect with `roost jobs`.", err=True)

    # --- next steps -------------------------------------------------------------
    panel = boot.panel_url(url, admin_token)
    click.echo("")
    click.echo("\033[1mYour Roost fleet is up.\033[0m")
    click.echo(f"  Panel:   {panel}")
    click.echo('  Use it:  roost do "report the OS and free memory on a CPU box"')
    click.echo("  Status:  roost workers   ·   roost jobs")
    click.echo("  Add machines: run /roost-onboard (or `roost enroll-token` for a join token)")


def _roost_argv() -> list[str]:
    """How to re-invoke this CLI for a detached child. Prefer the `roost` on PATH;
    fall back to `python -m roost.cli` so it works from a source checkout/venv."""
    import shutil
    found = shutil.which("roost")
    if found:
        return [found]
    return [sys.executable, "-m", "roost.cli"]


def _smoke_test(url: str, token: str) -> bool:
    """Submit a trivial command job and confirm it succeeds within ~30s. Returns
    True on success; False (never raises) so `up` can warn rather than abort."""
    try:
        with _client(url, token) as c:
            r = c.post("/jobs", json={"kind": "command",
                                      "command": "echo roost-up-ok"})
            if r.status_code >= 400:
                return False
            job_id = r.json()["id"]
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                jr = c.get(f"/jobs/{job_id}")
                if jr.status_code < 400:
                    st = jr.json().get("state")
                    if st == "succeeded":
                        return True
                    if st in ("failed", "cancelled"):
                        return False
                time.sleep(0.5)
    except (httpx.HTTPError, KeyError):
        return False
    return False


# ---------- enrollment / fleet membership ----------


def _collect_caps(caps_file: Optional[str], extra_caps: tuple[str, ...]) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if caps_file:
        loaded = yaml.safe_load(Path(caps_file).read_text()) or {}
        if not isinstance(loaded, dict):
            raise click.UsageError("--caps file must contain a YAML/JSON mapping")
        extra.update(loaded)
    for kv in extra_caps:
        if "=" not in kv:
            raise click.UsageError(f"--cap expects key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        try:
            extra[k] = yaml.safe_load(v)
        except yaml.YAMLError:
            extra[k] = v
    return extra


@cli.command()
@click.argument("enroll_token", required=False)
@click.option("--token", "enroll_token_opt", default=None,
              help="Enrollment token (alternative to positional arg)")
@click.option("--url", "url_opt", default=None, help="Control plane URL")
@click.option("--name", default=None, help="Worker name (default: hostname)")
@click.option("--caps", "caps_file", default=None, type=click.Path(exists=True),
              help="YAML/JSON file with extra capabilities to advertise")
@click.option("--cap", "extra_caps", multiple=True,
              help="Extra capability as key=value (repeatable)")
def enroll(enroll_token: Optional[str], enroll_token_opt: Optional[str],
           url_opt: Optional[str], name: Optional[str],
           caps_file: Optional[str], extra_caps: tuple[str, ...]) -> None:
    """Join the fleet using a single-use enrollment token.

    Exchanges the token for a per-worker credential and writes it to
    ~/.config/roost/config.toml so `roost worker` (and the supervised service)
    come up authenticated without any env vars.
    """
    import socket

    from .worker import detect_capabilities

    token = enroll_token or enroll_token_opt
    if not token:
        raise click.UsageError("provide an enrollment token (positional or --token)")
    url = url_opt or os.environ.get("ROOST_URL") or "http://127.0.0.1:8787"
    name = name or socket.gethostname()

    extra = _collect_caps(caps_file, extra_caps)
    caps = detect_capabilities(extra, self_test=True)

    with _client(url, "") as c:
        r = c.post("/enroll", json={"token": token, "name": name, "capabilities": caps})
    if r.status_code == 403:
        raise click.ClickException(f"enrollment rejected: {r.text}")
    if r.status_code >= 400:
        raise click.ClickException(f"enroll failed: HTTP {r.status_code}: {r.text}")
    body = r.json()

    cfg = roost_config.load()
    cfg.update(
        url=url,
        worker_id=body["worker_id"],
        credential=body["credential"],
        name=name,
    )
    path = roost_config.save(cfg)
    click.echo(f"enrolled as {name} (worker_id={body['worker_id']})")
    if body.get("policy"):
        click.echo(f"policy: {json.dumps(body['policy'])}")
    click.echo(f"config written to {path}")
    if body.get("onboarding"):
        _run_onboarding(body["onboarding"])
    click.echo("start the worker with `roost worker` or `roost service install --start`")


def _run_onboarding(onboarding: dict[str, Any]) -> None:
    """Bring Claude Code up on this worker: install it if missing and write the
    provisioned auth. Best-effort — failures warn but never abort enrollment."""
    import shutil
    import subprocess

    if onboarding.get("install_claude") and shutil.which("claude") is None:
        install_cmd = onboarding.get("install_cmd")
        if install_cmd:
            click.echo(f"[onboard] Claude Code not found; installing: {install_cmd}")
            try:
                subprocess.run(install_cmd, shell=True, check=True, timeout=600)
                click.echo("[onboard] Claude Code installed "
                           "(ensure ~/.local/bin is on PATH for `roost worker`)")
            except (subprocess.SubprocessError, OSError) as e:
                click.echo(f"[onboard] WARNING: install failed: {e}; "
                           "install Claude Code manually to run agent jobs", err=True)

    auth = onboarding.get("auth") or {}
    if auth.get("method") == "copy" and auth.get("credentials_json"):
        # If CLAUDE_CONFIG_DIR is set, provision INTO that dir so we use our own
        # credentials without touching the node's existing ~/.claude (claude
        # keeps ALL its state — creds AND .claude.json — under CLAUDE_CONFIG_DIR).
        ccd = os.environ.get("CLAUDE_CONFIG_DIR")
        if ccd:
            cfg_dir = Path(ccd).expanduser()
            target = cfg_dir / ".credentials.json"
            cfg = cfg_dir / ".claude.json"
        else:
            target = Path(auth.get("target", "~/.claude/.credentials.json")).expanduser()
            cfg = Path("~/.claude.json").expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(auth["credentials_json"])
            target.chmod(0o600)
            click.echo(f"[onboard] Claude auth provisioned → {target} (copied from host)")
        except OSError as e:
            click.echo(f"[onboard] WARNING: could not write auth to {target}: {e}", err=True)
        # Headless `claude -p` silently no-ops until onboarding is marked complete
        # in (CLAUDE_CONFIG_DIR or ~/).claude.json. Set it so the worker can run agent jobs.
        try:
            cfg.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(cfg.read_text()) if cfg.exists() else {}
            if not data.get("hasCompletedOnboarding"):
                data["hasCompletedOnboarding"] = True
                cfg.write_text(json.dumps(data))
                click.echo(f"[onboard] marked Claude onboarding complete → {cfg}")
        except (OSError, json.JSONDecodeError) as e:
            click.echo(f"[onboard] WARNING: could not update {cfg}: {e}", err=True)


@cli.command("enroll-token")
@click.option("--label", default=None, help="Human label for audit")
@click.option("--ttl", default=None, type=float, help="Lifetime in seconds (default: server's 15m)")
@click.option("--trust", is_flag=True,
              help="Allow this worker to honor --dangerously-skip-permissions jobs")
@click.option("--allow-command", "allow_commands", multiple=True,
              help="Restrict jobs to these shell commands on the worker (repeatable)")
@click.option("--allow-path", "allow_paths", multiple=True,
              help="Restrict jobs to these paths on the worker (repeatable)")
@click.option("--curl/--no-curl", default=True,
              help="Print a ready-to-paste install one-liner (default: yes)")
@click.pass_context
def enroll_token_cmd(ctx: click.Context, label: Optional[str], ttl: Optional[float],
                     trust: bool, allow_commands: tuple[str, ...],
                     allow_paths: tuple[str, ...], curl: bool) -> None:
    """(Admin) Mint a single-use enrollment token for a new machine."""
    policy: dict[str, Any] = {}
    if trust:
        policy["trust_skip_perms"] = True
    if allow_commands:
        policy["allow_commands"] = list(allow_commands)
    if allow_paths:
        policy["allow_paths"] = list(allow_paths)

    body: dict[str, Any] = {"label": label, "policy": policy}
    if ttl is not None:
        body["ttl_sec"] = ttl
    url, _, _ = _resolve(ctx)
    with _ctx_client(ctx) as c:
        r = c.post("/enroll-tokens", json=body)
    if r.status_code == 403:
        raise click.ClickException("admin auth required (use the shared --token/ROOST_TOKEN)")
    if r.status_code >= 400:
        raise click.ClickException(f"mint failed: HTTP {r.status_code}: {r.text}")
    tok = r.json()
    click.echo(f"enrollment token: {tok['token']}")
    click.echo(f"expires_at:       {tok['expires_at']:.0f} (epoch)")
    if curl:
        click.echo("")
        click.echo("On the new machine, paste:")
        click.echo(f"  curl -fsSL {url}/install.sh | sh -s -- {tok['token']}")


def _pair_uri(url: str, token: str, label: Optional[str]) -> str:
    """Compact pairing payload for QR: roost://pair?d=<base64url(json)>."""
    import base64

    payload = {"v": 1, "url": url, "token": token}
    if label:
        payload["name"] = label
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return "roost://pair?d=" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _print_qr(data: str) -> bool:
    """Render a terminal QR if the optional `qrcode` package is present."""
    try:
        import qrcode  # type: ignore
    except ImportError:
        return False
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.print_ascii(invert=True)
    return True


def _admin_403(verb: str) -> click.ClickException:
    return click.ClickException(
        f"admin auth required to {verb} (use the shared --token/ROOST_TOKEN)")


def _list_client_tokens(c) -> None:
    """Shared by `roost pair --list` and `roost token --list`."""
    r = c.get("/pair-tokens")
    if r.status_code == 403:
        raise _admin_403("list tokens")
    r.raise_for_status()
    rows = r.json()
    if not rows:
        click.echo("no client tokens")
        return
    for t in rows:
        state = "revoked" if t["revoked"] else "active"
        used = _rel_time(t["last_used_at"]) if t["last_used_at"] else "never"
        click.echo(f"{t['id']}  {state:8}  scope={t['scope']}  "
                   f"last used {used}  {t['label'] or ''}")


def _revoke_client_token(c, token_id: str) -> None:
    """Shared by `roost pair --revoke` and `roost token --revoke`."""
    r = c.delete(f"/pair-tokens/{token_id}")
    if r.status_code == 403:
        raise _admin_403("revoke a token")
    if r.status_code == 404:
        raise click.ClickException("token not found (or already revoked)")
    r.raise_for_status()
    click.echo(f"revoked {token_id}")


def _mint_client_token(c, label: Optional[str], scope: str) -> dict:
    """Mint a scoped client token; returns the minted row (token shown once)."""
    r = c.post("/pair-tokens", json={"label": label, "scope": scope})
    if r.status_code == 403:
        raise _admin_403("mint a token")
    if r.status_code >= 400:
        raise click.ClickException(f"mint failed: HTTP {r.status_code}: {r.text}")
    return r.json()


@cli.command()
@click.option("--label", default=None,
              help="Device label for audit (e.g. 'yang-iphone')")
@click.option("--list", "list_", is_flag=True, help="List paired tokens")
@click.option("--revoke", "revoke_id", default=None, metavar="TOKEN_ID",
              help="Revoke a pairing by token id")
@click.pass_context
def pair(ctx: click.Context, label: Optional[str], list_: bool,
         revoke_id: Optional[str]) -> None:
    """(Admin) Pair a phone: mint a 'mobile'-scoped token and show a QR.

    The mobile scope can read fleet state and submit/cancel jobs, but can
    never mint enroll tokens, touch workers, or fetch Claude credentials.
    For a Codex/script front door (no QR), use `roost token --scope agent`.
    """
    url, _, _ = _resolve(ctx)
    with _ctx_client(ctx) as c:
        if list_:
            _list_client_tokens(c)
            return
        if revoke_id:
            _revoke_client_token(c, revoke_id)
            return
        tok = _mint_client_token(c, label, "mobile")

    if any(h in url for h in ("127.0.0.1", "localhost")):
        click.echo("warning: control plane URL is loopback — the phone can't reach it.")
        click.echo("         re-run with --url http://<LAN-or-tailscale-address>:8787\n")
    uri = _pair_uri(url, tok["token"], label)
    if not _print_qr(uri):
        click.echo("(install `qrcode` for a scannable QR: uv tool install qrcode)")
    click.echo(f"pairing uri: {uri}")
    click.echo(f"token id:    {tok['id']}  (revoke later: roost pair --revoke {tok['id']})")
    click.echo("scan the QR with the Roost app, or paste the URI into its pairing screen.")


@cli.command()
@click.option("--label", default=None,
              help="Token label for audit (e.g. 'codex', 'ci-script')")
@click.option("--scope", default="agent",
              type=click.Choice(["agent", "mobile"]),
              help="Token scope (default: agent — for Codex/scripts)")
@click.option("--list", "list_", is_flag=True, help="List client tokens")
@click.option("--revoke", "revoke_id", default=None, metavar="TOKEN_ID",
              help="Revoke a client token by id")
@click.pass_context
def token(ctx: click.Context, label: Optional[str], scope: str,
          list_: bool, revoke_id: Optional[str]) -> None:
    """(Admin) Mint a scoped client token for an agent front door (Codex, the
    Codex app, scripts) — your personal compute backend without the admin token.

    Prints the token ONCE plus a ready-to-paste env snippet. The token can read
    fleet state, submit/cancel jobs, and use the blob store for file transfer,
    but can never mint tokens, enroll/revoke workers, finalize jobs, or read
    Claude credentials. Shares plumbing with `roost pair` (phones).
    """
    url, _, _ = _resolve(ctx)
    with _ctx_client(ctx) as c:
        if list_:
            _list_client_tokens(c)
            return
        if revoke_id:
            _revoke_client_token(c, revoke_id)
            return
        tok = _mint_client_token(c, label, scope)

    if any(h in url for h in ("127.0.0.1", "localhost")):
        click.echo("note: control plane URL is loopback — a remote client can't reach it.")
        click.echo("      re-run with --url http://<LAN-or-tailscale-address>:8787\n")
    click.echo(f"token id: {tok['id']}  scope={tok['scope']}  "
               f"(revoke later: roost token --revoke {tok['id']})")
    click.echo("\npaste into the client's environment (shown once):\n")
    click.echo(f"  export ROOST_URL={url}")
    click.echo(f"  export ROOST_TOKEN={tok['token']}")


@cli.command()
@click.argument("worker_id")
@click.pass_context
def revoke(ctx: click.Context, worker_id: str) -> None:
    """(Admin) Revoke a worker's credential and mark it offline."""
    with _ctx_client(ctx) as c:
        r = c.delete(f"/workers/{worker_id}")
    if r.status_code == 404:
        raise click.ClickException("worker not found")
    if r.status_code == 403:
        raise click.ClickException("admin auth required (use the shared --token/ROOST_TOKEN)")
    r.raise_for_status()
    click.echo(f"revoked {worker_id}")


@cli.command("prune-workers")
@click.option("--days", "days", default=7.0, type=float, show_default=True,
              help="Delete worker rows not seen in this many days.")
@click.option("--yes", "-y", "yes", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def prune_workers_cmd(ctx: click.Context, days: float, yes: bool) -> None:
    """(Admin) Delete ghost/duplicate worker rows not seen in --days days.

    Clears the dead rows a node leaves behind when it re-enrolls, so the fleet
    view stops accumulating duplicates. Live and recently-seen nodes are never
    touched; a worker still running a job is always spared.
    """
    with _ctx_client(ctx) as c:
        r = c.get("/workers")
        r.raise_for_status()
        now = time.time()
        cutoff = now - days * 86400.0
        victims = [w for w in r.json() if (w.get("last_seen") or 0) < cutoff]
        if not victims:
            click.echo(f"nothing to prune — no worker unseen for {days:g} day(s)")
            return
        click.echo(f"will prune {len(victims)} worker row(s) not seen in {days:g} day(s):")
        for w in victims:
            age_d = (now - (w.get("last_seen") or 0)) / 86400.0
            click.echo(f"  {w['name']:<20} {w['id']}  {w.get('status','?'):<7} seen {age_d:.1f}d ago")
        if not yes:
            click.confirm("proceed?", abort=True)
        r = c.post("/workers/prune", params={"older_than_days": days})
        if r.status_code == 403:
            raise click.ClickException("admin auth required (use the shared --token/ROOST_TOKEN)")
        r.raise_for_status()
        res = r.json()
        names = ", ".join(res.get("names", []))
        click.echo(f"pruned {res.get('pruned', 0)} worker(s)" + (f": {names}" if names else ""))


# ---------- worker daemon ----------


@cli.command()
@click.option("--name", default=None, help="Worker name (default: config/hostname)")
@click.option("--cwd", default=None, help="Default working directory for jobs")
@click.option("--caps", "caps_file", default=None, type=click.Path(exists=True),
              help="YAML/JSON file with extra capabilities to advertise")
@click.option("--cap", "extra_caps", multiple=True,
              help="Extra capability as key=value (repeatable); values are parsed as YAML")
@click.pass_context
def worker(ctx: click.Context, name: Optional[str], cwd: Optional[str],
           caps_file: Optional[str], extra_caps: tuple[str, ...]) -> None:
    """Run a worker on this machine.

    With no flags it uses the enrolled credential from
    ~/.config/roost/config.toml. Falls back to ROOST_URL/ROOST_TOKEN (shared
    token / trusted-LAN mode) if there's no enrolled config.
    """
    from . import worker as _worker

    extra = _collect_caps(caps_file, extra_caps)

    cfg = roost_config.load()
    url, token, worker_id = _resolve(ctx)
    enrolled = bool(cfg.get("worker_id") and cfg.get("credential"))
    name = name or cfg.get("name")

    _worker.run(
        base_url=url,
        token=token,
        worker_id=worker_id,
        name=name,
        extra_capabilities=extra,
        default_cwd=cwd,
        enrolled=enrolled,
    )


# ---------- supervised service ----------


@cli.group()
def service() -> None:
    """Install/manage the worker as a supervised user service (systemd/launchd)."""


@service.command("install")
@click.option("--start/--no-start", default=False, help="Start the unit after installing.")
def service_install(start: bool) -> None:
    """Install the supervised worker unit."""
    from . import service as _svc

    rc, msg = _svc.install(start=start)
    click.echo(msg)
    sys.exit(rc)


@service.command("start")
def service_start() -> None:
    """Start the worker service."""
    from . import service as _svc

    rc, msg = _svc.start()
    click.echo(msg)
    sys.exit(rc)


@service.command("stop")
def service_stop() -> None:
    """Stop the worker service."""
    from . import service as _svc

    rc, msg = _svc.stop()
    click.echo(msg)
    sys.exit(rc)


@service.command("status")
def service_status() -> None:
    """Show the worker service status."""
    from . import service as _svc

    rc, msg = _svc.status()
    if msg:
        click.echo(msg)
    sys.exit(rc)


@service.command("logs")
@click.option("--follow", "-f", is_flag=True, help="Tail the service logs.")
def service_logs(follow: bool) -> None:
    """Show the worker service logs."""
    from . import service as _svc

    rc, msg = _svc.logs(follow=follow)
    if msg:
        click.echo(msg)
    sys.exit(rc)


# ---------- captain (intelligent dispatch) ----------


def dispatch_goal(url: str, token: str, goal: str, *, model: Optional[str] = None,
                  max_tokens: Optional[int] = None,
                  echo: Optional[Any] = None) -> tuple[Optional[str], int]:
    """Run the captain over GOAL: snapshot the fleet, anchor a captain-root job,
    launch the captain agent, finalize the root. Returns (root_job_id, rc).

    Shared by the `dispatch` CLI command and the MCP `roost_do` multi path so both
    take the SAME captain route. `echo` is an optional callback for human-facing
    notes (the CLI passes click.echo); pass None to stay quiet (MCP)."""
    from . import captain as _captain

    def _note(msg: str) -> None:
        if echo is not None:
            echo(msg)

    with _client(url, token) as c:
        r = c.get("/workers")
        r.raise_for_status()
        workers = r.json()
        live = [w for w in workers if w.get("status") in ("idle", "busy")]
        if not live:
            _note("[roost] WARNING: no online workers; the captain has nowhere to "
                  "dispatch. Start one with `roost worker`.")
        root_spec: dict[str, Any] = {
            "intent": goal,
            "kind": "captain",
            "captain_root": True,
            "hierarchy": {"can_dispatch": True, "max_depth": 3},
        }
        if max_tokens:
            root_spec["budget"] = {"max_tokens": max_tokens}
        rr = c.post("/jobs", json=root_spec)
        if rr.status_code >= 400:
            raise RuntimeError(
                f"could not create captain-root job: HTTP {rr.status_code}: {rr.text}")
        root_id = rr.json()["id"]
        _note(f"[roost] captain-root {root_id} (track the plan: roost tree {root_id})")

    budget_note = None
    if max_tokens:
        budget_note = (f"Keep total token spend across all sub-jobs under "
                       f"{max_tokens} tokens. Set per-job max_tokens accordingly. "
                       f"The tree budget is enforced: a sub-job that would exceed "
                       f"the remaining budget is refused.")

    rc = 1
    try:
        rc = _captain.run(url, token, goal, workers, model=model,
                          budget_note=budget_note, parent_job_id=root_id)
    finally:
        final_state = "succeeded" if rc == 0 else "failed"
        with _client(url, token) as c:
            try:
                c.post(f"/jobs/{root_id}/finalize", json={"state": final_state})
            except httpx.HTTPError:
                pass
    return root_id, rc


@cli.command()
@click.argument("goal")
@click.option("--model", default=None,
              help="Model for the captain agent (default: Sonnet — claude-sonnet-4-6)")
@click.option("--budget", "max_tokens", default=None, type=int,
              help="Soft overall token budget the captain should stay within")
@click.pass_context
def dispatch(ctx: click.Context, goal: str, model: Optional[str],
             max_tokens: Optional[int]) -> None:
    """Dispatch a natural-language GOAL to the fleet via an orchestrator agent.

    A local captain agent inspects the fleet, decomposes the goal, places each
    sub-job on the best-fit worker, monitors them, and returns a merged result.

    Example:
      roost dispatch "run the eval on a GPU box and lint on any CPU box, then summarize"
    """
    url, token, _ = _resolve(ctx)
    try:
        _, rc = dispatch_goal(url, token, goal, model=model, max_tokens=max_tokens,
                              echo=lambda m: click.echo(m, err=m.startswith("[roost] WARNING")))
    except httpx.HTTPError as e:
        raise click.ClickException(f"cannot reach control plane at {url}: {e}")
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except RuntimeError as e:
        raise click.ClickException(str(e))
    sys.exit(rc)


@cli.command(name="do")
@click.argument("goal", nargs=-1, required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip clarification/confirmation prompts.")
@click.option("--model", default=None, help="Override the model.")
@click.pass_context
def do_(ctx: click.Context, goal: tuple, yes: bool, model: Optional[str]) -> None:
    """Do a GOAL — the one front door. Just say what you want.

    Roost classifies the request, asks a question only if it's genuinely ambiguous,
    confirms before anything destructive, then routes it: a single task runs on the
    best-fit node (verified); a multi-part goal goes to the captain to split across
    nodes. You never pick a kind, write a spec, or choose a worker.

      roost do "report the GPU model and free VRAM on a GPU box"
      roost do "run the test suite on a GPU box and lint the repo on a CPU box, then summarize"
    """
    goal_str = " ".join(goal)
    plan = _classify_goal(goal_str)
    interactive = sys.stdin.isatty()

    if not yes and plan["ambiguous"] and plan["clarifying_question"]:
        if not interactive:
            # Can't prompt in automation — abort with the question so the caller
            # can refine and re-run (with the answer baked in, or --yes).
            raise click.ClickException(
                f"clarification needed: {plan['clarifying_question']}\n"
                "Re-run with the answer in the goal, or pass --yes to proceed as-is "
                "(requires an interactive terminal otherwise).")
        ans = click.prompt(f"❓ {plan['clarifying_question']}\n  your answer")
        goal_str = f"{goal_str}\n\n(clarification: {ans})"
    if _needs_confirm(plan):
        reason = ("could not classify (fail-closed)" if plan.get("classify_failed")
                  else "looks destructive")
        # Always WARN (even under --yes): a destructive/unclassifiable goal must
        # never run silently. --yes only skips the interactive confirmation prompt,
        # not the warning itself.
        click.echo(f"⚠️  This {reason}: {plan['restated']}", err=True)
        if not yes:
            if not interactive:
                # Never block on a prompt in automation — fail clearly instead of hanging.
                raise click.ClickException(
                    "this goal needs confirmation before running, but no interactive "
                    "terminal is attached. Re-run with --yes to proceed, or run it in "
                    "an interactive shell.")
            if not click.confirm("  proceed?", default=False):
                raise click.Abort()

    if plan["mode"] == "multi":
        click.echo("[roost] multi-step goal → captain (splitting across the fleet)")
        ctx.invoke(dispatch, goal=goal_str, model=model, max_tokens=None)
    else:
        # Mostly Sonnet (reliable). Drop to Haiku ONLY for tasks flagged trivial, and
        # only when the user didn't pin a model. The verifier/fix stay Sonnet regardless.
        chosen = model
        if not model and plan["simple"]:
            chosen = _HAIKU_MODEL
            click.echo("[roost] trivial task → best-fit node, fast model (still Sonnet-verified)")
        else:
            click.echo("[roost] single task → best-fit node (Sonnet, verified)")
        ctx.invoke(run, task=(goal_str,), follow=True, model=chosen,
                   wallclock_min=15, verify=True, as_json=False)


@cli.command()
@click.pass_context
def capabilities(ctx: click.Context) -> None:
    """Describe what this fleet can do, in plain language, with examples (discovery)."""
    recent: list[str] = []
    with _ctx_client(ctx) as c:
        try:
            workers = c.get("/workers").json()
        except httpx.HTTPError as e:
            raise click.ClickException(f"cannot reach control plane: {e}")
        # Best-effort: real examples of what this fleet has actually done.
        try:
            d = c.get("/derived", params={"limit": 60})
            if d.status_code < 400:
                recent = _recent_successes((d.json() or {}).get("runs", []), limit=5)
        except (httpx.HTTPError, ValueError):
            recent = []
    live = [w for w in workers if w.get("status") in ("idle", "busy")]
    if not live:
        click.echo("No online workers. Start one with `roost worker`, or add nodes with /roost-onboard.")
        return
    cores = sum((w["capabilities"].get("cpus") or 0) for w in live)
    gpus = []
    for w in live:
        cp = w["capabilities"]
        n = cp.get("gpu_count") or 0
        if n:
            name = (cp.get("gpu") or ["GPU"])[0].replace("NVIDIA ", "")
            gpus.append(f"{w['name']}: {n}× {name}"
                        + (f" ({cp.get('gpu_vram_gb')}GB)" if cp.get("gpu_vram_gb") else ""))
    has_docker = any(w["capabilities"].get("docker") for w in live)
    click.echo(f"\033[1mYour Roost fleet\033[0m — {len(live)} nodes · {cores} CPU cores · "
               f"{len(gpus)} GPU node(s)")
    if gpus:
        click.echo("GPUs:")
        for g in gpus:
            click.echo(f"  • {g}")
    click.echo("\n\033[1mWhat it can do\033[0m")
    click.echo("  • Run anything you can say in plain language — it picks the best node and verifies the result.")
    click.echo("  • CPU work, agent tasks, and (on the GPU nodes) GPU / training jobs"
               + (" in isolated containers." if has_docker else "."))
    click.echo("\n\033[1mJust say what you want\033[0m")
    click.echo('  roost do "tell me the OS and free memory on a CPU box"')
    if gpus:
        click.echo('  roost do "report the GPU model and free VRAM on a GPU box"')
    click.echo('  roost do "run the test suite somewhere and tell me if it passes"')
    if recent:
        click.echo("\n\033[1mRecently run\033[0m (real goals this fleet has completed)")
        for g in recent:
            click.echo(f"  • {g}")
    click.echo("\nMore: `roost workers` (the fleet) · `roost history` (what it has run) · "
               "`roost do --help`")


@cli.command()
@click.pass_context
def mcp(ctx: click.Context) -> None:
    """Run the Roost MCP server (stdio) — lets a Claude console drive the fleet by talking.

    Add to a Claude Code project's .mcp.json:
      {"mcpServers": {"roost": {"command": "roost", "args": ["mcp"]}}}
    Then just talk: "run the tests on a GPU box", "what's running?", "why did that fail?".
    """
    from . import mcp as _mcp
    url, token, _ = _resolve(ctx)
    os.environ["ROOST_URL"] = url
    os.environ["ROOST_TOKEN"] = token or ""
    _mcp.main()


# ---------- publish (built thing → real URL on your own CP) ----------


def _tar_site(src: Path) -> bytes:
    """Tar.gz a directory with members relative to it (arcname='.'), in memory.

    Includes everything except a top-level .git directory."""
    import io
    import tarfile

    def _filter(info: "tarfile.TarInfo") -> Optional["tarfile.TarInfo"]:
        name = info.name.lstrip("./")
        if name == ".git" or name.startswith(".git/"):
            return None
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(src), arcname=".", filter=_filter)
    return buf.getvalue()


def _print_sites(c) -> None:
    r = c.get("/publish")
    if r.status_code == 403:
        raise _admin_403("list sites")
    r.raise_for_status()
    rows = r.json()
    if not rows:
        click.echo("no published sites")
        return
    for s in rows:
        click.echo(f"{s['slug']:20}  {s['files']:>5} files  "
                   f"{s['size'] / 1024:.0f} KB  {s['url']}")


@cli.command()
@click.argument("directory", required=False,
                type=click.Path(exists=True, file_okay=False))
@click.option("--name", default=None,
              help="Site name / slug (default: the directory name).")
@click.option("--list", "list_", is_flag=True, help="List published sites.")
@click.option("--unpublish", "unpublish_slug", default=None, metavar="SLUG",
              help="Remove a published site.")
@click.pass_context
def publish(ctx: click.Context, directory: Optional[str], name: Optional[str],
            list_: bool, unpublish_slug: Optional[str]) -> None:
    """Publish a built directory as a live site on your own control plane.

    One command, end to end: tars the directory, uploads it via the blob store,
    and the CP extracts it — live at <cp-url>/pub/<slug>/ immediately. Rebuild
    and re-run to republish (same name replaces the site).
    """
    url, _, _ = _resolve(ctx)
    with _ctx_client(ctx) as c:
        if list_:
            _print_sites(c)
            return
        if unpublish_slug:
            r = c.delete(f"/publish/{unpublish_slug}")
            if r.status_code == 403:
                raise _admin_403("unpublish a site")
            if r.status_code == 404:
                raise click.ClickException("site not found")
            r.raise_for_status()
            click.echo(f"unpublished {unpublish_slug}")
            return

        if not directory:
            raise click.ClickException(
                "give a directory to publish, or use --list / --unpublish")
        src = Path(directory)
        if not (src / "index.html").is_file():
            click.echo("warning: no index.html at the top level "
                       "(publishing anyway — it may be assets).")

        slug_name = name or src.resolve().name
        bundle = _tar_site(src)
        up = c.post(f"/blobs?name={slug_name}.tar.gz", content=bundle)
        if up.status_code >= 400:
            raise click.ClickException(
                f"upload failed: HTTP {up.status_code}: {up.text}")
        blob_id = up.json()["id"]

        r = c.post("/publish", json={"name": slug_name, "blob_id": blob_id})
        if r.status_code == 403:
            raise _admin_403("publish")
        if r.status_code >= 400:
            raise click.ClickException(
                f"publish failed: HTTP {r.status_code}: {r.text}")
        site = r.json()

    click.echo(f"live: {site['url']}")
    click.echo(f"      {site['files']} files, {site['size'] / 1024:.0f} KB")
    if any(h in url for h in ("127.0.0.1", "localhost")):
        click.echo("note: control plane URL is loopback — only reachable from this "
                   "machine.\n      re-run with --url http://<LAN-or-tailscale-address>:8787 "
                   "to share it.")


# ---------- jobs ----------


@cli.command()
@click.argument("spec", type=click.Path(allow_dash=True))
@click.option("--follow/--detach", default=True,
              help="Stream logs until job finishes (default) or return immediately.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON on submit.")
@click.pass_context
def submit(ctx: click.Context, spec: str, follow: bool, as_json: bool) -> None:
    """Submit a job spec (YAML/JSON file or - for stdin)."""
    body = _load_spec(spec)
    url, token, _ = _resolve(ctx)
    with _client(url, token) as c:
        r = c.post("/jobs", json=body)
        if r.status_code >= 400:
            raise click.ClickException(f"submit failed: HTTP {r.status_code}: {r.text}")
        job = r.json()
    if as_json:
        # Keep stdout clean machine-readable JSON — never stream logs after it.
        click.echo(json.dumps(job, indent=2))
        return
    click.echo(f"submitted: {job['id']} (state={job['state']})")
    if not follow:
        return
    rc = _stream(url, token, job["id"])
    sys.exit(rc)


@cli.command()
@click.argument("task", nargs=-1, required=True)
@click.option("--follow/--detach", default=True,
              help="Stream until the task finishes (default) or return immediately.")
@click.option("--model", default=None,
              help="Override the triage/exec model (default: Sonnet).")
@click.option("--wallclock-min", default=10, type=int, show_default=True,
              help="Hard wall-clock cap.")
@click.option("--verify/--no-verify", default=True, show_default=True,
              help="Independently verify the result was actually achieved (the trust loop).")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON on submit.")
@click.pass_context
def run(ctx: click.Context, task: tuple, follow: bool, model: Optional[str],
        wallclock_min: int, verify: bool, as_json: bool) -> None:
    """Run a plain-language TASK on whatever node fits best — no spec needed.

    The task is handed to a free worker whose own agent self-assesses fit and either
    does it or declines so it routes to a better-suited node (kind: auto). This is the
    zero-ceremony front door; use `submit` for precise control (requires/kind/docker)
    or `dispatch` to split a multi-part goal across nodes.

      roost run "report the GPU model and free VRAM"
      roost run "lint the repo and summarize the issues"
    """
    body = {"kind": "auto", "task": " ".join(task), "verify": verify,
            "budget": {"max_wallclock_min": wallclock_min, "max_tokens": 200000}}
    if model:
        body["model"] = model
    url, token, _ = _resolve(ctx)
    with _client(url, token) as c:
        r = c.post("/jobs", json=body)
        if r.status_code >= 400:
            raise click.ClickException(f"run failed: HTTP {r.status_code}: {r.text}")
        job = r.json()
    if as_json:
        click.echo(json.dumps(job, indent=2))
        return
    click.echo(f"running: {job['id']} (kind=auto) — a worker will self-select")
    if not follow:
        return
    rc = _stream(url, token, job["id"])
    # Trust loop: surface the verifier's verdict + evidence (the human reads this, not logs).
    try:
        with _client(url, token) as c:
            final = c.get(f"/jobs/{job['id']}").json()
        res = final.get("result")
        if isinstance(res, dict) and "verified" in res:
            mark = "✓ verified" if res.get("verified") else "✗ NOT verified"
            click.echo(f"\n{mark} — {res.get('evidence', '')}")
            if res.get("output"):
                click.echo(f"result: {res['output']}")
    except Exception:  # noqa: BLE001 — best-effort summary
        pass
    sys.exit(rc)


def _resolve_target(workers: list[dict], target: str) -> dict:
    """Pick the single worker a `roost exec` should hard-pin to.

    `target` matches a worker by id (exact) OR by name. Pure; raises
    click.ClickException with an actionable message when there's no match or an
    ambiguous one:
      - an id match always wins and is unambiguous (ids are unique).
      - otherwise match by name; if a name matches several ONLINE workers we
        can't tell them apart → ask the operator to use an id (two nodes can
        briefly share a name). Offline same-name rows don't count toward
        ambiguity (a dead duplicate shouldn't block exec).
    """
    for w in workers:
        if w.get("id") == target:
            return w
    by_name = [w for w in workers if w.get("name") == target]
    if not by_name:
        raise click.ClickException(
            f"no worker named/ided '{target}' — see `roost workers`")
    online = [w for w in by_name if w.get("status") in ("idle", "busy")]
    candidates = online or by_name
    if len(candidates) > 1:
        ids = ", ".join(w["id"] for w in candidates)
        raise click.ClickException(
            f"name '{target}' matches {len(candidates)} workers ({ids}) — "
            f"use a worker id (see `roost workers`)")
    return candidates[0]


@cli.command("exec")
@click.argument("worker")
@click.argument("command", nargs=-1, required=True)
@click.option("--timeout", "timeout_min", default=2.0, type=float, show_default=True,
              help="Hard wall-clock budget in minutes.")
@click.option("--detach", "detach", is_flag=True,
              help="Submit and print the job id without waiting.")
@click.option("--no-validate", "no_validate", is_flag=True,
              help="Skip the up-front GET /workers target check.")
@click.pass_context
def exec_(ctx: click.Context, worker: str, command: tuple, timeout_min: float,
          detach: bool, no_validate: bool) -> None:
    """Run a shell COMMAND on ONE specific fleet WORKER — no SSH.

    Pins a `command` job to a single node (by worker id or name) through the
    existing job channel, streams its output live, and exits with the job's exit
    code. For debugging/operating a pull-based fleet where nodes have no inbound
    SSH and changing IPs.

      roost exec gpu-box -- nvidia-smi
      roost exec gpu-box "df -h /data && free -m"
      roost exec 7f3a... --timeout 10 -- ./long_diagnostic.sh
    """
    cmd = " ".join(command)
    url, token, _ = _resolve(ctx)

    # Optionally validate the target exists up front so we fail fast with an
    # actionable message instead of submitting a job that sits queued forever.
    if not no_validate:
        with _client(url, token) as c:
            try:
                r = c.get("/workers")
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise click.ClickException(f"cannot reach control plane at {url}: {e}")
            target = _resolve_target(r.json(), worker)
        click.echo(f"[exec] → {target['name']} ({target['id'][:8]})  $ {cmd}")
    else:
        click.echo(f"[exec] → {worker}  $ {cmd}")

    # PINNED CONTRACT: `target` hard-pins the job to one worker (id OR name).
    body: dict[str, Any] = {
        "kind": "command",
        "command": cmd,
        "target": worker,
        "budget": {"max_wallclock_min": timeout_min},
    }
    with _client(url, token) as c:
        r = c.post("/jobs", json=body)
        if r.status_code >= 400:
            raise click.ClickException(f"exec failed: HTTP {r.status_code}: {r.text}")
        job = r.json()
    if detach:
        click.echo(f"submitted: {job['id']} (state={job['state']})")
        return
    rc = _stream(url, token, job["id"])
    sys.exit(rc)


@cli.command()
@click.argument("job_id")
@click.option("--verbose", "-v", is_flag=True)
@click.pass_context
def status(ctx: click.Context, job_id: str, verbose: bool) -> None:
    """Show job details."""
    with _ctx_client(ctx) as c:
        r = c.get(f"/jobs/{job_id}")
        if r.status_code == 404:
            raise click.ClickException("job not found")
        r.raise_for_status()
        _print_job(r.json(), verbose=verbose)


@cli.command()
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True,
              help="Emit the annotated tree as JSON (incl. liveness facts) — for agents.")
@click.option("--health", is_flag=True,
              help="Append per-job liveness facts (activity / idle / queued / capable).")
@click.pass_context
def tree(ctx: click.Context, job_id: str, as_json: bool, health: bool) -> None:
    """Show a job's lineage tree (root → children → ...)."""
    with _ctx_client(ctx) as c:
        r = c.get(f"/jobs/{job_id}/tree")
        if r.status_code == 404:
            raise click.ClickException("job not found")
        r.raise_for_status()
        jobs = r.json()
    if as_json:
        click.echo(json.dumps(jobs, indent=2))
        return
    if not jobs:
        click.echo("(empty)")
        return
    by_parent: dict[Optional[str], list[dict]] = {}
    for j in jobs:
        by_parent.setdefault(j.get("parent_job_id"), []).append(j)
    # Roots = jobs whose parent isn't in this result set.
    ids = {j["id"] for j in jobs}
    roots = [j for j in jobs if j.get("parent_job_id") not in ids]

    def render(job: dict, prefix: str) -> None:
        intent = (job.get("intent") or "").replace("\n", " ")
        if not intent and job.get("spec"):
            cmd = job["spec"].get("command")
            intent = (cmd if isinstance(cmd, str) else " ".join(cmd)) if cmd else ""
        if len(intent) > 50:
            intent = intent[:47] + "..."
        toks = f" {job['tokens_used']}tok" if job.get("tokens_used") else ""
        click.echo(
            f"{prefix}{job['id']}  {job['state']:<10} "
            f"{job.get('worker_id') or '-':<14}{toks}  {intent}"
        )
        if health:
            bits = []
            if job.get("idle_sec") is not None:
                bits.append(f"idle {job['idle_sec']}s")
            if job.get("queued_sec") is not None:
                bits.append(f"queued {job['queued_sec']}s")
            if job.get("capable_workers") is not None:
                bits.append(f"capable={job['capable_workers']}")
            if job.get("last_activity"):
                bits.append(f"· {job['last_activity']}")
            if bits:
                click.echo(f"{prefix}    └ " + "  ".join(bits))
        for child in by_parent.get(job["id"], []):
            render(child, prefix + "  ")

    for root in roots:
        render(root, "")


@cli.command()
@click.argument("job_id")
@click.option("--follow/--no-follow", default=False, help="Tail until job finishes.")
@click.option("--since", default=0, type=int, help="Start from this seq (exclusive).")
@click.pass_context
def logs(ctx: click.Context, job_id: str, follow: bool, since: int) -> None:
    """Dump (or follow) job logs."""
    url, token, _ = _resolve(ctx)
    if follow:
        rc = _stream(url, token, job_id, since=since)
        sys.exit(rc)
    with _client(url, token) as c:
        r = c.get(f"/jobs/{job_id}/logs", params={"since": since})
        if r.status_code == 404:
            raise click.ClickException("job not found")
        r.raise_for_status()
        payload = r.json()
        for log in payload.get("logs", []):
            click.echo(f"[{log['seq']:>4} {log['stream']}] {log['data']}")
        click.echo(f"-- state: {payload['state']} --")


@cli.command()
@click.argument("job_id")
@click.option("--tree", "as_tree", is_flag=True, help="Cancel the whole subtree rooted here.")
@click.pass_context
def cancel(ctx: click.Context, job_id: str, as_tree: bool) -> None:
    """Cancel a queued or running job (optionally its whole subtree)."""
    with _ctx_client(ctx) as c:
        r = c.delete(f"/jobs/{job_id}", params={"tree": as_tree})
        if r.status_code == 409:
            raise click.ClickException("job not cancellable (missing or already finished)")
        r.raise_for_status()
        count = r.json().get("cancelled", 1)
        click.echo(f"cancelled {count} job(s)" if as_tree else "cancelled")


@cli.command("jobs")
@click.option("--state", default=None,
              type=click.Choice(["queued", "assigned", "running", "succeeded",
                                 "failed", "cancelled"]))
@click.option("--root", default=None, help="Filter to a lineage root job id.")
@click.option("--limit", default=20, type=int, show_default=True)
@click.pass_context
def list_jobs_cmd(ctx: click.Context, state: Optional[str], root: Optional[str],
                  limit: int) -> None:
    """List jobs."""
    params: dict[str, Any] = {"limit": limit}
    if state:
        params["state"] = state
    if root:
        params["root"] = root
    with _ctx_client(ctx) as c:
        r = c.get("/jobs", params=params)
        r.raise_for_status()
        for job in r.json():
            intent = (job.get("intent") or "").replace("\n", " ")
            if len(intent) > 60:
                intent = intent[:57] + "..."
            click.echo(f"{job['id']}  {job['state']:<10}  {job.get('worker_id') or '-':<14}  {intent}")


@cli.command("history")
@click.option("--limit", default=20, type=int, show_default=True,
              help="How many recent finished runs to show.")
@click.option("--failed", "failed_only", is_flag=True,
              help="Only show runs that failed or were not verified.")
@click.option("--json", "as_json", is_flag=True, help="Emit the runs as JSON.")
@click.pass_context
def history_cmd(ctx: click.Context, limit: int, failed_only: bool, as_json: bool) -> None:
    """What this fleet has run — recent finished goals, outcome, worker, cost.

    A 'what have I done' view over the goal memory the jobs table already keeps:
    each terminal run with its outcome (verified ✓ / failed ✗ / done), the worker
    that ran it, cost, and how long ago.

      roost history                # last 20 finished runs
      roost history --failed       # only the ones that need a second look
      roost history --limit 50 --json
    """
    # Over-fetch a little so filtering to terminal/real-goal runs still fills the
    # requested limit. /derived already computes health / verified / cost.
    fetch = max(limit * 3, limit + 20)
    with _ctx_client(ctx) as c:
        try:
            r = c.get("/derived", params={"limit": fetch})
        except httpx.HTTPError as e:
            raise click.ClickException(f"cannot reach control plane: {e}")
        if r.status_code >= 400:
            raise click.ClickException(f"control plane error: HTTP {r.status_code}: {r.text}")
        runs = (r.json() or {}).get("runs", [])

    selected = _history_runs(runs, failed_only=failed_only)[:limit]

    if as_json:
        click.echo(json.dumps(selected, indent=2))
        return
    if not selected:
        click.echo("No finished runs yet."
                   + (" (none failed)" if failed_only else
                      " Try `roost do \"...\"` to run something."))
        return

    now = time.time()
    rows = [_history_row(run, now) for run in selected]
    # Align the outcome column so the goals line up regardless of color codes.
    outcome_w = max(len(label) for _, label, *_ in rows)
    cost_w = max((len(cost) for *_, cost, _, _ in rows), default=0)
    for short_id, label, color, worker, cost_str, age, goal in rows:
        painted = click.style(label.ljust(outcome_w), fg=color) if color \
            else label.ljust(outcome_w)
        click.echo(
            f"{short_id:<8}  {painted}  {worker:<14}  "
            f"{cost_str:>{cost_w}}  {age:>4}  {goal}"
        )


@cli.command("workers")
@click.pass_context
def list_workers_cmd(ctx: click.Context) -> None:
    """List registered workers."""
    with _ctx_client(ctx) as c:
        r = c.get("/workers")
        r.raise_for_status()
        for w in r.json():
            caps = w.get("capabilities", {})
            summary_bits = []
            if "hostname" in caps:
                summary_bits.append(caps["hostname"])
            if "gpu_vram_gb" in caps:
                summary_bits.append(f"gpu:{caps['gpu_vram_gb']}GB")
            if "arch" in caps:
                summary_bits.append(caps["arch"])
            load = caps.get("load") or {}
            if load.get("running"):
                summary_bits.append(f"running:{load['running']}")
            if load.get("free_vram_gb") is not None:
                summary_bits.append(f"vramfree:{load['free_vram_gb']}GB")
            if load.get("loadavg1") is not None:
                summary_bits.append(f"load:{load['loadavg1']}")
            summary = " ".join(summary_bits)
            age = time.time() - w["last_seen"]
            click.echo(
                f"{w['id']}  {w['name']:<20}  {w['status']:<6}  "
                f"seen {age:>5.0f}s ago  {summary}"
            )


@cli.command()
@click.pass_context
def ping(ctx: click.Context) -> None:
    """Verify the control plane is reachable."""
    with _ctx_client(ctx) as c:
        try:
            r = c.get("/healthz")
        except httpx.HTTPError as e:
            raise click.ClickException(f"connect failed: {e}")
    if r.status_code >= 400:
        raise click.ClickException(f"HTTP {r.status_code}: {r.text}")
    click.echo(r.json())


def _stream(url: str, token: str, job_id: str, since: int = 0) -> int:
    """Stream a job's SSE feed to stdout. Returns a process-style exit code."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    exit_code = 0
    with httpx.Client(base_url=url.rstrip("/"), headers=headers,
                      timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)) as c:
        with c.stream("GET", f"/jobs/{job_id}/stream", params={"since": since}) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise click.ClickException(f"stream failed: HTTP {resp.status_code}: {resp.text}")
            for event, data in _iter_sse(resp):
                if event == "log":
                    click.echo(f"[{data.get('seq', '?'):>4} {data.get('stream', '?')}] {data.get('data', '')}")
                elif event == "state":
                    click.echo(f"-- state: {data.get('state')} --")
                elif event == "done":
                    state = data.get("state")
                    ec = data.get("exit_code")
                    if state == "succeeded":
                        exit_code = 0
                    elif ec is not None:
                        exit_code = ec if ec > 0 else 1
                    else:
                        exit_code = 1
                    click.echo(f"-- done: {state} (exit_code={ec}) --")
                    if data.get("error"):
                        click.echo(f"-- error: {data['error']} --")
                elif event == "error":
                    raise click.ClickException(data.get("error", "stream error"))
    return exit_code


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
