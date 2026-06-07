"""Roost worker daemon (V1).

Long-running, supervisor-friendly. Each loop iteration:

  * heartbeat (15s) — renews the lease on every job this worker is currently running
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
from collections import deque
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
from . import steward
from . import triage
from . import verify as verify_mod

POLL_TIMEOUT = 25.0
HEARTBEAT_INTERVAL = 15.0
HEARTBEAT_FAIL_THRESHOLD = 3      # consecutive failures before re-registering
# [R3] A locally-running job the server no longer attributes to us is aborted on
# reconcile — but only after this grace (> the server's 60s LEASE_TTL), so a job
# leased moments after the server built its heartbeat snapshot is never reaped.
LEASE_LOST_GRACE = 90.0
CREDS_REFRESH_INTERVAL = 1200.0  # re-pull Claude creds every 20 min (they rotate)
CAPACITY_REFRESH_INTERVAL = 180.0  # re-judge worker capacity at most every few min
CAPACITY_AGENT_TIMEOUT = 30.0    # cap the steward capacity call (fail-safe to 1)
# [BUG3a] Diagnosis runs before its OWN job's FAILED terminal event is posted. With
# real concurrency (Bug-1) it no longer stalls the worker globally, but a long bound
# still delays this job's terminal event (and its lease renews via the heartbeat, not
# here), so keep it tight; fall back to the mechanical one-liner on timeout.
DIAGNOSIS_AGENT_TIMEOUT = 20.0   # cap the failure-diagnosis call (fail-safe to mechanical)
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


# Fraction of system RAM advertised as usable "VRAM" on a Jetson/Tegra board.
# Jetsons have NO dedicated VRAM — the GPU and CPU share the same physical memory
# (unified memory). Advertising the FULL RAM would let a job request all of it as
# GPU memory and starve the OS, so report a conservative fraction. Floored to a
# small value so a tiny dev board still advertises *some* GPU memory.
_TEGRA_VRAM_FRACTION = 0.5


def _detect_tegra_gpu() -> list[dict]:
    """Fallback GPU detection for NVIDIA Jetson (Tegra) boards.

    On Jetson Orin/Xavier/Nano, `nvidia-smi --query-gpu=...` returns nothing usable
    (the integrated `nvgpu` reports name "Orin (nvgpu)" with `[N/A]` memory), so the
    standard discrete-GPU probe (`_detect_gpus`) yields an empty list and the node
    advertises NO GPU — it then can't match `gpu` / `docker_gpu` / `gpu_vram_gb`
    requirements despite being a real (integrated) GPU box.

    This detects a Jetson via several signals (any one is sufficient) and, when found,
    advertises a single integrated GPU. Jetsons share system RAM (unified memory), so
    the "VRAM" figure is a conservative fraction of total RAM (see _TEGRA_VRAM_FRACTION).
    Returns [] on non-Tegra hardware so discrete-GPU detection is unaffected."""
    model = _tegra_model()
    if model is None:
        return []
    ram = _detect_ram_gb()
    # Unified memory: a conservative share of system RAM, floored so even a 4GB Nano
    # advertises ~1GB. None RAM → modest default so the node still advertises a GPU.
    if ram is not None:
        vram_gb = round(max(1.0, ram * _TEGRA_VRAM_FRACTION), 1)
    else:
        vram_gb = 2.0
    return [{
        "name": model,
        "vram_gb": vram_gb,
        "driver": None,
        "tegra": True,          # marker: integrated/unified-memory GPU, not discrete
        "integrated": True,
    }]


def _tegra_model() -> Optional[str]:
    """Return a human label for a Jetson/Tegra board if this is one, else None.

    Signals (any one suffices):
      * /etc/nv_tegra_release exists (the Tegra L4T release stamp)
      * /proc/device-tree/model contains "Jetson"/"Orin"/"Xavier"/"Tegra"
      * nvidia-smi output mentions an integrated `nvgpu` / "Orin"
    Pure best-effort; never raises."""
    # 1) L4T release stamp — present on every flashed Jetson.
    if os.path.exists("/etc/nv_tegra_release"):
        dt = _device_tree_model()
        return dt or "Jetson (Tegra)"
    # 2) Device-tree model node (e.g. "NVIDIA Jetson AGX Orin Developer Kit").
    dt = _device_tree_model()
    if dt and any(k in dt for k in ("Jetson", "Orin", "Xavier", "Tegra")):
        return dt
    # 3) nvidia-smi present but reporting an integrated nvgpu / Orin.
    smi = _find_nvidia_smi()
    if smi:
        try:
            out = subprocess.run(
                [smi, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5.0,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            out = ""
        low = out.lower()
        if "nvgpu" in low or "orin" in low or "tegra" in low:
            return dt or out.strip().splitlines()[0].strip() or "Jetson (Tegra)"
    return None


def _device_tree_model() -> Optional[str]:
    """The board model string from /proc/device-tree/model (NUL-terminated), or None.
    On Jetson this reads e.g. "NVIDIA Jetson AGX Orin Developer Kit"."""
    try:
        with open("/proc/device-tree/model", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    s = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
    return s or None


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


def load_snapshot(running: int, capacity: int = 1) -> dict[str, Any]:
    """Live worker load, refreshed on each heartbeat (V2-3).

    Cheap to compute; carried in heartbeat capabilities so the captain and the
    ranking matcher see the same picture. ``capacity`` is the steward-judged max
    concurrency (cached, recomputed periodically — see Worker._capacity); it sits
    next to ``running`` so the server can gate placement on free slots.
    """
    snap: dict[str, Any] = {"running": running, "capacity": max(1, int(capacity))}
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
    # Jetson/Tegra fallback: the discrete-GPU probe returns nothing usable on an
    # integrated nvgpu, so a real Jetson would advertise NO GPU. Only fall back when
    # the standard probe found nothing, so discrete-GPU detection stays unchanged.
    tegra = False
    if not gpus:
        tegra_gpus = _detect_tegra_gpu()
        if tegra_gpus:
            gpus = tegra_gpus
            tegra = True
    if gpus:
        caps["gpu"] = [g["name"] for g in gpus]
        caps["gpu_count"] = len(gpus)
        caps["gpu_vram_gb"] = max(g["vram_gb"] for g in gpus)
        caps["gpus"] = gpus
        if tegra:
            # Marker so the matcher / triage know this is unified-memory (shared with
            # the CPU), not dedicated VRAM — and that GPU work runs on a Jetson.
            caps["tegra"] = True
    # Docker-as-executor capability: can this worker actually RUN containers
    # (daemon reachable), and can those containers see GPUs (nvidia runtime)?
    caps.update(_detect_docker())
    # OS-level sandbox availability: bubblewrap lets us sandbox agent jobs on a
    # NON-trusted worker even when `claude` lacks a `--sandbox` flag (see
    # build_bwrap_argv / worker policy `sandbox: "bwrap"`).
    if shutil.which("bwrap") is not None:
        caps["bwrap"] = True
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
            cwd=cwd,
        )
        return argv, cwd, tempfiles

    # Docker-as-executor: run the job in a fresh, isolated container (GPU/limits
    # per job). Checked before `command` because a docker job carries BOTH an
    # `image` and the in-container `command`.
    if (spec.get("kind") or "").lower() == "docker":
        return _build_docker_argv(spec, job_id, worker_policy or {}), cwd, tempfiles

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
            cwd=cwd,
        )
        return argv, cwd, tempfiles
    if kind == "codex":
        return _build_codex_argv(spec), cwd, tempfiles
    raise ValueError(f"unknown kind: {kind!r} (and no `command` given)")


def docker_container_name(job_id: str) -> str:
    """Deterministic name for a job's container, so we can `docker kill` it."""
    return f"roost-job-{job_id}"


