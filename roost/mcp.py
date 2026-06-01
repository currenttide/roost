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
        "name": "roost_submit",
        "description": (
            "Submit a sub-job to the Roost fleet. Returns immediately with "
            "{job_id, state, depth, root_job_id}. Use roost_wait to block "
            "until completion. The job inherits the caller's job as parent, "
            "so depth/tree-budget guardrails apply."
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
        "description": "Return a job's logs (optionally since a given seq).",
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
        "description": "Cancel a job (set tree=true to cancel its descendants too).",
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
        "description": "List registered workers and a summary of their capabilities.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------- tool implementations ----------


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


TOOL_IMPL = {
    "roost_submit":  tool_roost_submit,
    "roost_status":  tool_roost_status,
    "roost_wait":    tool_roost_wait,
    "roost_logs":    tool_roost_logs,
    "roost_cancel":  tool_roost_cancel,
    "roost_workers": tool_roost_workers,
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
