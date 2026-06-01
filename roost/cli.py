"""Unified CLI for Roost.

Control plane / fleet:

    roost serve              run the control plane
    roost enroll-token       (admin) mint a single-use enrollment token
    roost revoke <worker>    (admin) revoke a worker's credential

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


@cli.command()
@click.argument("goal")
@click.option("--model", default=None,
              help="Model for the captain agent (default: Claude Code's default)")
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
    from . import captain as _captain

    url, token, _ = _resolve(ctx)
    with _client(url, token) as c:
        try:
            r = c.get("/workers")
        except httpx.HTTPError as e:
            raise click.ClickException(f"cannot reach control plane at {url}: {e}")
        if r.status_code >= 400:
            raise click.ClickException(f"control plane error: HTTP {r.status_code}: {r.text}")
        workers = r.json()

        live = [w for w in workers if w.get("status") in ("idle", "busy")]
        if not live:
            click.echo("[roost] WARNING: no online workers; the captain has nowhere to "
                       "dispatch. Start one with `roost worker`.", err=True)

        # Anchor a captain-root so the whole plan shares one lineage tree and
        # one tree budget (V2-1). Sub-jobs attach to it via ROOST_PARENT_JOB_ID.
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
            raise click.ClickException(
                f"could not create captain-root job: HTTP {rr.status_code}: {rr.text}")
        root = rr.json()
        root_id = root["id"]
        click.echo(f"[roost] captain-root {root_id} (track the plan: roost tree {root_id})")

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
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    finally:
        # Close out the plan anchor whatever happened to the captain process.
        final_state = "succeeded" if rc == 0 else "failed"
        with _client(url, token) as c:
            try:
                c.post(f"/jobs/{root_id}/finalize", json={"state": final_state})
            except httpx.HTTPError:
                pass
    sys.exit(rc)


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