# [M4] Env vars a job must NOT be able to set, because they could redirect the
# operator's Claude OAuth token to an attacker endpoint, inject code into the
# subprocess, or proxy/exfiltrate traffic. The worker inherits the operator's real
# environment (incl. live creds); a job-supplied env is layered on top, so any of
# these from the job is dropped unless worker policy explicitly allows it.
_BLOCKED_ENV: frozenset[str] = frozenset({
    "NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
})
_BLOCKED_ENV_PREFIXES: tuple[str, ...] = ("ANTHROPIC_", "CLAUDE_CODE_")


def _is_blocked_env_key(key: str) -> bool:
    """Whether a job-supplied env var name is dangerous to honor (credential/endpoint
    redirect, proxy, or code-injection vector). Case-insensitive on the proxy class."""
    if key in _BLOCKED_ENV:
        return True
    if key.startswith(_BLOCKED_ENV_PREFIXES):
        return True
    up = key.upper()
    if up.endswith("_PROXY") or up in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        return True
    return False


def _sanitize_env(env: Optional[dict], policy: Optional[dict]) -> tuple[dict[str, str], list[str]]:
    """Filter a job-supplied env dict, dropping keys that could exfiltrate the
    operator's Claude OAuth token or inject code (see _BLOCKED_ENV*). Returns the
    cleaned {str: str} mapping plus the list of dropped key names. Worker policy may
    opt in to the raw env via `policy.allow_unsafe_env: true` (trusted use only)."""
    out: dict[str, str] = {}
    dropped: list[str] = []
    allow_unsafe = bool((policy or {}).get("allow_unsafe_env"))
    for k, v in (env or {}).items():
        key = str(k)
        if not allow_unsafe and _is_blocked_env_key(key):
            dropped.append(key)
            continue
        out[key] = str(v)
    return out, dropped


def _validate_container(container: Optional[dict], policy: Optional[dict]) -> None:
    """[H3] Reject docker container specs that would breach the host: mounting a
    sensitive HOST dir (operator home, creds, ssh, /, /etc, roost DB) or using
    `network: host`. Raises ValueError with a clear message. Worker policy may opt in
    via `policy.allow_host_mounts: true` (trusted use only)."""
    c = container or {}
    if bool((policy or {}).get("allow_host_mounts")):
        return
    home = Path.home()
    # Roots whose entire subtree is off-limits (mounting them OR anything inside them
    # exposes the operator's creds/keys/state). Resolved for prefix comparison.
    sensitive_subtrees = [
        Path("/etc"), Path("/root"),
        home, home / ".claude", home / ".config" / "roost", home / ".ssh",
        home / "roost-fleet",
    ]
    # On shared boxes (Mac/WSL/Oracle) the live OAuth creds live under an isolated
    # CLAUDE_CONFIG_DIR, not ~/.claude — block that too (H-2).
    ccd = os.environ.get("CLAUDE_CONFIG_DIR")
    if ccd:
        sensitive_subtrees.append(Path(ccd))
    sensitive_subtrees = [p.resolve(strict=False) for p in sensitive_subtrees]
    # `/` is rejected only as an exact mount (mounting the whole filesystem); a path
    # *under* `/` (e.g. /data) is fine — otherwise every absolute mount would fail.
    root_fs = Path("/").resolve(strict=False)

    for vol in c.get("volumes") or []:
        spec = str(vol)
        if ".." in spec.split(":"):
            raise ValueError(f"refusing volume with '..' path traversal: {spec!r}")
        host = spec.split(":", 1)[0].strip()
        if not host:
            continue
        if ".." in Path(host).parts:
            raise ValueError(f"refusing volume with '..' path traversal: {spec!r}")
        # Only host-path mounts (absolute or ~) are sensitive; named volumes are fine.
        if not (host.startswith("/") or host.startswith("~")):
            continue
        hp = Path(host).expanduser().resolve(strict=False)
        if hp == root_fs:
            raise ValueError(
                "refusing to mount the entire host filesystem '/' into container "
                "(set policy.allow_host_mounts to override)"
            )
        for root in sensitive_subtrees:
            if hp == root or root in hp.parents:
                raise ValueError(
                    f"refusing to mount sensitive host path {host!r} into container "
                    "(set policy.allow_host_mounts to override)"
                )
    if str(c.get("network") or "").lower() == "host":
        raise ValueError(
            "refusing `network: host` for container (set policy.allow_host_mounts to override)"
        )


def _argv_value(what: str, value: Any) -> str:
    """[R1] Validate a spec-sourced value bound for the `docker run` argv: reject
    empty/whitespace-only values and values whose first non-space char is '-' —
    docker's CLI parser would read those as flags, not arguments (e.g.
    `image: "--privileged"` lands after the option flags and silently grants the
    container host privileges). Returns the value as a str."""
    s = str(value)
    if not s.strip():
        raise ValueError(f"docker job: `{what}` must not be empty")
    if s.lstrip().startswith("-"):
        raise ValueError(
            f"docker job: refusing {what}={s!r} — a leading '-' would be parsed "
            "by docker as a flag, not a value")
    return s


