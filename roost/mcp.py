"""roost-mcp: stdio MCP server that lets a Claude Code agent dispatch sub-jobs.

Started by the worker as a local stdio subprocess when a job sets
``hierarchy.can_dispatch=true``. Reads ROOST_URL + ROOST_TOKEN +
ROOST_PARENT_JOB_ID from the environment; every ``roost_submit`` call
attaches ``parent_job_id`` so the control plane can enforce depth and
tree-budget guardrails.

Protocol: line-delimited JSON-RPC 2.0 over stdin/stdout, MCP-shaped
(initialize / tools/list / tools/call).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

import httpx

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "roost-mcp"
SERVER_VERSION = "0.2.0"


def _client() -> httpx.Client:
    url = os.environ.get("ROOST_URL", "http://127.0.0.1:8787")
    token = os.environ.get("ROOST_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=30.0)


def _parent_id() -> Optional[str]:
    return os.environ.get("ROOST_PARENT_JOB_ID") or None


TOOLS: list[dict[str, Any]] = [
    {
        "name": "roost_do",
        "description": (
            "THE main tool: do a plain-language GOAL on the fleet. The goal is first "
            "CLASSIFIED (same trust loop as the CLI): a multi-part goal is dispatched "
            "via the captain; an AMBIGUOUS goal comes back with a clarifying_question to "
            "answer and re-call (nothing runs); a DESTRUCTIVE goal (or one that couldn't "
            "be classified) requires you to re-call with confirm: true (nothing runs "
            "otherwise). A single, safe goal runs: a worker self-selects the best-fit "
            "node, runs it, and an INDEPENDENT verifier checks the goal was actually "
            "achieved (and self-heals a wrong result). On a run it returns {run_id, "
            "state} — call roost_result to get the verified outcome + evidence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What you want done, in plain language."},
                "verify": {"type": "boolean", "default": True,
                           "description": "Independently verify the result (default true)."},
                "model": {"type": "string", "description": "Optional model override."},
                "wallclock_min": {"type": "number", "default": 15},
                "confirm": {"type": "boolean", "default": False,
                            "description": "Required true to proceed when the goal is "
                                           "destructive or could not be classified."},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "roost_runs",
        "description": (
            "The inbox: recent + in-flight goals, each with its phase (running / "
            "verifying / self-healing / done), whether it was verified, and a one-line "
            "result. Reach for this to answer 'what's running?' / 'how did that go?' / "
            "'why did it fail?'. Returns {runs: [{run_id, goal, phase, verified, "
            "result, worker}]}. Gotcha: results are truncated one-liners — use "
            "roost_result(run_id) for the full verified outcome + evidence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 15}},
        },
    },
    {
        "name": "roost_result",
        "description": (
            "Wait for a run to finish and return its verified outcome: {state, verified, "
            "evidence, output}. Block up to timeout_sec. This is how you report back to "
            "the user with proof, not just 'it ran'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 900},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "roost_capabilities",
        "description": (
            "Describe what this fleet can do — live node count, total CPU cores, and "
            "GPU nodes (model + VRAM) — in plain language. Reach for this to answer "
            "'what can this run?' or to size a job before submitting. Returns {nodes, "
            "cpu_cores, gpu_nodes, can}. Gotcha: only counts ONLINE (idle/busy) "
            "workers, so offline nodes don't show."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "roost_submit",
        "description": (
            "Submit a precisely-shaped sub-job — reach for this (over roost_do) when "
            "you need explicit control: a docker/GPU image, hard `requires` "
            "constraints, a budget, or to fan out children yourself. Returns "
            "immediately with {job_id, state, depth, root_job_id}; block with "
            "roost_wait. Gotcha: the job inherits the caller's job as parent, so "
            "depth/tree-budget guardrails apply — a 409 comes back as "
            "{error: guardrail}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string",
                           "description": "Natural-language task for a `claude -p` job."},
                "command": {"description": "Shell command (string) or argv (array). "
                                           "Use this OR intent. For kind=docker this is the "
                                           "command run INSIDE the container."},
                "kind": {"type": "string", "enum": ["claude", "codex", "docker"]},
                "image": {"type": "string",
                          "description": "kind=docker only: container image to run the job in "
                                         "(e.g. pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime)."},
                "container": {"type": "object",
                              "description": "kind=docker only: container run options. Keys: "
                                             "gpus ('all' or 'device=0'), cpus, memory, shm_size, "
                                             "network, workdir, volumes (array of 'HOSTPATH:CTRPATH[:ro]'), "
                                             "env (object). Pass as a real JSON object, not a string."},
                "requires": {"type": "object",
                             "description": "HARD capability constraints (gpu_vram_gb, docker, "
                                            "docker_gpu, tools, repo, hostname, etc). The worker "
                                            "must satisfy all."},
                "prefer": {"type": "object",
                           "description": "SOFT placement hint, e.g. {\"worker\": \"<worker_id>\"}. "
                                          "Among capable workers the scheduler favors this one and "
                                          "briefly holds the job for it; falls back to any capable "
                                          "worker if it doesn't poll in time."},
                "budget": {"type": "object",
                           "description": "max_tokens / max_wallclock_min."},
                "permissions": {"type": "object"},
                "model": {"type": "string"},
                "subagent_model": {"type": "string"},
                "hierarchy": {"type": "object",
                              "description": "Pass {can_dispatch: true} to let this child "
                                             "spawn grandchildren."},
                "cwd": {"type": "string"},
                "env": {"type": "object"},
                "args": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "roost_status",
        "description": (
            "Get current state and metadata for a job_id. Includes liveness FACTS "
            "for judging health: `last_activity` (compact 'what it's doing now'), "
            "`idle_sec` (seconds since last sign of life — large + running may mean "
            "stuck), `queued_sec`, and `capable_workers` (online workers that satisfy "
            "the job's requires — 0 on a queued job means it can NEVER be placed as "
            "specified). Interpret these yourself; the control plane reports facts, "
            "not verdicts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "roost_wait",
        "description": (
            "Block until the job reaches a terminal state (succeeded/failed/cancelled) "
            "or timeout_sec elapses. Returns the final job record including result, "
            "exit_code, error, tokens_used."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 600},
                "poll_interval_sec": {"type": "number", "default": 1.0},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "roost_logs",
        "description": (
            "Return a job's captured stdout/stderr log lines. Reach for this to see "
            "WHAT a job actually printed — progress, errors, output — after "
            "roost_status/roost_wait tells you the state. Returns {logs: [{seq, "
            "stream, data}]}. Gotcha: logs are paginated — pass `since` (the last seq "
            "you saw) to tail incrementally rather than re-reading from the top."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "since": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 500},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "roost_cancel",
        "description": (
            "Cancel a job — stop a run that's stuck, runaway, or no longer needed. "
            "Set tree=true to also cancel everything it spawned (the whole subtree). "
            "Returns the updated job record. Gotcha: a job already in a terminal "
            "state can't be cancelled and comes back as {error: not_cancellable}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "tree": {"type": "boolean", "default": False},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "roost_workers",
        "description": (
            "List registered workers with their id, name, status, and full "
            "capabilities (os, arch, hostname, cpus, gpu, tools, …). Reach for this "
            "to pick a target for roost_exec/send_file, or to debug why a job won't "
            "place. Returns {workers: [...]}. Gotcha: includes offline/stale rows — "
            "check `status` (idle/busy = reachable) before pinning work to one."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "roost_exec",
        "description": (
            "Run a shell command on ONE specific fleet worker — no SSH. Reach for "
            "this to operate/debug a node directly (changing IPs, no inbound SSH). "
            "Hard-pins a `command` job to the named node (id OR name), waits for it, "
            "and returns {job_id, state, exit_code, output, worker}. Gotcha: if a "
            "NAME matches several online workers it errors — use an id; set "
            "wait:false to submit and return the job_id without blocking."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string",
                           "description": "Target worker — its id OR name."},
                "command": {"description": "Shell command (string) or argv (array)."},
                "timeout_min": {"type": "number", "default": 2,
                                "description": "Hard wall-clock budget in minutes."},
                "wait": {"type": "boolean", "default": True,
                         "description": "Block until the command finishes (default true)."},
            },
            "required": ["worker", "command"],
        },
    },
    {
        "name": "stage_file",
        "description": (
            "Stage a LOCAL file into the control-plane blob store (POST /blobs) and "
            "get back a PRESIGNED get_url that carries its own auth. Reach for this "
            "when you want a worker/job to pull a file without handing it any "
            "credentials — drop the get_url straight into a goal ('fetch <url> and "
            "run it'). Returns {id, name, size, sha256, get_url, expires_at}. Gotcha: "
            "it's a staging area, not a fileserver — blobs are size-capped and expire "
            "(default 24h); pass ttl_sec to extend up to the CP's ceiling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Local file to upload."},
                "ttl_sec": {"type": "number",
                            "description": "How long the staged blob lives (seconds). "
                                           "Default 24h; clamped to the CP ceiling."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "send_file",
        "description": (
            "Push a LOCAL file TO a worker — the no-SSH `scp`. Stages the file, then "
            "hard-pins a delivery `command` job to that node which mkdir -ps the "
            "destination dir, curls the presigned blob, and sha256-verifies it "
            "(exit 1 on mismatch). Reach for this to seed a node with a dataset, "
            "script, or model. Returns {job_id, destination, worker, blob}; poll it "
            "with roost_wait/roost_status. Gotcha: the destination is a path on the "
            "REMOTE worker, and the parent dir is created for you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string",
                           "description": "Target worker — its id OR name."},
                "local_path": {"type": "string", "description": "Local file to send."},
                "destination_path": {"type": "string",
                                     "description": "Absolute path to write on the worker."},
                "ttl_sec": {"type": "number",
                            "description": "Blob TTL (seconds) — must outlast delivery."},
            },
            "required": ["worker", "local_path", "destination_path"],
        },
    },
    {
        "name": "fetch_file",
        "description": (
            "Pull a file FROM a worker to here — the reverse of send_file. Mints a "
            "presigned upload slot, hard-pins a `command` job that curl -T uploads "
            "the remote file into it, waits for that job, then downloads the blob to "
            "local_path. Reach for this to retrieve a result/log/artifact off a node "
            "with no inbound SSH. Returns {local_path, size, worker, job_id}. Gotcha: "
            "this blocks until the remote upload finishes; local_path defaults to the "
            "remote basename in the current directory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string",
                           "description": "Source worker — its id OR name."},
                "remote_path": {"type": "string",
                                "description": "Absolute path to read on the worker."},
                "local_path": {"type": "string",
                               "description": "Where to write it locally "
                                              "(default: basename in cwd)."},
                "timeout_min": {"type": "number", "default": 5,
                                "description": "Budget for the remote upload job."},
            },
            "required": ["worker", "remote_path"],
        },
    },
    {
        "name": "list_staged",
        "description": (
            "List files currently staged in the blob store (GET /blobs) with their "
            "presigned get_urls. Use to see what's available to hand to a job, or to "
            "find a stale upload. Returns {staged: [{id, name, size, sha256, state, "
            "get_url, expires_at}]}. Gotcha: expired blobs are swept and won't appear; "
            "a `pending` state means an upload (fetch) hasn't finished yet."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------- tool implementations ----------


def _goal_of(job: dict) -> str:
    spec = job.get("spec") or {}
    g = spec.get("task") or spec.get("intent") or spec.get("command") or ""
    return (g if isinstance(g, str) else " ".join(g))[:120]


def _phase_of(job: dict) -> str:
    state = job.get("state", "?")
    if state in ("succeeded", "failed", "cancelled"):
        return state
    act = job.get("last_activity") or ""
    if "verifying" in act:
        return "verifying"
    if "self-healing" in act:
        return "self-healing"
    return state


def _run_summary(job: dict) -> dict:
    res = job.get("result") if isinstance(job.get("result"), dict) else {}
    return {
        "run_id": job.get("id"),
        "goal": _goal_of(job),
        "phase": _phase_of(job),
        "verified": res.get("verified"),
        "result": (res.get("output") or res.get("evidence") or job.get("error") or "")[:200],
        "worker": job.get("worker_id"),
    }


def tool_roost_do(args: dict) -> dict:
    """The trust loop, mirrored for MCP (which cannot interactively prompt):

    - multi-part goal → dispatch via the captain (same path as CLI `do`).
    - ambiguous → return the clarifying_question; the caller refines and calls again.
    - destructive OR classify-failed → require an explicit `confirm: true`; without
      it, return what will happen and that confirmation is required (do NOT run).
    - single, safe → post a kind: auto job (the original behavior).
    """
    from . import cli as _cli

    goal = args["goal"]
    plan = _cli._classify_goal(goal)

    # Mirror the CLI ordering exactly (roost/cli.py `do_`): ambiguity and the
    # needs-confirm gate come FIRST, before routing multi→dispatch / single→run.
    # A destructive (or classify-failed) goal must demand confirmation regardless
    # of whether it's single OR multi — otherwise a destructive multi could be
    # dispatched to the captain with no confirm.
    if plan["ambiguous"] and plan["clarifying_question"]:
        return {"needs": "clarification",
                "clarifying_question": plan["clarifying_question"],
                "note": "ambiguous goal — refine it (or fold the answer into the goal) "
                        "and call roost_do again. Nothing was run."}

    if _cli._needs_confirm(plan) and not args.get("confirm"):
        reason = ("could not classify the goal (fail-closed)"
                  if plan.get("classify_failed") else "looks destructive")
        return {"needs": "confirmation", "reason": reason,
                "will_do": plan["restated"],
                "note": "this goal " + reason + " — call roost_do again with "
                        "confirm: true to proceed. Nothing was run."}

    if plan["mode"] == "multi":
        url = os.environ.get("ROOST_URL", "http://127.0.0.1:8787")
        token = os.environ.get("ROOST_TOKEN", "")
        try:
            root_id, rc = _cli.dispatch_goal(url, token, goal, model=args.get("model"))
        except FileNotFoundError as e:
            return {"error": "captain_unavailable", "detail": str(e),
                    "note": "the captain needs Claude Code (`claude`) on PATH."}
        return {"run_id": root_id, "state": "succeeded" if rc == 0 else "failed",
                "mode": "multi",
                "note": "multi-step goal dispatched via the captain; inspect the plan "
                        f"with roost_status/roost_logs on {root_id}."}

    body: dict[str, Any] = {
        "kind": "auto",
        "task": goal,
        "verify": args.get("verify", True),
        "budget": {"max_wallclock_min": args.get("wallclock_min", 15), "max_tokens": 200000},
    }
    if args.get("model"):
        body["model"] = args["model"]
    parent = _parent_id()
    if parent:
        body["parent_job_id"] = parent
    with _client() as c:
        r = c.post("/jobs", json=body)
        r.raise_for_status()
        j = r.json()
    return {"run_id": j.get("id"), "state": j.get("state"),
            "note": "started — call roost_result(run_id) to get the verified outcome."}


def tool_roost_runs(args: dict) -> dict:
    limit = int(args.get("limit", 15))
    with _client() as c:
        r = c.get("/jobs", params={"limit": limit})
        r.raise_for_status()
        jobs = r.json()
    return {"runs": [_run_summary(j) for j in jobs]}


def tool_roost_result(args: dict) -> dict:
    final = tool_roost_wait({"job_id": args["run_id"],
                             "timeout_sec": args.get("timeout_sec", 900)})
    res = final.get("result") if isinstance(final.get("result"), dict) else {}
    return {
        "run_id": final.get("id"),
        "state": final.get("state"),
        "verified": res.get("verified"),
        "evidence": res.get("evidence"),
        "output": res.get("output") or (final.get("result") if not res else None),
        "error": final.get("error"),
        "tokens_used": final.get("tokens_used"),
        "timed_out_waiting": final.get("timed_out_waiting"),
    }


def tool_roost_capabilities(_args: dict) -> dict:
    with _client() as c:
        r = c.get("/workers")
        r.raise_for_status()
        workers = r.json()
    live = [w for w in workers if w.get("status") in ("idle", "busy")]
    cores = sum((w["capabilities"].get("cpus") or 0) for w in live)
    gpus = []
    for w in live:
        cp = w["capabilities"]
        n = cp.get("gpu_count") or 0
        if n:
            gpus.append({"node": w["name"], "count": n,
                         "gpu": (cp.get("gpu") or ["GPU"])[0], "vram_gb": cp.get("gpu_vram_gb")})
    return {"nodes": len(live), "cpu_cores": cores, "gpu_nodes": gpus,
            "can": "Run anything stated in plain language via roost_do; it picks the best "
                   "node and verifies the result. CPU work, agent tasks, and GPU/training "
                   "jobs on the GPU nodes."}


def tool_roost_submit(args: dict) -> dict:
    body = dict(args)
    parent = _parent_id()
    if parent:
        body["parent_job_id"] = parent
    with _client() as c:
        r = c.post("/jobs", json=body)
        if r.status_code == 409:
            return {
                "error": "guardrail",
                "detail": r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text,
            }
        r.raise_for_status()
        return r.json()


def tool_roost_status(args: dict) -> dict:
    with _client() as c:
        r = c.get(f"/jobs/{args['job_id']}")
        r.raise_for_status()
        return r.json()


def tool_roost_wait(args: dict) -> dict:
    job_id = args["job_id"]
    timeout_sec = float(args.get("timeout_sec", 600))
    poll = float(args.get("poll_interval_sec", 1.0))
    deadline = time.time() + timeout_sec
    with _client() as c:
        last: dict[str, Any] = {}
        while time.time() < deadline:
            r = c.get(f"/jobs/{job_id}")
            r.raise_for_status()
            last = r.json()
            if last["state"] in ("succeeded", "failed", "cancelled"):
                return last
            time.sleep(poll)
        last["timed_out_waiting"] = True
        return last


def tool_roost_logs(args: dict) -> dict:
    with _client() as c:
        r = c.get(
            f"/jobs/{args['job_id']}/logs",
            params={"since": args.get("since", 0), "limit": args.get("limit", 500)},
        )
        r.raise_for_status()
        return r.json()


def tool_roost_cancel(args: dict) -> dict:
    with _client() as c:
        r = c.delete(
            f"/jobs/{args['job_id']}", params={"tree": args.get("tree", False)}
        )
        if r.status_code == 409:
            return {"error": "not_cancellable", "detail": r.text}
        r.raise_for_status()
        return r.json()


def tool_roost_workers(_args: dict) -> dict:
    with _client() as c:
        r = c.get("/workers")
        r.raise_for_status()
        return {"workers": r.json()}


def tool_roost_exec(args: dict) -> dict:
    """Run a shell command on ONE specific worker via a hard-pinned command job.

    Validates the target (id OR name) against GET /workers — clear error on no
    match / ambiguous online name — then submits a `command` job carrying the
    pinned `target` field (the control-plane contract) and waits for it.
    """
    from . import cli as _cli

    worker = args["worker"]
    command = args["command"]
    cmd = command if isinstance(command, str) else " ".join(command)
    with _client() as c:
        r = c.get("/workers")
        r.raise_for_status()
        workers = r.json()
    try:
        target = _cli._resolve_target(workers, worker)
    except Exception as e:  # click.ClickException (or any) → a clean tool error
        return {"error": "bad_target", "detail": getattr(e, "message", str(e))}

    # PINNED CONTRACT: `target` hard-pins the job to one worker (id OR name).
    body: dict[str, Any] = {
        "kind": "command",
        "command": cmd,
        "target": worker,
        "budget": {"max_wallclock_min": args.get("timeout_min", 2)},
    }
    parent = _parent_id()
    if parent:
        body["parent_job_id"] = parent
    with _client() as c:
        r = c.post("/jobs", json=body)
        r.raise_for_status()
        job = r.json()
    if not args.get("wait", True):
        return {"job_id": job.get("id"), "state": job.get("state"),
                "worker": f"{target.get('name')} ({target.get('id')})",
                "note": "submitted — call roost_wait(job_id)/roost_logs(job_id)."}
    final = tool_roost_wait({"job_id": job["id"], "timeout_sec": 600})
    logs = tool_roost_logs({"job_id": job["id"], "limit": 500})
    output = "\n".join(le.get("data", "") for le in logs.get("logs", []))
    return {
        "job_id": final.get("id"),
        "state": final.get("state"),
        "exit_code": final.get("exit_code"),
        "worker": f"{target.get('name')} ({target.get('id')})",
        "output": output,
        "error": final.get("error"),
        "timed_out_waiting": final.get("timed_out_waiting"),
    }


# ---------- transfer tools (blob store: stage / send / fetch) ----------


def _stage_blob(path: str, ttl_sec: Optional[float] = None) -> dict:
    """Upload a local file to POST /blobs; return its public blob dict."""
    import os.path as _osp

    name = _osp.basename(path) or "blob"
    with open(path, "rb") as f:
        data = f.read()
    params: dict[str, Any] = {"name": name}
    if ttl_sec:
        params["ttl_sec"] = ttl_sec
    with _client() as c:
        r = c.post("/blobs", params=params, content=data)
        r.raise_for_status()
        return r.json()


def _resolve_worker(worker: str) -> dict:
    """Resolve a worker id/name to its record (clear error on miss/ambiguity)."""
    from . import cli as _cli

    with _client() as c:
        r = c.get("/workers")
        r.raise_for_status()
        workers = r.json()
    return _cli._resolve_target(workers, worker)


def _sha_check_cmd(os_name: str, sha256: str, dest: str) -> str:
    """Shell snippet that verifies dest's sha256 == expected, exit 1 on mismatch.

    Uses shasum on darwin (no coreutils sha256sum), sha256sum elsewhere.
    """
    if (os_name or "").lower() == "darwin":
        got = f"$(shasum -a 256 {dest} | cut -d' ' -f1)"
    else:
        got = f"$(sha256sum {dest} | cut -d' ' -f1)"
    return (f'got={got}; '
            f'if [ "$got" != "{sha256}" ]; then '
            f'echo "sha256 mismatch: $got != {sha256}" >&2; exit 1; fi')


def tool_stage_file(args: dict) -> dict:
    blob = _stage_blob(args["path"], args.get("ttl_sec"))
    return {"id": blob["id"], "name": blob["name"], "size": blob["size"],
            "sha256": blob["sha256"], "get_url": blob["get_url"],
            "expires_at": blob["expires_at"]}


def tool_send_file(args: dict) -> dict:
    """Stage a local file, then hard-pin a delivery command job to the worker.

    The job mkdir -ps the destination dir, curls the presigned blob, and verifies
    the sha256 (exit 1 on mismatch) — sha256 the contract, the worker never sees a
    credential. Pinned via requires.hostname (== the worker's capabilities.hostname).
    """
    try:
        target = _resolve_worker(args["worker"])
    except Exception as e:  # click.ClickException (or any) → clean tool error
        return {"error": "bad_target", "detail": getattr(e, "message", str(e))}

    caps = target.get("capabilities") or {}
    hostname = caps.get("hostname")
    if not hostname:
        return {"error": "no_hostname",
                "detail": f"worker {target.get('name')} advertises no hostname "
                          "capability — cannot pin a delivery job to it."}

    blob = _stage_blob(args["local_path"], args.get("ttl_sec"))
    dest = args["destination_path"]
    sha_check = _sha_check_cmd(caps.get("os"), blob["sha256"], dest)
    command = (
        f'set -e; mkdir -p "$(dirname {dest})"; '
        f"curl -fsSL '{blob['get_url']}' -o {dest}; "
        f"{sha_check}"
    )

    body: dict[str, Any] = {
        "kind": "command",
        "command": command,
        "requires": {"hostname": f"=={hostname}"},
        "budget": {"max_wallclock_min": 10},
    }
    parent = _parent_id()
    if parent:
        body["parent_job_id"] = parent
    with _client() as c:
        r = c.post("/jobs", json=body)
        r.raise_for_status()
        job = r.json()
    return {"job_id": job.get("id"), "state": job.get("state"),
            "destination": dest,
            "worker": f"{target.get('name')} ({target.get('id')})",
            "blob": {"id": blob["id"], "size": blob["size"], "sha256": blob["sha256"]},
            "note": "delivery job submitted — poll roost_wait(job_id)/roost_status(job_id)."}


def tool_fetch_file(args: dict) -> dict:
    """Pull a remote file here: presign an upload slot, pin a curl -T upload job,
    wait for it, then download the blob to local_path."""
    import os.path as _osp

    try:
        target = _resolve_worker(args["worker"])
    except Exception as e:
        return {"error": "bad_target", "detail": getattr(e, "message", str(e))}

    caps = target.get("capabilities") or {}
    hostname = caps.get("hostname")
    if not hostname:
        return {"error": "no_hostname",
                "detail": f"worker {target.get('name')} advertises no hostname "
                          "capability — cannot pin a fetch job to it."}

    remote = args["remote_path"]
    local = args.get("local_path") or _osp.basename(remote) or "blob"

    with _client() as c:
        r = c.post("/blobs/presign", json={"name": _osp.basename(remote) or "blob"})
        r.raise_for_status()
        slot = r.json()

    # The worker-side leg: PUT the remote file into the presigned slot.
    command = f"curl -fsSL -T {remote} '{slot['put_url']}'"
    body: dict[str, Any] = {
        "kind": "command",
        "command": command,
        "requires": {"hostname": f"=={hostname}"},
        "budget": {"max_wallclock_min": args.get("timeout_min", 5)},
    }
    parent = _parent_id()
    if parent:
        body["parent_job_id"] = parent
    with _client() as c:
        r = c.post("/jobs", json=body)
        r.raise_for_status()
        job = r.json()

    final = tool_roost_wait({"job_id": job["id"],
                             "timeout_sec": float(args.get("timeout_min", 5)) * 60})
    if final.get("state") != "succeeded":
        logs = tool_roost_logs({"job_id": job["id"], "limit": 200})
        output = "\n".join(le.get("data", "") for le in logs.get("logs", []))
        return {"error": "fetch_failed", "job_id": job.get("id"),
                "state": final.get("state"), "detail": final.get("error"),
                "output": output,
                "note": "the remote upload job did not succeed; the file was not fetched."}

    with _client() as c:
        r = c.get(f"/blobs/{slot['id']}")
        r.raise_for_status()
        content = r.content
    with open(local, "wb") as f:
        f.write(content)
    return {"local_path": _osp.abspath(local), "size": len(content),
            "worker": f"{target.get('name')} ({target.get('id')})",
            "job_id": job.get("id")}


def tool_list_staged(_args: dict) -> dict:
    with _client() as c:
        r = c.get("/blobs")
        r.raise_for_status()
        blobs = r.json()
    return {"staged": [
        {"id": b["id"], "name": b["name"], "size": b["size"], "sha256": b["sha256"],
         "state": b["state"], "get_url": b["get_url"], "expires_at": b["expires_at"]}
        for b in blobs
    ]}


TOOL_IMPL = {
    "roost_do":           tool_roost_do,
    "roost_runs":         tool_roost_runs,
    "roost_result":       tool_roost_result,
    "roost_capabilities": tool_roost_capabilities,
    "roost_submit":  tool_roost_submit,
    "roost_status":  tool_roost_status,
    "roost_wait":    tool_roost_wait,
    "roost_logs":    tool_roost_logs,
    "roost_cancel":  tool_roost_cancel,
    "roost_workers": tool_roost_workers,
    "roost_exec":    tool_roost_exec,
    "stage_file":    tool_stage_file,
    "send_file":     tool_send_file,
    "fetch_file":    tool_fetch_file,
    "list_staged":   tool_list_staged,
}


# ---------- JSON-RPC plumbing ----------


def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    e: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": e}


def handle(req: dict) -> Optional[dict]:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    # Notifications (no id) → no response.
    if req_id is None:
        return None

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"tools": {"listChanged": False}},
        })
    if method == "ping":
        return _ok(req_id, {})
    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        impl = TOOL_IMPL.get(name)
        if impl is None:
            return _err(req_id, -32601, f"unknown tool: {name}")
        try:
            payload = impl(arguments)
        except httpx.HTTPStatusError as e:
            return _ok(req_id, {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": f"http error {e.response.status_code}: {e.response.text}",
                }],
            })
        except Exception as e:  # noqa: BLE001
            return _ok(req_id, {
                "isError": True,
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
            })
        return _ok(req_id, {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        })
    return _err(req_id, -32601, f"method not found: {method}")


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.stdout.write(
                json.dumps(_err(None, -32700, f"parse error: {e}")) + "\n"
            )
            sys.stdout.flush()
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
