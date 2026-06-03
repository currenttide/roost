"""Roost worker daemon (V1).

Long-running, supervisor-friendly. Each loop iteration:

  * heartbeat (15s) — renews lease on the assigned job (if any)
  * long-poll (≤ 30s) — picks up new work
  * execute — spawns subprocess for ``command`` jobs, ``claude -p`` for
    ``intent`` jobs (with sandbox + model routing + optional roost-mcp);
    streams stdout/stderr back to the control plane
  * re-register on heartbeat failure — survives laptop sleep/wake by
    forgetting the old session and starting a fresh one

Capability self-test on startup: each declared tool is probed (--version) and
only tools that respond are advertised, so jobs never land on a box that
claims a tool it doesn't actually have.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from . import config as roost_config
from . import triage
from . import verify as verify_mod

POLL_TIMEOUT = 25.0
HEARTBEAT_INTERVAL = 15.0
HEARTBEAT_FAIL_THRESHOLD = 3      # consecutive failures before re-registering
CREDS_REFRESH_INTERVAL = 1200.0  # re-pull Claude creds every 20 min (they rotate)
TOOL_PROBES: dict[str, list[str]] = {
    "claude":   ["claude", "--version"],
    "codex":    ["codex", "--version"],
    "git":      ["git", "--version"],
    "python":   ["python", "--version"],
    "python3":  ["python3", "--version"],
    "node":     ["node", "--version"],
    "pnpm":     ["pnpm", "--version"],
    "npm":      ["npm", "--version"],
    "uv":       ["uv", "--version"],
    "pip":      ["pip", "--version"],
    "pytest":   ["pytest", "--version"],
    "ruff":     ["ruff", "--version"],
    "make":     ["make", "--version"],
    "cargo":    ["cargo", "--version"],
    "go":       ["go", "version"],
    "docker":   ["docker", "--version"],
}


# ---------- Capability detection + self-test ----------


def _find_nvidia_smi() -> Optional[str]:
    """Locate nvidia-smi. On WSL2 the Windows driver exposes it at
    /usr/lib/wsl/lib/nvidia-smi, which is often NOT on a service's PATH — check
    there too so GPUs on Windows/WSL workers are detected."""
    p = shutil.which("nvidia-smi")
    if p:
        return p
    for cand in ("/usr/lib/wsl/lib/nvidia-smi",):
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _detect_gpus() -> list[dict]:
    nvidia_smi = _find_nvidia_smi()
    if not nvidia_smi:
        return []
    try:
        out = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5.0, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []
    gpus: list[dict] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            vram_mb = float(parts[1])
        except ValueError:
            continue
        gpus.append(
            {
                "name": parts[0],
                "vram_gb": round(vram_mb / 1024.0, 1),
                "driver": parts[2] if len(parts) > 2 else None,
            }
        )
    return gpus


def _detect_ram_gb() -> Optional[float]:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024.0 / 1024.0, 1)
    except OSError:
        pass
    return None


def _free_vram_gb() -> Optional[float]:
    """Total free VRAM across GPUs, re-queried live (cheap; ~ms)."""
    nvidia_smi = _find_nvidia_smi()
    if not nvidia_smi:
        return None
    try:
        out = subprocess.run(
            [nvidia_smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5.0, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    total = 0.0
    for line in out.strip().splitlines():
        try:
            total += float(line.strip())
        except ValueError:
            continue
    return round(total / 1024.0, 1)


def load_snapshot(running: int) -> dict[str, Any]:
    """Live worker load, refreshed on each heartbeat (V2-3).

    Cheap to compute; carried in heartbeat capabilities so the captain and the
    ranking matcher see the same picture.
    """
    snap: dict[str, Any] = {"running": running}
    try:
        snap["loadavg1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    free_vram = _free_vram_gb()
    if free_vram is not None:
        snap["free_vram_gb"] = free_vram
    return snap


def _probe_tool(name: str) -> bool:
    if shutil.which(name) is None:
        return False
    cmd = TOOL_PROBES.get(name)
    if cmd is None:
        return True  # on PATH and we have no probe recipe → trust it
    try:
        subprocess.run(
            cmd, capture_output=True, timeout=5.0, check=True
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _detect_docker() -> dict[str, Any]:
    """Probe whether this worker can run Docker jobs: daemon reachable, and
    whether containers can see GPUs (nvidia runtime registered). Used so the
    matcher can place `kind: docker` / GPU jobs only where they can actually run."""
    out: dict[str, Any] = {}
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            out["docker"] = True
            out["docker_gpu"] = "nvidia" in r.stdout.lower()
    except (subprocess.SubprocessError, OSError):
        pass
    return out


def detect_capabilities(
    extra: Optional[dict] = None,
    self_test: bool = True,
) -> dict[str, Any]:
    caps: dict[str, Any] = {
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "hostname": socket.gethostname(),
        "cpus": os.cpu_count() or 1,
        "python": platform.python_version(),
    }
    ram = _detect_ram_gb()
    if ram is not None:
        caps["ram_gb"] = ram
    gpus = _detect_gpus()
    if gpus:
        caps["gpu"] = [g["name"] for g in gpus]
        caps["gpu_count"] = len(gpus)
        caps["gpu_vram_gb"] = max(g["vram_gb"] for g in gpus)
        caps["gpus"] = gpus
    # Docker-as-executor capability: can this worker actually RUN containers
    # (daemon reachable), and can those containers see GPUs (nvidia runtime)?
    caps.update(_detect_docker())
    declared = list(TOOL_PROBES.keys())
    if self_test:
        passed = [t for t in declared if _probe_tool(t)]
    else:
        passed = [t for t in declared if shutil.which(t)]
    caps["tools"] = passed
    if extra:
        caps.update(extra)
    return caps


_SANDBOX_SUPPORTED: Optional[bool] = None


def _claude_supports_sandbox() -> bool:
    """Whether the local `claude` accepts `--sandbox` (absent on 2.1.x). Cached;
    probes `claude --help` once. Conservative: False if claude is missing or the
    probe fails."""
    global _SANDBOX_SUPPORTED
    if _SANDBOX_SUPPORTED is None:
        if shutil.which("claude") is None:
            _SANDBOX_SUPPORTED = False
        else:
            try:
                out = subprocess.run(
                    ["claude", "--help"], capture_output=True, text=True,
                    timeout=15, stdin=subprocess.DEVNULL,
                )
                _SANDBOX_SUPPORTED = "--sandbox" in (out.stdout + out.stderr)
            except (subprocess.SubprocessError, OSError):
                _SANDBOX_SUPPORTED = False
    return _SANDBOX_SUPPORTED


# ---------- Activity extraction (liveness) ----------


def activity_from_stream_json(obj: dict) -> Optional[str]:
    """Distil a claude/codex stream-json message into a compact 'what it's doing
    now' line for liveness reporting. Returns None if the message carries no
    human-meaningful activity. Pure; never raises on odd shapes."""
    try:
        mtype = obj.get("type")
        if mtype == "system":
            return f"init ({obj.get('subtype')})" if obj.get("subtype") else "init"
        if mtype == "result":
            return "done"
        msg = obj.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip().replace("\n", " ")
            return f"💬 {text[:80]}" if text else None
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "tool_use":
                    name = item.get("name") or "tool"
                    inp = item.get("input") or {}
                    hint = ""
                    if isinstance(inp, dict):
                        for k in ("command", "file_path", "path", "pattern", "query", "description"):
                            if inp.get(k):
                                hint = f" {str(inp[k]).splitlines()[0][:60]}"
                                break
                    return f"→ {name}{hint}"
                if itype == "tool_result":
                    return "✓ tool result"
                if itype == "text" and item.get("text", "").strip():
                    return f"💬 {item['text'].strip().replace(chr(10), ' ')[:80]}"
    except Exception:  # noqa: BLE001 — liveness must never break the relay
        return None
    return None


# ---------- Command construction ----------


def build_command(
    spec: dict,
    job_id: str,
    *,
    default_cwd: Optional[str] = None,
    worker_policy: Optional[dict] = None,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    can_dispatch: bool = False,
    triage_prompt: Optional[str] = None,
) -> tuple[list[str], str, list[Path]]:
    """Build (argv, cwd, extra_tempfiles). The tempfiles are cleaned up by caller."""
    cwd = spec.get("cwd") or default_cwd or os.getcwd()
    tempfiles: list[Path] = []

    # Bare-worker (kind: auto): a triage agent self-assesses fit then accepts (does
    # the task) or declines. It runs as a claude agent with a triage system prompt.
    if (spec.get("kind") or "").lower() == "auto":
        argv = _build_auto_argv(
            spec, job_id,
            worker_policy=worker_policy or {},
            base_url=base_url, token=token,
            can_dispatch=can_dispatch,
            triage_prompt=triage_prompt or "",
            tempfiles=tempfiles,
        )
        return argv, cwd, tempfiles

    # Docker-as-executor: run the job in a fresh, isolated container (GPU/limits
    # per job). Checked before `command` because a docker job carries BOTH an
    # `image` and the in-container `command`.
    if (spec.get("kind") or "").lower() == "docker":
        return _build_docker_argv(spec, job_id), cwd, tempfiles

    if spec.get("command"):
        cmd = spec["command"]
        if isinstance(cmd, str):
            return ["/bin/sh", "-c", cmd], cwd, tempfiles
        if isinstance(cmd, list):
            return list(cmd), cwd, tempfiles
        raise ValueError("`command` must be string or list")

    kind = (spec.get("kind") or "claude").lower()
    if kind == "claude":
        argv = _build_claude_argv(
            spec, job_id,
            worker_policy=worker_policy or {},
            base_url=base_url, token=token,
            can_dispatch=can_dispatch,
            tempfiles=tempfiles,
        )
        return argv, cwd, tempfiles
    if kind == "codex":
        return _build_codex_argv(spec), cwd, tempfiles
    raise ValueError(f"unknown kind: {kind!r} (and no `command` given)")


def docker_container_name(job_id: str) -> str:
    """Deterministic name for a job's container, so we can `docker kill` it."""
    return f"roost-job-{job_id}"