def _build_docker_argv(spec: dict, job_id: str, policy: Optional[dict] = None) -> list[str]:
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
    # [H3] Reject sensitive host mounts / host networking unless policy opts in.
    _validate_container(c, policy)
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
        argv += ["--gpus", _argv_value("container.gpus", gpus)]
    if c.get("cpus"):
        argv += ["--cpus", _argv_value("container.cpus", c["cpus"])]
    if c.get("memory"):
        argv += ["--memory", _argv_value("container.memory", c["memory"])]
    if c.get("shm_size"):
        argv += ["--shm-size", _argv_value("container.shm_size", c["shm_size"])]
    if c.get("network"):
        argv += ["--network", _argv_value("container.network", c["network"])]
    if c.get("workdir"):
        argv += ["-w", _argv_value("container.workdir", c["workdir"])]
    for vol in c.get("volumes") or []:
        argv += ["-v", _argv_value("container.volumes entry", vol)]
    # [M4-parity] The in-container env is attacker-controllable too — a docker job
    # could set ANTHROPIC_*/*_PROXY/NODE_OPTIONS inside the container to redirect creds
    # or inject code. Apply the SAME blocked-key policy as the subprocess env, honoring
    # the `allow_unsafe_env` opt-in for parity.
    ctr_env, dropped_ctr_env = _sanitize_env(c.get("env"), policy)
    if dropped_ctr_env:
        print(f"[roost] dropped unsafe container env keys for {job_id}: "
              f"{', '.join(sorted(dropped_ctr_env))}", flush=True)
    for key, val in ctr_env.items():
        argv += ["-e", f"{key}={val}"]
    # [R1] `image` is the first positional after the option flags — the highest-value
    # injection point (a leading-dash image becomes a docker flag and the first
    # command element silently becomes the image). In-container `command` elements
    # are NOT restricted: they land after the image, where docker stops flag
    # parsing, and leading dashes there are legitimate (e.g. ["ls", "-la"]).
    argv.append(_argv_value("image", image))
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


def _claude_config_dir() -> Path:
    """The directory `claude` reads its config + OAuth creds from: the isolated
    CLAUDE_CONFIG_DIR if set (shared box), else ~/.claude. The sandbox must keep this
    writable so claude can read creds AND refresh its rotating OAuth token."""
    ccd = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(ccd).expanduser() if ccd else (Path.home() / ".claude")


def build_bwrap_argv(claude_argv: list[str], cwd: str) -> list[str]:
    """Wrap a `claude` invocation in a conservative bubblewrap (`bwrap`) profile so an
    agent job can be OS-sandboxed on a NON-trusted worker even though `claude` 2.1.x
    has no `--sandbox` flag. Returns `[bwrap, <opts...>, --, claude, ...]`.

    SECURITY PROFILE (v1 — conservative; expect field validation):
      * `--ro-bind / /` — the entire host filesystem is mounted READ-ONLY by default,
        so the job can read system libs/binaries it needs but cannot modify anything
        outside the explicit read-write holes below.
      * read-WRITE holes (bound AFTER the ro-bind, so they win):
          - the job's cwd            — where the job does its work
          - /tmp and /var/tmp        — scratch space (claude + toolchains write here)
          - the claude config/creds dir (CLAUDE_CONFIG_DIR or ~/.claude) — claude must
            read creds AND rewrite its rotating OAuth token, so it needs RW here.
      * `--dev /dev`, `--proc /proc` — minimal pseudo-filesystems claude/node need.
      * NETWORK IS KEPT (no `--unshare-net`): claude must reach the Anthropic API.
        We DO unshare PID/UTS/IPC so the job can't see or signal host processes.
      * `--die-with-parent` — the sandbox dies if the worker tears the job down.
      * `--new-session` — detach the controlling TTY (defense against TIOCSTI tricks).

    ASSUMPTIONS / LIMITS (documented honestly for a v1):
      * This is a FILESYSTEM + namespace sandbox, NOT a network egress filter — a
        compromised job can still talk to the network (it has to, for the API). Pair
        with network policy if you need egress control.
      * The claude config dir is RW, so a malicious job in the sandbox could in
        principle read/rewrite the OAuth token there. That's the same trust surface as
        running claude at all; bwrap here is about protecting the REST of the host
        (cwd-scoping writes), not about hiding creds from claude itself.
      * Binds are skipped silently if a path doesn't exist (e.g. no /var/tmp), so the
        profile is portable across distros."""
    cfg_dir = _claude_config_dir()
    argv: list[str] = ["bwrap"]
    # Whole host read-only first; RW holes layered on top so they take precedence.
    argv += ["--ro-bind", "/", "/"]
    argv += ["--dev", "/dev", "--proc", "/proc"]
    # Read-write holes. Only bind paths that exist so the profile is portable.
    rw_paths: list[str] = []
    cwd_p = str(Path(cwd).resolve(strict=False)) if cwd else os.getcwd()
    for p in (cwd_p, "/tmp", "/var/tmp", str(cfg_dir.resolve(strict=False))):
        if p and p not in rw_paths and os.path.exists(p):
            rw_paths.append(p)
    for p in rw_paths:
        argv += ["--bind", p, p]
    # Namespace isolation: hide host processes/IPC/hostname, but DO NOT unshare net
    # (claude needs the Anthropic API). Tie lifetime to the worker; drop the TTY.
    argv += [
        "--unshare-pid", "--unshare-uts", "--unshare-ipc",
        "--die-with-parent", "--new-session",
    ]
    argv += ["--"]
    argv += claude_argv
    return argv


def _bwrap_policy_enabled(worker_policy: dict) -> bool:
    """Whether the worker policy opts IN to the OS-level bwrap sandbox fallback.
    OFF by default — accepts `sandbox: "bwrap"` (preferred) or a boolean `bwrap: true`."""
    sb = worker_policy.get("sandbox")
    if isinstance(sb, str) and sb.strip().lower() == "bwrap":
        return True
    return bool(worker_policy.get("bwrap"))


def _build_claude_argv(
    spec: dict,
    job_id: str,
    *,
    worker_policy: dict,
    base_url: Optional[str],
    token: Optional[str],
    can_dispatch: bool,
    tempfiles: list[Path],
    cwd: Optional[str] = None,
) -> list[str]:
    intent = spec.get("intent")
    if not intent:
        raise ValueError("claude job requires `intent`")
    if shutil.which("claude") is None:
        raise FileNotFoundError("`claude` CLI not on PATH")
    argv = ["claude", "-p", intent, "--output-format", "stream-json", "--verbose"]

    perms = spec.get("permissions") or {}
    trust_skip = worker_policy.get("trust_skip_perms", False)

    # When we OS-sandbox via bwrap below, claude runs inside the sandbox and the host
    # is already protected, so we let claude act freely (skip-permissions) INSIDE the
    # jail rather than crash on the unknown --sandbox flag or be neutered to read-only.
    bwrap_wrap = False

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
        elif _bwrap_policy_enabled(worker_policy) and shutil.which("bwrap") is not None:
            # OPT-IN OS-level sandbox: claude lacks --sandbox, but the worker policy
            # enables bwrap and bubblewrap is installed. Run claude with skip-permissions
            # INSIDE a conservative bwrap jail (see build_bwrap_argv) so a NON-trusted
            # worker can still run agent jobs sandboxed — instead of either crashing or
            # silently falling back to an UNsandboxed skip-permissions on the bare host.
            argv.append("--dangerously-skip-permissions")
            bwrap_wrap = True
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

    # If we chose the OS-level sandbox above, wrap the whole claude invocation in a
    # conservative bwrap jail now (after all claude flags are assembled). The MCP
    # config tempfile lives under /tmp (RW in the profile), so roost-mcp still works.
    if bwrap_wrap:
        argv = build_bwrap_argv(argv, cwd or os.getcwd())
    return argv