def _build_docker_argv(spec: dict, job_id: str) -> list[str]:
    """Build a `docker run` argv for a `kind: docker` job.

    Spec shape:
        kind: docker
        image: <image>
        command: "<cmd>" | [argv...]   # in-container command (optional)
        container:
          gpus: "all" | "device=0"
          cpus: "16"
          memory: "64g"
          volumes: ["/host:/ctr[:ro]", ...]   # HOST paths (DooD: sibling containers)
          env: {KEY: val, ...}
          workdir: /workspace
          network: host | <name>
          shm_size: "16g"
    """
    c = spec.get("container") or {}
    image = spec.get("image") or c.get("image")
    if not image:
        raise ValueError("docker job requires `image`")
    # --rm so the container is cleaned up; --name so cancel/timeout can kill it.
    argv = ["docker", "run", "--rm", "--name", docker_container_name(job_id)]
    gpus = c.get("gpus")
    # Robustness: if the job declares it needs a GPU (via requires) but forgot to
    # set container.gpus, default to all GPUs — otherwise the container has no
    # GPU and even `nvidia-smi` is absent.
    req = spec.get("requires") or {}
    if not gpus and (req.get("docker_gpu") or req.get("gpu_vram_gb") or req.get("gpu")):
        gpus = "all"
    if gpus:
        argv += ["--gpus", str(gpus)]
    if c.get("cpus"):
        argv += ["--cpus", str(c["cpus"])]
    if c.get("memory"):
        argv += ["--memory", str(c["memory"])]
    if c.get("shm_size"):
        argv += ["--shm-size", str(c["shm_size"])]
    if c.get("network"):
        argv += ["--network", str(c["network"])]
    if c.get("workdir"):
        argv += ["-w", str(c["workdir"])]
    for vol in c.get("volumes") or []:
        argv += ["-v", str(vol)]
    for key, val in (c.get("env") or {}).items():
        argv += ["-e", f"{key}={val}"]
    argv.append(str(image))
    cmd = spec.get("command")
    if cmd:
        if isinstance(cmd, str):
            argv += ["sh", "-c", cmd]
        elif isinstance(cmd, list):
            argv += [str(x) for x in cmd]
        else:
            raise ValueError("`command` must be string or list")
    return argv