# Default model for kind:auto triage+execution: Sonnet — capable enough to
# reliably introspect (run nvidia-smi etc.) and execute, while far cheaper than the
# Opus that drove the original "triage tax" (plan.md Phase 2/3). Override with
# `model:` in the spec (e.g. a smaller model for bulk/trivial work).
AUTO_DEFAULT_MODEL = "claude-sonnet-4-6"

# Self-healing (trust loop): if the independent verifier rejects a result, fix-and-
# re-verify on the same node up to this many times before reporting failure.
MAX_FIX_ATTEMPTS = 2

# [C4/H2] Default per-subprocess ceiling for verify/self-heal agents.
VERIFY_HEAL_TIMEOUT = 300.0

# [R2] Default wallclock caps (minutes) per job kind, applied only when the spec
# sets no budget — an unbudgeted job must not hold a capacity slot forever.
# Override per worker via policy `default_wallclock_min` (scalar for all kinds,
# or a {kind: minutes} mapping); 0 or a negative value opts that kind out
# (unbounded — an explicit, trusted choice). Generous on purpose: these are
# runaway breakers, not schedulers.
DEFAULT_WALLCLOCK_MIN: dict[str, float] = {
    "command": 120.0,   # shell one-liners / scripts
    "claude": 240.0,    # agent jobs: long but not infinite
    "auto": 240.0,      # bare-worker triage+execute (an agent job underneath)
    "docker": 360.0,    # containerized builds / GPU work run longest
}
DEFAULT_WALLCLOCK_FALLBACK_MIN = 240.0  # unknown kinds


def _resolve_timeout(spec: dict, policy: Optional[dict]) -> tuple[Optional[float], str]:
    """[R2] Resolve a job's wallclock cap. Returns (timeout_s, source) where
    source is "budget" (explicit spec budget), "default" (per-kind cap above /
    policy override), or "none" (policy opted the kind out). Pure + total; safe
    on missing/garbage values (garbage budget falls through to the default cap,
    garbage policy falls back to the built-in default)."""
    budget = spec.get("budget") or {}
    try:
        if budget.get("max_wallclock_min"):
            return float(budget["max_wallclock_min"]) * 60.0, "budget"
        if budget.get("max_wallclock_sec"):
            return float(budget["max_wallclock_sec"]), "budget"
    except (TypeError, ValueError):
        pass  # garbage budget → treat as unbudgeted, fall through to default

    kind = (spec.get("kind") or "claude").lower()
    default_min = DEFAULT_WALLCLOCK_MIN.get(kind, DEFAULT_WALLCLOCK_FALLBACK_MIN)
    cfg = (policy or {}).get("default_wallclock_min")
    if isinstance(cfg, dict):
        cfg = cfg.get(kind, default_min)
    if cfg is None:
        cfg = default_min
    try:
        minutes = float(cfg)
    except (TypeError, ValueError):
        minutes = default_min
    if minutes <= 0:
        return None, "none"  # explicit opt-out: unbounded
    return minutes * 60.0, "default"


def _budget_remaining(
    budget: dict,
    elapsed_s: float,
    tokens_used: int,
) -> tuple[Optional[float], bool]:
    """[C4/H2] Bound the verify/self-heal loop's cost. Given the job's `budget`
    (max_wallclock_min/max_wallclock_sec, max_tokens) plus what the executor+verify+heal
    have already spent, return (remaining_wallclock_s, exhausted):

      remaining_wallclock_s — how long the NEXT verify/heal subprocess may run, capped at
        VERIFY_HEAL_TIMEOUT; None means "no wallclock budget set" (use the default cap).
      exhausted — True if there's no budget left to start another subprocess (token cap
        hit, or wallclock fully consumed), so the caller should accept the current result.

    Pure + total; safe on missing/garbage budget values."""
    max_tokens = budget.get("max_tokens")
    try:
        if max_tokens is not None and int(tokens_used) >= int(max_tokens):
            return 0.0, True
    except (TypeError, ValueError):
        pass

    total_wallclock: Optional[float] = None
    try:
        if budget.get("max_wallclock_min"):
            total_wallclock = float(budget["max_wallclock_min"]) * 60.0
        elif budget.get("max_wallclock_sec"):
            total_wallclock = float(budget["max_wallclock_sec"])
    except (TypeError, ValueError):
        total_wallclock = None

    if total_wallclock is None:
        # No wallclock budget: each subprocess still capped at the default ceiling.
        return VERIFY_HEAL_TIMEOUT, False

    remaining = total_wallclock - max(0.0, elapsed_s)
    if remaining <= 1.0:  # not enough headroom to do anything useful
        return 0.0, True
    return min(VERIFY_HEAL_TIMEOUT, remaining), False

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
    cwd: Optional[str] = None,
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
        cwd=cwd,
    )
    # Inject the triage system prompt right after `claude -p <task>`. Locate the
    # `claude` token rather than assuming index 0 — the argv may be wrapped in a
    # bwrap jail (`bwrap ... -- claude -p <task> ...`), in which case `claude` is not
    # at the front.
    if triage_prompt:
        try:
            ci = argv.index("claude")
        except ValueError:
            ci = 0
        cut = ci + 3  # ["claude", "-p", task]
        argv = argv[:cut] + ["--append-system-prompt", triage_prompt] + argv[cut:]
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
        self._running = 0  # in-flight jobs on this worker
        # Steward-judged max concurrency (load.capacity). Cached and recomputed
        # periodically (first heartbeat, then every CAPACITY_REFRESH_INTERVAL or when
        # the running-count changes) — NOT on every 15s heartbeat. Fail-safe = 1.
        self._capacity = steward.FALLBACK_CAPACITY
        self._capacity_at = 0.0          # monotonic time of last judgment
        self._capacity_running = -1      # running-count the cached value was judged at
        self._capacity_lock = asyncio.Lock()
        self._capacity_task: Optional[asyncio.Task] = None  # in-flight bg judgment
        self._stop = asyncio.Event()
        self._heartbeat_failures = 0
        # job_id -> {"process", "is_docker", "cancelled"} for jobs executing now,
        # so a server-side cancel (delivered via the heartbeat response) can tear
        # them down — including the sibling docker container of a kind:docker job.
        self._active: dict[str, dict] = {}
        # [BUG1] In-flight run_job tasks keyed by job_id, so the worker runs up to
        # self._capacity jobs CONCURRENTLY (not one inline at a time). The loop spawns
        # into here and waits on _slot_free (set whenever any job task finishes) for a
        # free slot, rather than awaiting a single job to completion or busy-spinning.
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._slot_free = asyncio.Event()
        # [H5] Verify/self-heal subprocesses spawned by _oneshot_agent, keyed by
        # job_id, so a server cancel (or timeout teardown) of the parent job also
        # kills an in-flight verifier/fix agent instead of leaking tokens.
        self._aux_procs: dict[str, set] = {}
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
        # [H5] Always tear down any aux verify/fix subprocesses for this job, even
        # if the main executor process has already finished (the job may be mid
        # verify/self-heal, where _active has no live process but tokens are burning).
        self._kill_aux_procs(job_id, reason)
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

    def _kill_aux_procs(self, job_id: str, reason: str) -> None:
        """[H5] SIGKILL any in-flight verify/self-heal subprocesses for this job
        (and their process groups). Marks the set so _oneshot_agent knows it was
        cancelled. Synchronous + best-effort; the spawning coroutine reaps them."""
        procs = self._aux_procs.get(job_id)
        if not procs:
            return
        for proc in list(procs):
            if getattr(proc, "returncode", None) is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.kill()
                    except (ProcessLookupError, OSError):
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
                # [BUG2] Capacity judgment can await a ~claude -p subprocess; it must
                # NEVER delay the heartbeat POST (which renews leases + delivers cancels
                # — a stall risks lease expiry → requeue → double-execution). So we only
                # TRIGGER the judgment as a detached background task here and post the
                # heartbeat immediately using the cached value (fail-safe 1 until the
                # first judgment lands).
                self._maybe_spawn_capacity_refresh()
                caps_with_load = {
                    **self.capabilities,
                    "load": load_snapshot(self._running, self._capacity),
                }
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
                        body = r.json()
                        for jid in (body.get("cancel") or []):
                            if jid in self._active:
                                print(f"[roost] job {jid} cancelled by server; killing",
                                      flush=True)
                                await self._kill_active_job(jid, "cancelled")
                        # [R3] Lease reconciliation: abort local attempts the
                        # server no longer attributes to us (requeued during a
                        # CP outage past the lease TTL). Absent on older servers.
                        owned = body.get("owned")
                        if owned is not None:
                            await self._reconcile_owned(set(owned))
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

    # ---------- steward judgments (capacity + diagnosis) ----------

    async def _run_steward_agent(
        self, prompt: str, *, label: str, timeout_s: float,
    ) -> Optional[str]:
        """Run a one-shot haiku steward agent (fresh context) and return its result
        text, or None if claude is absent / the call fails / times out / yields no
        result. Callers MUST fall back deterministically on None — the steward never
        blocks the worker. Uses --output-format json (single JSON object on stdout)."""
        if shutil.which("claude") is None:
            return None
        argv = [
            "claude", "-p", prompt,
            "--model", steward.STEWARD_MODEL,
            "--output-format", "json",
        ]
        # [BUG3b] The steward call ingests attacker-controlled job stdout/stderr tails
        # (diagnosis) and runs in the worker's cwd. Sanitize the inherited environment
        # the same way job subprocesses are sanitized, so a job can't pre-seed
        # ANTHROPIC_*/*_PROXY/NODE_OPTIONS/LD_* into the steward's process and redirect
        # its creds or inject code. We filter os.environ itself (not a job env layer).
        env, _dropped = _sanitize_env(dict(os.environ), self.policy)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.default_cwd or os.getcwd(), env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError):
            return None
        try:
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return None
        except Exception:  # noqa: BLE001 — steward must never raise into the worker
            return None
        if proc.returncode != 0:
            return None
        text = (out or b"").decode("utf-8", errors="replace").strip()
        if not text:
            return None
        # `--output-format json` wraps the agent reply: {"type":"result","result": "..."}.
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("result") is not None:
                return str(obj["result"])
        except (json.JSONDecodeError, TypeError):
            pass
        return text  # not the wrapper shape — let the caller parse the raw text

    async def _judge_capacity(self) -> int:
        """Ask the steward agent for this box's max concurrency, cache it, and return
        it. Deterministic fail-safe = 1 (steward.FALLBACK_CAPACITY) when claude is
        absent or the call fails/parses wrong — logged. Pure-mechanical fallback never
        blocks the worker."""
        facts = steward.machine_facts(
            self.capabilities, self._running, find_nvidia_smi=_find_nvidia_smi,
        )
        raw = await self._run_steward_agent(
            steward.capacity_prompt(facts), label="capacity",
            timeout_s=CAPACITY_AGENT_TIMEOUT,
        )
        cap = steward.parse_capacity(raw)
        if cap is None:
            # Claude steward couldn't run / parse: don't collapse a big idle box to 1.
            # Degrade gracefully to a MECHANICAL estimate from the machine facts we
            # already gathered (cores + available memory), bounded, min 1.
            cap = steward.mechanical_capacity(facts)
            print("[roost] capacity steward unavailable/unparseable; "
                  f"mechanical estimate capacity={cap}", flush=True)
        else:
            print(f"[roost] capacity steward → max_concurrent={cap} "
                  f"(running={self._running})", flush=True)
        self._capacity = cap
        self._capacity_at = time.monotonic()
        self._capacity_running = self._running
        # [BUG1] If capacity GREW while the main loop is parked waiting for a slot, wake
        # it so it re-evaluates and can pick up more concurrent work without waiting for
        # a job to finish first. (Shrinking is enforced lazily at the next poll check.)
        self._slot_free.set()
        return cap

    def _capacity_is_stale(self) -> bool:
        """Whether the cached capacity needs recomputing: first time, the running-count
        changed since it was judged, or CAPACITY_REFRESH_INTERVAL elapsed."""
        now = time.monotonic()
        return (
            self._capacity_at == 0.0
            or self._running != self._capacity_running
            or (now - self._capacity_at) >= CAPACITY_REFRESH_INTERVAL
        )

    def _maybe_spawn_capacity_refresh(self) -> None:
        """[BUG2] Non-blocking trigger: if the cached capacity is stale and no judgment
        is already in flight, spawn one as a DETACHED background task that only updates
        the cached value. Returns immediately so the caller (the heartbeat) never waits
        on a ~claude subprocess. Single-flight via the in-flight task handle."""
        if self._stop.is_set():
            return
        if self._capacity_task is not None and not self._capacity_task.done():
            return
        if not self._capacity_is_stale():
            return
        self._capacity_task = asyncio.create_task(self._refresh_capacity_once())

    async def _refresh_capacity_once(self) -> None:
        """Recompute capacity in the background, updating only the cached value. Holds a
        lock so overlapping triggers can't spawn duplicate steward agents, and never
        raises into its caller (it runs detached)."""
        if self._capacity_lock.locked():
            return
        async with self._capacity_lock:
            if not self._capacity_is_stale():
                return
            try:
                await self._judge_capacity()
            except Exception as e:  # noqa: BLE001 — never let the steward break the worker
                self._capacity = steward.FALLBACK_CAPACITY
                self._capacity_at = time.monotonic()
                self._capacity_running = self._running
                print(f"[roost] capacity judgment error: {e}; fail-safe capacity=1",
                      flush=True)

    async def _diagnose_failure(
        self, spec: dict, *, exit_code: Optional[Any],
        stdout_tail: Optional[str], stderr_tail: Optional[str],
        error: Optional[str] = None,
    ) -> str:
        """Author a short root-cause line for a FAILED terminal event. Agentic (haiku)
        with a deterministic mechanical fallback (exit_code + last stderr line) when
        claude is absent or the call fails. Always returns a non-empty string."""
        fallback = steward.deterministic_diagnosis(
            exit_code=exit_code, stderr_tail=stderr_tail,
            stdout_tail=stdout_tail, error=error,
        )
        try:
            raw = await self._run_steward_agent(
                steward.diagnosis_prompt(
                    spec_summary=steward.spec_summary(spec), exit_code=exit_code,
                    stdout_tail=stdout_tail, stderr_tail=stderr_tail,
                ),
                label="diagnosis", timeout_s=DIAGNOSIS_AGENT_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 — diagnosis must never break reporting
            raw = None
        return steward.clean_diagnosis(raw) or fallback

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
                # [BUG1] Bounded concurrency: re-read the (dynamic, steward-judged)
                # capacity each iteration and only poll for new work while we have a free
                # slot. At capacity, WAIT for a job to finish (slot to free) instead of
                # polling — so we never lease more than we can run, and never busy-spin.
                if len(self._job_tasks) >= max(1, self._capacity):
                    await self._wait_for_free_slot()
                    continue
                job = await self.poll_once()
                if not job:
                    continue
                # [R3] If this is a re-lease of a job we're still running, tear
                # down the stale attempt before starting the new one.
                await self._reap_stale_attempt(job["id"])
                self._spawn_job(job)
        finally:
            self._stop.set()
            hb_task.cancel()
            creds_task.cancel()
            # [BUG1] Shutdown: tear down EVERY in-flight job (process group + docker
            # container + aux verify/heal procs) and cancel its task, not just one.
            await self._shutdown_jobs()
            for t in (hb_task, creds_task):
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def _reconcile_owned(self, owned: set[str]) -> None:
        """[R3] Abort any locally-running job the server no longer attributes to
        us. Chosen semantics: ABORT local work on reconcile (vs. report-and-dedupe)
        — the server has already requeued the job, so finishing the orphaned
        attempt can only duplicate side effects and burn tokens; its terminal
        report would be rejected as stale anyway. Only entries older than
        LEASE_LOST_GRACE are eligible, so a job leased after the server built its
        heartbeat snapshot is never reaped."""
        now = time.monotonic()
        for jid, entry in list(self._active.items()):
            if jid in owned or entry.get("cancelled"):
                continue
            if now - float(entry.get("since") or now) < LEASE_LOST_GRACE:
                continue
            print(f"[roost] job {jid} lease lost (server no longer attributes "
                  "it to us); aborting local attempt", flush=True)
            await self._kill_active_job(jid, "lease_lost")

    async def _reap_stale_attempt(self, job_id: str) -> None:
        """[R3] The same job re-leased to us while a previous attempt still runs
        locally (CP outage → sweep → requeue → we won the new lease): the old
        attempt lost its lease by definition. Kill it and wait for its task to
        fully unwind (its done-callback pops _job_tasks/_active) BEFORE starting
        the new attempt — both maps are keyed by job_id, so starting early would
        cross the attempts' tracking entries."""
        old = self._job_tasks.get(job_id)
        if old is None or old.done():
            return
        print(f"[roost] job {job_id} re-leased while a stale local attempt is "
              "still running; aborting the stale attempt", flush=True)
        await self._kill_active_job(job_id, "lease_lost")
        try:
            await old
        except Exception:  # noqa: BLE001 — old attempt's failure is not ours
            pass

    def _spawn_job(self, job: dict) -> None:
        """[BUG1] Launch run_job as a tracked background task so the loop can keep
        polling for more concurrent work. A done-callback frees the slot and reaps the
        task entry on EVERY completion path (success/verify/heal/cancel/timeout/error)."""
        job_id = job["id"]
        task = asyncio.create_task(self.run_job(job))
        self._job_tasks[job_id] = task

        def _done(t: asyncio.Task, _jid: str = job_id) -> None:
            self._job_tasks.pop(_jid, None)
            # Surface an unexpected crash (run_job guards its own paths, but never let a
            # task die silently and leak _running/_active accounting unnoticed).
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    print(f"[roost] job {_jid} task crashed: {exc!r}", flush=True)
            self._slot_free.set()

        task.add_done_callback(_done)

    async def _wait_for_free_slot(self) -> None:
        """Block until a job task completes (slot frees) or the worker is told to stop.
        The done-callback sets _slot_free; we clear it before returning so the next full
        slot blocks again."""
        self._slot_free.clear()
        # Re-check: a slot may have freed (or capacity grown) between the loop's check
        # and clearing the event, in which case return immediately.
        if len(self._job_tasks) < max(1, self._capacity):
            return
        stop_wait = asyncio.create_task(self._stop.wait())
        slot_wait = asyncio.create_task(self._slot_free.wait())
        try:
            await asyncio.wait(
                {stop_wait, slot_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (stop_wait, slot_wait):
                t.cancel()

    async def _shutdown_jobs(self) -> None:
        """[BUG1] Cancel ALL in-flight job tasks and tear down their process groups /
        docker containers / aux procs. Used on worker shutdown."""
        tasks = list(self._job_tasks.values())
        for job_id in list(self._active.keys()):
            try:
                await self._kill_active_job(job_id, "shutdown")
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Done-callbacks that pop _job_tasks run on a later loop turn; clear here so
        # the worker's view of in-flight work is empty immediately after shutdown.
        self._job_tasks.clear()

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
        # [C4/H2] Wallclock origin for bounding the verify/self-heal loop's total cost.
        job_started = time.monotonic()

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
        # [M4] Layer the job-supplied env on top of the operator's real environment,
        # but strip keys that could redirect/exfiltrate the Claude OAuth token or
        # inject code (ANTHROPIC_*/CLAUDE_CODE_*/*_PROXY/NODE_OPTIONS/LD_*).
        job_env, dropped_env = _sanitize_env(spec.get("env"), self.policy)
        env.update(job_env)
        if dropped_env:
            await self._send_log(
                job_id, "event",
                f"dropped unsafe job env keys: {', '.join(sorted(dropped_env))}",
            )
        if spec.get("subagent_model"):
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = spec["subagent_model"]

        budget = spec.get("budget") or {}
        # [R2] No explicit budget → per-kind default cap (policy-overridable), so an
        # unbudgeted job can't hold a capacity slot forever.
        timeout_s, timeout_source = _resolve_timeout(spec, self.policy)
        if timeout_source == "default":
            await self._send_log(
                job_id, "event",
                f"no wallclock budget set; applying default cap {timeout_s:.0f}s "
                f"for kind={spec.get('kind') or 'claude'} "
                "(set budget.max_wallclock_min to override)",
            )

        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env,
                stdin=asyncio.subprocess.DEVNULL,  # headless: `claude -p` hangs on an open stdin with no TTY
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group, so cancel/timeout can kill the whole job tree
            )
        except OSError as e:
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
        self._active[job_id] = {"process": process, "is_docker": is_docker,
                                "cancelled": None, "since": time.monotonic()}

        tokens_used = 0
        declined = False
        decline_reason: Optional[str] = None
        result_text: Optional[str] = None
        # Bounded tails of each stream, kept only so a FAILED job can be diagnosed
        # (steward). Capped lines so a chatty job can't grow worker memory.
        tails: dict[str, deque] = {"stdout": deque(maxlen=40), "stderr": deque(maxlen=40)}

        async def relay(stream: asyncio.StreamReader, kind: str) -> None:
            nonlocal tokens_used, declined, decline_reason, result_text
            assert stream is not None
            while True:
                try:
                    line = await stream.readline()
                except ValueError:
                    # A single output line overran the 64 KiB stream buffer
                    # (which matches the server's per-append cap). Pre-R11 this
                    # exception KILLED the relay task, silently losing all
                    # subsequent output. Drop the line with a loud marker and
                    # keep relaying; the line's remainder drains as fragments.
                    await self._send_log(
                        job_id, "event",
                        "oversized output line dropped (> 64 KiB stream limit)")
                    continue
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                tails[kind].append(text)
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
            # [R2] Say which cap fired: an explicit budget vs the default cap (the
            # latter tells the operator how to opt for a longer run).
            if timeout_source == "default":
                msg = (f"default runtime cap exceeded ({timeout_s:.0f}s, no budget "
                       "set); killing — set budget.max_wallclock_min to run longer")
            else:
                msg = f"wallclock budget exceeded ({timeout_s:.0f}s); killing"
            await self._send_log(job_id, "event", msg)
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
        # NOTE: do NOT pop _active yet — keep the entry alive through the verify/
        # self-heal phase so a server cancel still routes to _kill_active_job (which
        # tears down the in-flight aux verifier/fix subprocess too). Popped below.
        def _teardown_reason() -> Optional[str]:
            """[R3] 'cancelled' (server cancel) or 'lease_lost' (lease
            reconciliation after a CP outage / re-lease): either way this attempt
            is torn down — report no terminal event, the server has moved on.
            'timeout' is NOT a teardown; the timed_out branch reports it."""
            r = (self._active.get(job_id) or {}).get("cancelled")
            return r if r in ("cancelled", "lease_lost") else None

        def _is_cancelled() -> bool:
            return _teardown_reason() is not None

        cancelled = _is_cancelled()

        exit_code = process.returncode
        terminal: dict[str, Any] = {"attempt": attempt, "tokens_used": tokens_used}
        if cancelled:
            # Torn down (server cancel, or lease lost to a newer attempt): the
            # control plane has moved on — post nothing, just unwind accounting.
            reason = _teardown_reason()
            self._active.pop(job_id, None)
            self._running = max(0, self._running - 1)
            print(f"[roost] job {job_id} torn down ({reason})", flush=True)
            return
        if timed_out:
            # [R2] Distinct error tokens so a timeout is tellable from an ordinary
            # failure — and a default-cap kill from an explicit-budget one.
            terminal.update(
                type="failed",
                error=("default_runtime_cap_exceeded" if timeout_source == "default"
                       else "wallclock_exceeded"),
                exit_code=exit_code)
        elif declined and exit_code == 0:
            # Self-declined clean exit: requeue for a better-fit node.
            # A non-zero exit means the triage subprocess itself crashed — that is a
            # failure, not a decision; fall through to the else branch below.
            terminal.update(type="declined", reason=decline_reason or "declined")
        elif exit_code == 0:
            # Trust loop: for agentic jobs that opt in, an INDEPENDENT verifier checks
            # the goal was actually achieved before we call it succeeded.
            goal = spec.get("task") or spec.get("intent")
            if _wants_verify(spec, is_auto) and goal:
                # [C4/H2] Bound the verify/self-heal phase to the job's remaining
                # budget (wallclock + tokens). Each _oneshot_agent is also capped per
                # call, and we post a `progress` event BEFORE every subprocess so the
                # 60s lease is renewed between phases (long verify/heal must not let
                # the lease lapse → double-execution).
                budget_exhausted_note: Optional[str] = None

                async def _phase_progress(activity: str) -> Optional[float]:
                    """Renew the lease (progress event) and return the per-subprocess
                    wallclock cap, or None if the budget is exhausted."""
                    nonlocal budget_exhausted_note
                    rem, exhausted = _budget_remaining(
                        budget, time.monotonic() - job_started, tokens_used)
                    if exhausted:
                        budget_exhausted_note = (
                            "budget exhausted; accepting current result without further "
                            "verify/self-heal")
                        return None
                    await self._post_event(job_id, {"type": "progress", "attempt": attempt,
                                                    "tokens_used": tokens_used,
                                                    "activity": activity})
                    return rem

                cap = await _phase_progress("🔎 verifying result")
                if cap is None:
                    # No budget even to verify: accept the raw result, mark unverified.
                    passed, evidence = None, "verification skipped (budget exhausted)"
                    heals = 0
                else:
                    passed, evidence, vtok = await self._run_verifier(
                        job_id, goal, result_text, timeout_s=cap)
                    tokens_used += vtok
                    # Self-healing (ease-of-use-plan Phase 3): if the verifier positively
                    # rejected the result, fix-and-re-verify on this same node, bounded.
                    heals = 0
                    while passed is False and heals < MAX_FIX_ATTEMPTS and not _is_cancelled():
                        heals += 1
                        cap = await _phase_progress(f"🔧 self-healing (attempt {heals})")
                        if cap is None:
                            break
                        await self._send_log(job_id, "event",
                                             f"verification failed; self-healing attempt {heals}: {evidence[:120]}")
                        fixed, _full, ftok = await self._oneshot_agent(
                            job_id, verify_mod.render_fix(goal, evidence, result_text),
                            label=f"fix{heals}", timeout_s=cap)
                        tokens_used += ftok
                        if fixed:
                            result_text = fixed
                        cap = await _phase_progress("🔎 re-verifying result")
                        if cap is None:
                            break
                        passed, evidence, vtok = await self._run_verifier(
                            job_id, goal, result_text, timeout_s=cap)
                        tokens_used += vtok
                if _is_cancelled():
                    reason = _teardown_reason()
                    self._active.pop(job_id, None)
                    self._running = max(0, self._running - 1)
                    print(f"[roost] job {job_id} torn down ({reason} during verify)", flush=True)
                    return
                terminal["tokens_used"] = tokens_used
                if budget_exhausted_note:
                    evidence = f"{evidence} [{budget_exhausted_note}]"
                bundle = {"output": result_text, "verified": passed is True,
                          "evidence": evidence, "self_heal_attempts": heals}
                if passed is False:
                    terminal.update(
                        type="failed", exit_code=0, result=bundle,
                        error=f"verification failed after {heals} self-heal attempt(s): {evidence}"[:300])
                elif passed is None:
                    # [M3] Inconclusive — verifier gave no usable verdict (crash/timeout/
                    # budget). Complete the job, but do NOT claim a confident success:
                    # verified=null with explicit evidence so the operator can tell.
                    bundle["verified"] = None
                    terminal.update(type="succeeded", exit_code=0, result=bundle,
                                    verified=None)
                else:
                    terminal.update(type="succeeded", exit_code=0, result=bundle)
            else:
                terminal.update(type="succeeded", exit_code=0,
                                result={"output": result_text} if result_text else None)
        else:
            terminal.update(type="failed", error=f"exit_code={exit_code}", exit_code=exit_code)
        # On a FAILED terminal event, attach a steward-authored root-cause `diagnosis`
        # (agentic haiku, with a deterministic mechanical fallback). Never touches the
        # success/decline paths, so it can't slow a healthy job down.
        if terminal.get("type") == "failed":
            terminal["diagnosis"] = await self._diagnose_failure(
                spec, exit_code=terminal.get("exit_code"),
                stdout_tail="\n".join(tails["stdout"]),
                stderr_tail="\n".join(tails["stderr"]),
                error=terminal.get("error"),
            )
        self._active.pop(job_id, None)
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
        node as the executor so it sees the artifacts. [H5] Registered in
        self._aux_procs[job_id] so a server cancel / timeout teardown of the parent job
        kills this subprocess too instead of leaking tokens."""
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

        # [H5] Register so a parent-job cancel/timeout can SIGKILL this aux process.
        self._aux_procs.setdefault(job_id, set()).add(proc)

        chunks: list[str] = []
        tokens = 0
        result: Optional[str] = None

        async def rel(stream: asyncio.StreamReader) -> None:
            nonlocal tokens, result
            while True:
                try:
                    line = await stream.readline()
                except ValueError:
                    # Same oversized-line guard as the job relay: never let one
                    # huge line kill the reader (the rest drains as fragments).
                    chunks.append("[oversized line dropped]")
                    continue
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
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                # Reap so we don't leave a zombie / dangling transport on timeout.
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.gather(t1, t2, return_exceptions=True)
        finally:
            # Deregister and reap on EVERY path (success, timeout, cancel-kill).
            procs = self._aux_procs.get(job_id)
            if procs is not None:
                procs.discard(proc)
                if not procs:
                    self._aux_procs.pop(job_id, None)
            for p in temp:
                p.unlink(missing_ok=True)
        return result, "\n".join(chunks), tokens

    async def _run_verifier(
        self, job_id: str, goal: str, result_text: Optional[str],
        *, timeout_s: float = VERIFY_HEAL_TIMEOUT,
    ) -> tuple[Optional[bool], str, int]:
        """Run an INDEPENDENT verifier (fresh context, adversarial) to check the goal was
        actually achieved. Returns (passed, evidence, tokens): True=verified,
        False=positively unmet (→ fail), None=inconclusive.

        [M3] An inconclusive verdict (None) usually means the verifier crashed/timed out
        and produced no usable PASS/FAIL line — NOT evidence of success. Retry the
        verifier ONCE before reporting inconclusive, so a single flake doesn't either
        block good work or silently pass bad work."""
        total_tokens = 0
        passed: Optional[bool] = None
        reason = "verifier produced no verdict"
        for attempt_i in range(2):
            result, full, tokens = await self._oneshot_agent(
                job_id, verify_mod.render_user(goal, result_text),
                system_prompt=verify_mod.system_prompt(), label="verify",
                timeout_s=timeout_s)
            total_tokens += tokens
            passed, reason = verify_mod.parse_verdict(result or full)
            verdict = "PASS" if passed else ("FAIL" if passed is False else "INCONCLUSIVE")
            await self._send_log(job_id, "event", f"verifier verdict: {verdict} — {reason[:160]}")
            print(f"[roost] job {job_id} verifier: {verdict} — {reason[:120]}", flush=True)
            if passed is not None:
                break
            if attempt_i == 0:
                await self._send_log(job_id, "event",
                                     "verifier inconclusive (no verdict); retrying once")
        if passed is None:
            reason = f"verification inconclusive (verifier produced no verdict): {reason}"
        return passed, reason, total_tokens

    async def _send_log(self, job_id: str, stream: str, data: str) -> None:
        try:
            r = await self.client.post(
                f"/workers/{self.worker_id}/jobs/{job_id}/logs",
                json={"stream": stream, "data": data},
            )
            if r.status_code >= 400:
                # Write-time log bounds (413 oversize / 429 row ceiling): the
                # job keeps running — logs are observability, not the work —
                # but say clearly WHY lines are being dropped.
                try:
                    detail = r.json().get("detail", r.text)
                except Exception:  # noqa: BLE001 — non-JSON error body
                    detail = r.text
                print(f"[roost] log line dropped by server "
                      f"({r.status_code}): {str(detail)[:200]}", flush=True)
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