def _intersect(requested: list, allowed: Optional[list]) -> list:
    if not allowed:
        return list(requested or [])
    a = set(allowed)
    return [x for x in (requested or []) if x in a]


def _build_claude_argv(
    spec: dict,
    job_id: str,
    *,
    worker_policy: dict,
    base_url: Optional[str],
    token: Optional[str],
    can_dispatch: bool,
    tempfiles: list[Path],
) -> list[str]:
    intent = spec.get("intent")
    if not intent:
        raise ValueError("claude job requires `intent`")
    if shutil.which("claude") is None:
        raise FileNotFoundError("`claude` CLI not on PATH")
    argv = ["claude", "-p", intent, "--output-format", "stream-json", "--verbose"]

    perms = spec.get("permissions") or {}
    trust_skip = worker_policy.get("trust_skip_perms", False)

    # Sandbox preference (Decision security): default sandbox unless trusted worker
    # is asked to skip permissions. `--sandbox` only exists on some Claude Code
    # builds (e.g. absent on 2.1.x); passing it where unsupported makes claude
    # exit immediately, so probe support and degrade gracefully.
    wants_skip = perms.get("dangerously_skip") or perms.get("mode") == "skip"
    if wants_skip and trust_skip:
        argv.append("--dangerously-skip-permissions")
    elif perms.get("sandbox", True):
        if _claude_supports_sandbox():
            argv.append("--sandbox")
        elif trust_skip:
            # Trusted personal worker without --sandbox support: run unsandboxed
            # but functional rather than crash on an unknown flag.
            argv.append("--dangerously-skip-permissions")
        else:
            argv += ["--permission-mode", "default"]
    elif perms.get("mode") in ("acceptEdits", "plan", "default", "bypassPermissions"):
        argv += ["--permission-mode", perms["mode"]]

    # Path + tool allowlist, intersected with worker policy.
    allow_paths = _intersect(perms.get("allow_paths") or [], worker_policy.get("allow_paths"))
    allow_cmds = _intersect(perms.get("allow_commands") or [], worker_policy.get("allow_commands"))
    for path in allow_paths:
        argv += ["--add-dir", path]
    tools = list(perms.get("allow_tools") or [])
    tools += [f"Bash({c})" for c in allow_cmds]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]

    # Model routing.
    if spec.get("model"):
        argv += ["--model", spec["model"]]

    # roost-mcp injection: write a temp mcp config and pass via --mcp-config.
    if can_dispatch and base_url and token:
        cfg = {
            "mcpServers": {
                "roost": {
                    "command": sys.executable,
                    "args": ["-m", "roost.mcp"],
                    "env": {
                        "ROOST_URL": base_url,
                        "ROOST_TOKEN": token,
                        "ROOST_PARENT_JOB_ID": job_id,
                    },
                }
            }
        }
        tmp = Path(tempfile.mkstemp(prefix=f"roost-mcp-{job_id}-", suffix=".json")[1])
        tmp.write_text(json.dumps(cfg))
        tempfiles.append(tmp)
        argv += ["--mcp-config", str(tmp)]

    if spec.get("args"):
        argv += list(spec["args"])
    return argv


# Default model for kind:auto triage+execution: Sonnet — capable enough to
# reliably introspect (run nvidia-smi etc.) and execute, while far cheaper than the
# Opus that drove the original "triage tax" (plan.md Phase 2/3). Override with
# `model:` in the spec (e.g. a smaller model for bulk/trivial work).
AUTO_DEFAULT_MODEL = "claude-sonnet-4-6"

# Self-healing (trust loop): if the independent verifier rejects a result, fix-and-
# re-verify on the same node up to this many times before reporting failure.
MAX_FIX_ATTEMPTS = 2

# Strong signals that a task genuinely needs a CUDA GPU. Conservative on purpose:
# only triggers an instant (no-LLM) decline on a node that has no GPU, so the worst
# case of a false positive is the task waiting for / escalating to another node.
_GPU_REQUIRE_RE = re.compile(
    r"\b(cuda|nvidia-smi|vram|cudnn|torch\.cuda|tensor\s*cores?)\b"
    r"|\b(needs?|requires?|use[sd]?|run\w*|train\w*|benchmark\w*)\b[^.\n]{0,24}\bgpu\b"
    r"|\bgpu\b[^.\n]{0,24}\b(required|needed|only|benchmark\w*|train\w*|matmul)\b",
    re.IGNORECASE,
)


def _wants_verify(spec: dict, is_auto: bool) -> bool:
    """Whether to run the independent verifier after a successful agentic job.
    Explicit `verify:` wins; otherwise auto/roost-run jobs verify by default (the
    trust loop), other kinds opt in. Never verify command/docker jobs here."""
    v = spec.get("verify")
    if v is not None:
        return bool(v)
    return is_auto


def _auto_prefilter(task: str, caps: dict) -> Optional[str]:
    """Cheap deterministic gate for kind:auto: return a decline reason for an obvious
    capability mismatch (so we skip the LLM triage call), else None to run the agent."""
    has_gpu = bool(caps.get("gpu_count")) or bool(caps.get("docker_gpu"))
    if not has_gpu and _GPU_REQUIRE_RE.search(task or ""):
        return "no GPU on this node (task requires CUDA/GPU)"
    return None


def _build_auto_argv(
    spec: dict,
    job_id: str,
    *,
    worker_policy: dict,
    base_url: Optional[str],
    token: Optional[str],
    can_dispatch: bool,
    triage_prompt: str,
    tempfiles: list[Path],
) -> list[str]:
    """Bare-worker triage: run a claude agent whose system prompt tells it to
    self-assess fit and either do the task or decline. The task is the `-p` prompt;
    the triage prompt is injected as the system prompt."""
    task = spec.get("task") or spec.get("intent")
    if not task:
        raise ValueError("auto job requires `task`")
    sub = dict(spec)
    sub["intent"] = task
    # An auto agent must be able to ACT (shell / write code / docker). Default to
    # sandboxed execution unless the spec pins permissions explicitly.
    if not sub.get("permissions"):
        sub["permissions"] = {"sandbox": True}
    if not sub.get("model"):
        sub["model"] = AUTO_DEFAULT_MODEL
    argv = _build_claude_argv(
        sub, job_id,
        worker_policy=worker_policy,
        base_url=base_url, token=token,
        can_dispatch=can_dispatch,
        tempfiles=tempfiles,
    )
    # argv[:3] == ["claude", "-p", task]; inject the triage system prompt after it.
    if triage_prompt:
        argv = argv[:3] + ["--append-system-prompt", triage_prompt] + argv[3:]
    return argv


def _build_codex_argv(spec: dict) -> list[str]:
    intent = spec.get("intent")
    if not intent:
        raise ValueError("codex job requires `intent`")
    if shutil.which("codex") is None:
        raise FileNotFoundError("`codex` CLI not on PATH")
    argv = ["codex", "exec", intent]
    if spec.get("args"):
        argv += list(spec["args"])
    return argv


# ---------- Worker ----------


class WorkerNotEnrolled(RuntimeError):
    """Raised when an enrolled worker's credential is no longer recognized.

    The only recovery is to re-run ``roost enroll <token>`` with a fresh
    enrollment token, so the daemon exits cleanly and lets the supervisor
    restart it (the operator can re-enroll in the meantime).
    """


class Worker:
    def __init__(
        self,
        base_url: str,
        token: str,
        worker_id: Optional[str],
        name: Optional[str] = None,
        extra_capabilities: Optional[dict] = None,
        default_cwd: Optional[str] = None,
        poll_timeout: float = POLL_TIMEOUT,
        self_test: bool = True,
        enrolled: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.name = name or socket.gethostname()
        self.capabilities = detect_capabilities(extra_capabilities, self_test=self_test)
        self.default_cwd = default_cwd
        self.poll_timeout = poll_timeout
        self.worker_id: Optional[str] = worker_id
        # Enrolled = we hold a per-worker credential (not the shared token), so
        # the server keeps our row across restarts. We must never silently
        # re-register via the legacy shared-token path.
        self.enrolled = enrolled
        self.policy: dict = {}
        self._running = 0  # in-flight jobs on this worker (V1 caps at 1)
        self._stop = asyncio.Event()
        self._heartbeat_failures = 0
        # job_id -> {"process", "is_docker", "cancelled"} for jobs executing now,
        # so a server-side cancel (delivered via the heartbeat response) can tear
        # them down — including the sibling docker container of a kind:docker job.
        self._active: dict[str, dict] = {}
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(
                connect=10.0, read=poll_timeout + 30.0, write=30.0, pool=10.0
            ),
        )

    async def close(self) -> None:
        await self.client.aclose()

    def stop(self) -> None:
        self._stop.set()

    async def _kill_active_job(self, job_id: str, reason: str) -> None:
        """Terminate a running job's process (and its docker container, if any).
        Safe to call for an unknown/finished job_id. Marks it so the executor
        reports the right terminal state."""
        entry = self._active.get(job_id)
        if not entry:
            return
        entry["cancelled"] = reason
        proc = entry.get("process")
        if proc is not None and proc.returncode is None:
            # Kill the whole process GROUP, not just the immediate child. A
            # `command` job is `/bin/sh -c "<cmd>"`; killing only the shell
            # orphans its children (e.g. `sleep`), which keep the stdout/stderr
            # pipes open so the relay never sees EOF and the worker hangs. The
            # job is spawned with start_new_session=True so it has its own group.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        if entry.get("is_docker"):
            # Killing the `docker run` client may leave the sibling container
            # running (or mid-create); stop AND remove it by its deterministic
            # name so a retry can reuse the name.
            for argv in (["docker", "kill", docker_container_name(job_id)],
                         ["docker", "rm", "-f", docker_container_name(job_id)]):
                try:
                    k = await asyncio.create_subprocess_exec(
                        *argv, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL)
                    await k.wait()
                except Exception:  # noqa: BLE001
                    pass

    async def _register_legacy(self) -> None:
        """Fallback registration via /workers/register using the shared token."""
        r = await self.client.post(
            "/workers/register",
            json={"name": self.name, "capabilities": self.capabilities},
        )
        r.raise_for_status()
        self.worker_id = r.json()["id"]
        self.policy = {}
        print(f"[roost] registered (shared token) as {self.name} ({self.worker_id})", flush=True)

    async def ensure_registered(self) -> None:
        """Confirm we have a worker_id the server recognizes; (re)register if not.

        Enrolled workers never fall back to legacy register: their credential
        identifies them and the server keeps the row, so a transient error
        keeps the existing id while a genuine 404 means the credential was
        revoked/forgotten — unrecoverable without re-enrollment.
        """
        if self.worker_id:
            try:
                r = await self.client.get(f"/workers/{self.worker_id}")
            except httpx.HTTPError:
                # Transient — assume we're still registered; keep the id.
                return
            if r.status_code == 200:
                self.policy = r.json().get("policy", {}) or {}
                return
            if r.status_code != 404:
                # Server hiccup (5xx, etc.) — keep the id and retry later.
                return
            print(f"[roost] worker {self.worker_id} unknown to server", flush=True)
            self.worker_id = None
        if self.enrolled:
            raise WorkerNotEnrolled(
                "server no longer recognizes this enrolled worker; "
                "re-run `roost enroll <token>` with a fresh enrollment token"
            )
        await self._register_legacy()
        print(f"[roost] capabilities: {json.dumps(self.capabilities, sort_keys=True)}", flush=True)

    async def heartbeat_forever(self) -> None:
        while not self._stop.is_set():
            try:
                caps_with_load = {**self.capabilities, "load": load_snapshot(self._running)}
                r = await self.client.post(
                    f"/workers/{self.worker_id}/heartbeat",
                    json={"capabilities": caps_with_load},
                )
                if r.status_code == 404:
                    # Server forgot us; let the main loop re-register/re-enroll.
                    print("[roost] heartbeat: worker unknown to server", flush=True)
                    self.worker_id = None
                    self._heartbeat_failures = 0
                elif r.status_code >= 400:
                    self._heartbeat_failures += 1
                else:
                    self._heartbeat_failures = 0
                    # The server reports which of our running jobs were cancelled;
                    # tear them down (process + docker container) promptly.
                    try:
                        for jid in (r.json().get("cancel") or []):
                            if jid in self._active:
                                print(f"[roost] job {jid} cancelled by server; killing",
                                      flush=True)
                                await self._kill_active_job(jid, "cancelled")
                    except (ValueError, KeyError, AttributeError):
                        pass
            except httpx.HTTPError as e:
                self._heartbeat_failures += 1
                print(f"[roost] heartbeat failed: {e}", flush=True)
            # Shared-token workers re-register cheaply after a run of failures
            # (handles laptop sleep/wake). Enrolled workers keep their id — the
            # server preserves their row, so we just retry until connectivity
            # returns rather than discarding a still-valid credential.
            if (
                self._heartbeat_failures >= HEARTBEAT_FAIL_THRESHOLD
                and not self.enrolled
            ):
                print(
                    f"[roost] {self._heartbeat_failures} consecutive heartbeat "
                    "failures; forcing re-register",
                    flush=True,
                )
                self.worker_id = None
                self._heartbeat_failures = 0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                pass

    def _claude_creds_path(self) -> Path:
        """Where this worker's claude reads its credentials: the isolated
        CLAUDE_CONFIG_DIR if set (shared box), else the default ~/.claude."""
        ccd = os.environ.get("CLAUDE_CONFIG_DIR")
        base = Path(ccd).expanduser() if ccd else (Path.home() / ".claude")
        return base / ".credentials.json"

    async def _refresh_claude_creds(self) -> None:
        """Pull current creds from the control plane and update the local copy if
        changed. Copied OAuth tokens rotate/expire, so a one-time copy goes stale
        (401) — this keeps the worker in sync with the operator's live creds."""
        try:
            r = await self.client.get("/claude-creds")
        except httpx.HTTPError:
            return
        if r.status_code != 200:
            return  # provisioning disabled / no creds — leave local copy alone
        creds = (r.json() or {}).get("credentials_json")
        if not creds:
            return
        path = self._claude_creds_path()
        try:
            if path.exists() and path.read_text() == creds:
                return  # unchanged
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file then atomically rename, so a concurrent
            # `claude` never reads a truncated/half-written credentials file.
            tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
            data = creds.encode("utf-8")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                while data:
                    data = data[os.write(fd, data):]
            finally:
                os.close(fd)
            os.replace(tmp, path)
            print(f"[roost] refreshed claude creds -> {path}", flush=True)
        except OSError as e:
            print(f"[roost] creds refresh write failed: {e}", flush=True)

    async def refresh_creds_forever(self) -> None:
        while not self._stop.is_set():
            await self._refresh_claude_creds()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=CREDS_REFRESH_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> Optional[dict]:
        if not self.worker_id:
            await self.ensure_registered()
        try:
            r = await self.client.get(
                f"/workers/{self.worker_id}/poll",
                params={"timeout": self.poll_timeout},
            )
        except httpx.HTTPError as e:
            print(f"[roost] poll error: {e}", flush=True)
            await asyncio.sleep(2.0)
            return None
        if r.status_code == 204:
            return None
        if r.status_code == 404:
            print("[roost] poll: worker unknown; re-registering", flush=True)
            self.worker_id = None
            return None
        if r.status_code >= 400:
            print(f"[roost] poll http {r.status_code}: {r.text[:200]}", flush=True)
            await asyncio.sleep(2.0)
            return None
        return r.json()

    async def loop(self) -> None:
        try:
            await self.ensure_registered()
        except WorkerNotEnrolled as e:
            print(f"[roost] {e}", flush=True)
            return
        hb_task = asyncio.create_task(self.heartbeat_forever())
        creds_task = asyncio.create_task(self.refresh_creds_forever())
        try:
            while not self._stop.is_set():
                if not self.worker_id:
                    try:
                        await self.ensure_registered()
                    except WorkerNotEnrolled as e:
                        print(f"[roost] {e}", flush=True)
                        break
                    except httpx.HTTPError as e:
                        print(f"[roost] re-register failed: {e}; backing off", flush=True)
                        await asyncio.sleep(5.0)
                        continue
                job = await self.poll_once()
                if not job:
                    continue
                await self.run_job(job)
        finally:
            self._stop.set()
            hb_task.cancel()
            creds_task.cancel()
            for t in (hb_task, creds_task):
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def run_job(self, job: dict) -> None:
        job_id = job["id"]
        spec = job["spec"]
        attempt = job.get("attempt") or 1
        print(
            f"[roost] received job {job_id} attempt {attempt}: "
            f"{spec.get('intent') or spec.get('command')!r}",
            flush=True,
        )
        is_auto = (spec.get("kind") or "").lower() == "auto"

        # Bare-worker pre-filter (cost mitigation, plan.md Phase 2): decline an
        # obvious capability mismatch (e.g. a GPU task on a no-GPU node) WITHOUT
        # spending an LLM triage call. Conservative — strong signals only.
        if is_auto:
            pf = _auto_prefilter(spec.get("task") or spec.get("intent") or "", self.capabilities)
            if pf:
                await self._send_log(job_id, "event", f"auto pre-filter declined (no LLM): {pf}")
                await self._post_event(job_id, {"type": "declined", "attempt": attempt, "reason": pf})
                print(f"[roost] job {job_id} pre-filter declined: {pf}", flush=True)
                return

        await self._post_event(job_id, {"type": "started", "attempt": attempt})
        self._running += 1

        can_dispatch = bool((spec.get("hierarchy") or {}).get("can_dispatch", False))

        # Bare-worker (kind: auto): fetch the triage system prompt (rendered for us
        # against the live fleet) from the control plane, with a local fallback.
        triage_prompt: Optional[str] = None
        decline_marker = triage.DECLINE_MARKER
        if is_auto:
            triage_prompt, decline_marker = await self._fetch_triage_prompt()

        try:
            argv, cwd, tempfiles = build_command(
                spec, job_id,
                default_cwd=self.default_cwd,
                worker_policy=self.policy,
                base_url=self.base_url,
                token=self.token,
                can_dispatch=can_dispatch,
                triage_prompt=triage_prompt,
            )
        except (ValueError, FileNotFoundError) as e:
            await self._send_log(job_id, "stderr", f"failed to build command: {e}")
            await self._post_event(
                job_id,
                {"type": "failed", "attempt": attempt, "error": str(e), "exit_code": None},
            )
            self._running = max(0, self._running - 1)
            return

        await self._send_log(
            job_id, "event",
            json.dumps({"argv": argv, "cwd": cwd, "attempt": attempt}),
        )

        env = os.environ.copy()
        for k, v in (spec.get("env") or {}).items():
            env[str(k)] = str(v)
        if spec.get("subagent_model"):
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = spec["subagent_model"]

        budget = spec.get("budget") or {}
        timeout_s: Optional[float] = None
        if budget.get("max_wallclock_min"):
            timeout_s = float(budget["max_wallclock_min"]) * 60.0
        elif budget.get("max_wallclock_sec"):
            timeout_s = float(budget["max_wallclock_sec"])

        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env,
                stdin=asyncio.subprocess.DEVNULL,  # headless: `claude -p` hangs on an open stdin with no TTY
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group, so cancel/timeout can kill the whole job tree
            )
        except (FileNotFoundError, PermissionError) as e:
            await self._send_log(job_id, "stderr", f"spawn failed: {e}")
            await self._post_event(
                job_id,
                {"type": "failed", "attempt": attempt, "error": str(e), "exit_code": None},
            )
            for p in tempfiles:
                p.unlink(missing_ok=True)
            self._running = max(0, self._running - 1)
            return

        is_docker = (spec.get("kind") or "").lower() == "docker"
        self._active[job_id] = {"process": process, "is_docker": is_docker, "cancelled": None}

        tokens_used = 0
        declined = False
        decline_reason: Optional[str] = None
        result_text: Optional[str] = None

        async def relay(stream: asyncio.StreamReader, kind: str) -> None:
            nonlocal tokens_used, declined, decline_reason, result_text
            assert stream is not None
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                # Bare-worker accept/decline protocol: the triage agent emits the
                # decline marker in its output to route the task to a better node.
                if is_auto and not declined and decline_marker in text:
                    declined = True
                    after = text.split(decline_marker, 1)[1].strip()
                    decline_reason = (after.split('"')[0].split("\\n")[0].strip()
                                      or "declined")[:200]
                # Parse stream-json for token usage if applicable.
                if kind == "stdout" and text.startswith("{"):
                    try:
                        obj = json.loads(text)
                        if obj.get("type") == "result" and obj.get("result") is not None:
                            result_text = str(obj.get("result"))
                        usage = (obj.get("message") or {}).get("usage") or obj.get("usage")
                        delta = 0
                        if isinstance(usage, dict):
                            delta = int(usage.get("input_tokens") or 0) + int(
                                usage.get("output_tokens") or 0
                            )
                            if delta:
                                tokens_used += delta
                        # Liveness: report token checkpoints AND structured
                        # activity so observers see "what it's doing now", not
                        # just raw JSON. Emit when either signal is present.
                        activity = activity_from_stream_json(obj)
                        if delta or activity:
                            await self._post_event(
                                job_id,
                                {"type": "progress", "attempt": attempt,
                                 "tokens_used": tokens_used, "activity": activity},
                            )
                    except (json.JSONDecodeError, TypeError):
                        pass
                await self._send_log(job_id, kind, text)
                print(f"[{job_id} {kind}] {text}", flush=True)

        stdout_task = asyncio.create_task(relay(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(relay(process.stderr, "stderr"))

        timed_out = False
        try:
            if timeout_s:
                await asyncio.wait_for(process.wait(), timeout=timeout_s)
            else:
                await process.wait()
        except asyncio.TimeoutError:
            timed_out = True
            await self._send_log(
                job_id, "event",
                f"wallclock budget exceeded ({timeout_s:.0f}s); killing",
            )
            # Reuse the shared teardown (kills process + docker container).
            await self._kill_active_job(job_id, "timeout")
            try:
                await process.wait()
            except Exception:  # noqa: BLE001
                pass
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            for p in tempfiles:
                p.unlink(missing_ok=True)

        # Was this job killed by a server-side cancel (vs. finishing on its own)?
        cancelled = (self._active.get(job_id) or {}).get("cancelled") == "cancelled"
        self._active.pop(job_id, None)

        exit_code = process.returncode
        terminal: dict[str, Any] = {"attempt": attempt, "tokens_used": tokens_used}
        if cancelled:
            # The control plane already moved the job to 'cancelled'; report a
            # terminal event it will ignore (terminal-state guard) but which
            # keeps our accounting honest. Skip posting to avoid clobbering.
            self._running = max(0, self._running - 1)
            print(f"[roost] job {job_id} torn down (cancelled)", flush=True)
            return
        if timed_out:
            terminal.update(type="failed", error="wallclock_exceeded", exit_code=exit_code)
        elif declined:
            # Self-declined: not a failure — the kernel requeues for a better-fit node.
            terminal.update(type="declined", reason=decline_reason or "declined")
        elif exit_code == 0:
            # Trust loop: for agentic jobs that opt in, an INDEPENDENT verifier checks
            # the goal was actually achieved before we call it succeeded.
            goal = spec.get("task") or spec.get("intent")
            if _wants_verify(spec, is_auto) and goal:
                await self._post_event(job_id, {"type": "progress", "attempt": attempt,
                                                "tokens_used": tokens_used,
                                                "activity": "🔎 verifying result"})
                passed, evidence, vtok = await self._run_verifier(job_id, goal, result_text)
                tokens_used += vtok
                # Self-healing (ease-of-use-plan Phase 3): if the verifier positively
                # rejected the result, fix-and-re-verify on this same node, bounded.
                heals = 0
                while passed is False and heals < MAX_FIX_ATTEMPTS:
                    heals += 1
                    await self._post_event(job_id, {"type": "progress", "attempt": attempt,
                                                    "tokens_used": tokens_used,
                                                    "activity": f"🔧 self-healing (attempt {heals})"})
                    await self._send_log(job_id, "event",
                                         f"verification failed; self-healing attempt {heals}: {evidence[:120]}")
                    fixed, _full, ftok = await self._oneshot_agent(
                        job_id, verify_mod.render_fix(goal, evidence, result_text), label=f"fix{heals}")
                    tokens_used += ftok
                    if fixed:
                        result_text = fixed
                    passed, evidence, vtok = await self._run_verifier(job_id, goal, result_text)
                    tokens_used += vtok
                terminal["tokens_used"] = tokens_used
                bundle = {"output": result_text, "verified": passed is True,
                          "evidence": evidence, "self_heal_attempts": heals}
                if passed is False:
                    terminal.update(
                        type="failed", exit_code=0, result=bundle,
                        error=f"verification failed after {heals} self-heal attempt(s): {evidence}"[:300])
                else:
                    # PASS, or inconclusive (a broken verifier shouldn't fail good work).
                    terminal.update(type="succeeded", exit_code=0, result=bundle)
            else:
                terminal.update(type="succeeded", exit_code=0,
                                result={"output": result_text} if result_text else None)
        else:
            terminal.update(type="failed", error=f"exit_code={exit_code}", exit_code=exit_code)
        await self._post_event(job_id, terminal)
        self._running = max(0, self._running - 1)
        print(
            f"[roost] job {job_id} finished exit={exit_code} timed_out={timed_out} "
            f"tokens_used={tokens_used}",
            flush=True,
        )

    async def _fetch_triage_prompt(self) -> tuple[str, str]:
        """Pull the bare-worker triage system prompt from the control plane (rendered
        for us against the live fleet). Falls back to the bundled prompt + our own
        capabilities if the control plane is unreachable, so an auto job still runs."""
        try:
            r = await self.client.get("/triage-prompt", timeout=10.0)
            r.raise_for_status()
            data = r.json()
            return data["system"], data.get("decline_marker", triage.DECLINE_MARKER)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            print(f"[roost] triage-prompt fetch failed ({e}); using local fallback", flush=True)
            return triage.render(self.capabilities), triage.DECLINE_MARKER

    async def _oneshot_agent(
        self, job_id: str, intent: str, *, system_prompt: Optional[str] = None,
        label: str = "agent", timeout_s: float = 300.0,
    ) -> tuple[Optional[str], str, int]:
        """Run a one-shot Sonnet agent (fresh context) and return (result_text,
        full_stdout, tokens). Used by the verifier and the self-healing fix loop — same
        node as the executor so it sees the artifacts. No cancel tracking (sub-step)."""
        temp: list[Path] = []
        try:
            spec = {"intent": intent, "model": AUTO_DEFAULT_MODEL,
                    "permissions": {"sandbox": True}}
            argv = _build_claude_argv(
                spec, f"{job_id}-{label}", worker_policy=self.policy,
                base_url=None, token=None, can_dispatch=False, tempfiles=temp,
            )
            if system_prompt:
                argv = argv[:3] + ["--append-system-prompt", system_prompt] + argv[3:]
        except (ValueError, FileNotFoundError) as e:
            return None, "", 0
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.default_cwd or os.getcwd(), env=os.environ.copy(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError):
            for p in temp:
                p.unlink(missing_ok=True)
            return None, "", 0

        chunks: list[str] = []
        tokens = 0
        result: Optional[str] = None

        async def rel(stream: asyncio.StreamReader) -> None:
            nonlocal tokens, result
            while True:
                line = await stream.readline()
                if not line:
                    return
                t = line.decode("utf-8", errors="replace").rstrip("\n")
                chunks.append(t)
                if t.startswith("{"):
                    try:
                        o = json.loads(t)
                        if o.get("type") == "result" and o.get("result") is not None:
                            result = str(o.get("result"))
                        u = (o.get("message") or {}).get("usage") or o.get("usage")
                        if isinstance(u, dict):
                            tokens += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
                    except (json.JSONDecodeError, TypeError):
                        pass

        t1 = asyncio.create_task(rel(proc.stdout))
        t2 = asyncio.create_task(rel(proc.stderr))
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        await asyncio.gather(t1, t2, return_exceptions=True)
        for p in temp:
            p.unlink(missing_ok=True)
        return result, "\n".join(chunks), tokens

    async def _run_verifier(
        self, job_id: str, goal: str, result_text: Optional[str]
    ) -> tuple[Optional[bool], str, int]:
        """Run an INDEPENDENT verifier (fresh context, adversarial) to check the goal was
        actually achieved. Returns (passed, evidence, tokens): True=verified,
        False=positively unmet (→ fail), None=inconclusive (→ don't block)."""
        result, full, tokens = await self._oneshot_agent(
            job_id, verify_mod.render_user(goal, result_text),
            system_prompt=verify_mod.system_prompt(), label="verify")
        passed, reason = verify_mod.parse_verdict(result or full)
        verdict = "PASS" if passed else ("FAIL" if passed is False else "INCONCLUSIVE")
        await self._send_log(job_id, "event", f"verifier verdict: {verdict} — {reason[:160]}")
        print(f"[roost] job {job_id} verifier: {verdict} — {reason[:120]}", flush=True)
        return passed, reason, tokens

    async def _send_log(self, job_id: str, stream: str, data: str) -> None:
        try:
            await self.client.post(
                f"/workers/{self.worker_id}/jobs/{job_id}/logs",
                json={"stream": stream, "data": data},
            )
        except httpx.HTTPError as e:
            print(f"[roost] log POST failed: {e}", flush=True)

    async def _post_event(self, job_id: str, event: dict) -> None:
        try:
            await self.client.post(
                f"/workers/{self.worker_id}/jobs/{job_id}/event", json=event
            )
        except httpx.HTTPError as e:
            print(f"[roost] event POST failed: {e}", flush=True)


def run(
    base_url: str,
    token: str,
    worker_id: Optional[str] = None,
    name: Optional[str] = None,
    extra_capabilities: Optional[dict] = None,
    default_cwd: Optional[str] = None,
    self_test: bool = True,
    enrolled: bool = False,
) -> None:
    worker = Worker(
        base_url=base_url,
        token=token,
        worker_id=worker_id,
        name=name,
        extra_capabilities=extra_capabilities,
        default_cwd=default_cwd,
        self_test=self_test,
        enrolled=enrolled,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _request_stop(*_a):
        print("[roost] shutdown requested", flush=True)
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, _request_stop)

    try:
        loop.run_until_complete(worker.loop())
    finally:
        loop.run_until_complete(worker.close())
        loop.close()
