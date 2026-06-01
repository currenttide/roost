<div align="center">

# 🪺 Roost

**A pull-based orchestrator for agent jobs across a fleet of heterogeneous machines.**

Drive laptops, servers, Raspberry Pis, GPU boxes, and cloud VMs from one control plane.
State a goal in plain language — a captain agent splits it and places each piece on the
best-fit node. Or submit jobs directly.

[Quickstart](#-quickstart) · [Job kinds](#-submitting-jobs) · [Auth](#-auth-for-agent-jobs) · [Docker](#-running-in-docker) · [Architecture](#-architecture)

</div>

---

## The problem

You've accumulated machines — a desktop with a GPU, a couple of Raspberry Pis, an old
laptop, a cloud VM or two. Most of them sit idle most of the time. The moment you have
work to spread across them — train something on the GPU box, lint a repo on a spare core,
fan a hundred agent tasks out in parallel — your options are both bad:

- **Do it by hand.** SSH into each box, remember which one has the GPU, copy files around,
  start each run, babysit it, and stitch the results back together yourself. It doesn't
  scale past a couple of machines, and you never really know if a remote job is alive,
  stuck, or quietly dead.
- **Stand up real cluster infra** (Kubernetes, Slurm, Ray). Heavy to operate, assumes a
  uniform cluster with open inbound networking, and none of it is built for *agent* jobs —
  running `claude -p` on the right node, with the right credentials, and knowing whether
  it's actually making progress.

Neither fits a pile of **mismatched, personally-owned machines** scattered across home
Wi-Fi, WSL, and the cloud — especially now that a lot of the work you want to run is
*agents*, not just batch commands. There's no inbound-port-free way to say "run this on
whichever of my boxes can handle it" and trust that it happened.

## What Roost does

**Roost is that missing middle.** One lightweight control plane turns your scattered
machines into a fleet you can hand work to in plain language. Workers **pull** work
(long-poll), lease a job, heartbeat while running, and report results — so a Pi on home
Wi-Fi and a cloud VM behind NAT both join with **no inbound ports**. Each job declares
what it needs (`requires:`); the control plane places it on a capable, free worker, and
because jobs report liveness you can actually *see* whether each one is healthy, stuck, or
failed.

Three kinds of job:

| Kind | What it runs | Where it lands |
|------|--------------|----------------|
| **`command`** | a shell command | any worker |
| **`claude`** | an agent job (`claude -p` with an intent) | workers with Claude Code set up |
| **`docker`** | an isolated container, optionally GPU | workers with Docker (and a GPU, if required) |

### Why pull-based?

The control plane never connects *to* a worker — workers dial out. That buys you:

- **No firewall holes on workers.** A Pi on home Wi-Fi and a cloud VM behind NAT enroll the same way.
- **Best-fit placement.** Jobs declare `requires:` (CPU, GPU VRAM, tools, Docker); the control plane matches within a short grace window so the *best* free node wins, not just the first.
- **Self-healing.** Leases expire, heartbeats catch stalls, jobs requeue.
- **Plain-language dispatch.** `roost dispatch "<goal>"` runs a captain agent that reads the live fleet, splits the goal, places sub-jobs, and merges the results.

---

## 📦 Install

Requires Python **3.10+** (3.12 recommended). Easiest with [`uv`](https://docs.astral.sh/uv/):

```bash
# from a checkout of this repo
uv tool install --python 3.12 .

# …or straight from git once you've pushed it
uv tool install --python 3.12 "git+https://github.com/<you>/roost"
```

> ⚠️ **Pin Python 3.12.** `uv` may otherwise pick a newer interpreter the async HTTP
> client doesn't support yet.

This puts a `roost` command on your PATH. (Prefer pip? `pip install -e ".[dev]"` in a venv.)

---

## 🚀 Quickstart

### Option A — let Claude set it up (recommended)

If you use [Claude Code](https://claude.com/claude-code), open this repo and run:

```
/roost-quickstart
```

The skill takes you from zero to a running fleet — install the CLI, start the control
plane, enroll this machine as the first worker, wire up Claude auth, and run a smoke
test — **running each step with your consent** and pausing before anything that installs
software, opens a port, or touches credentials.

> Add more machines later with **`/roost-onboard`**. Watch jobs with **`/roost-oversee`**.

### Option B — by hand

```bash
# 1. Start the control plane with an admin token (a shared secret you'll reuse)
export ROOST_TOKEN=$(openssl rand -hex 12)        # save this
export ROOST_URL=http://127.0.0.1:8787
roost serve --host 0.0.0.0 --port 8787 &
roost ping                                         # → OK

# 2. Enroll this machine as a worker and start pulling jobs
TOKEN=$(roost enroll-token --label local --no-curl | grep -oE 'token: \S+' | awk '{print $2}')
roost enroll "$TOKEN" --url "$ROOST_URL" --name local
roost worker &                                     # (or: roost service install --start)

# 3. Run something
roost submit examples/echo.yaml                    # a trivial command job
roost dispatch "print the OS and CPU count on a worker, then summarize"
roost workers                                       # see the fleet
```

Dashboards: `scripts/fleet` (terminal) and a live web panel at
`http://<host>:8787/panel?token=<admin-token>` — one card per node, busy nodes show the
job they're running.

---

## 🧩 Submitting jobs

`roost dispatch "<goal>"` is the plain-language path. For precise control, write a spec
and `roost submit spec.yaml` (add `--detach` to not block):

```yaml
# command job — any worker
command: "python3 -c 'import platform; print(platform.platform())'"
requires: { tools: [python3] }
budget: { max_wallclock_min: 1 }
```

```yaml
# agent job — runs `claude -p` where Claude Code is configured
kind: claude
intent: "Summarize README.md and list open TODOs."
requires: { tools: [claude] }
budget: { max_tokens: 100000, max_wallclock_min: 10 }
```

```yaml
# GPU container job — runs in an isolated container on a GPU node
kind: docker
image: "pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime"
command: "python train.py --epochs 10"
requires: { docker_gpu: true, gpu_vram_gb: ">=40" }
container: { gpus: "all", cpus: "32", memory: "128g", volumes: ["/data:/data:ro"], shm_size: "16g" }
budget: { max_wallclock_min: 240 }
```

**Placement** = hard `requires:` (`gpu_vram_gb`, `tools`, `docker_gpu`, `hostname: "==<name>"`)
+ optional soft `prefer: { worker: "<id>" }`.

**Inspect & control runs:**

| | |
|---|---|
| `roost jobs` | recent jobs |
| `roost status <id>` | one job's state |
| `roost logs <id> [--follow]` | job output |
| `roost tree <root> --health` | a dispatch's whole job tree + health |
| `roost cancel <id> [--tree]` | cancel a job (or its lineage) |
| `roost workers` | the live fleet |

Runnable specs live in [`examples/`](examples/).

---

## 🔑 Auth for agent jobs

Agent (`claude`) jobs run Claude Code on the worker, which needs credentials. The method
is **always an explicit choice** (the onboarding skill asks — never silent):

| Method | How | Tradeoff |
|--------|-----|----------|
| **Copy host credentials** | Control plane run with `--provision-auth` hands the operator's Claude creds to a worker at enroll time. | Simplest; one subscription. Replicates a token to another machine — trusted hosts only. Linux→Linux (macOS uses Keychain). |
| **Per-worker API key** | Set `ANTHROPIC_API_KEY` on the worker; enroll with policy `{"provision_claude": false}`. | Revocable per machine, no token spreading; bills API credits. |
| **Interactive login** | Run `claude` once on the worker and log in. | No copying; needs a human there. |

When creds are copied, workers **re-pull fresh credentials periodically** so rotating
OAuth tokens don't go stale. On shared machines, set `CLAUDE_CONFIG_DIR` so Roost's
credentials don't disturb the user's own Claude.

> 🔒 Copying real credentials across machines is sensitive. Keep it to hosts you own on a
> trusted network, prefer per-worker API keys for anything shared, and never commit a
> token, DB, or `*.credentials.json` (the `.gitignore` blocks them).

---

## 🐳 Running in Docker

Everything can run in containers so jobs don't disturb the host or each other.

- **[`docker/docker-compose.yml`](docker/docker-compose.yml)** — one or more isolated worker containers on a host.
- **[`docker/stack.yml`](docker/stack.yml)** — the full stack: control plane + a GPU-enabled worker that launches isolated *per-job* containers on the host daemon (Docker-as-executor).

```bash
uv build --wheel
export ROOST_TOKEN=<admin-token>
export DOCKER_GID=$(getent group docker | cut -d: -f3)
docker compose -f docker/stack.yml up -d --build
```

See the header comments in each file for the full recipe.

---

## 🖥 macOS control app (optional)

[`mac-app/build.sh`](mac-app/build.sh) builds **RoostFleet.app** — a double-click app with
a floating, always-on-top panel (which node is doing what, live) plus a Terminal running
Claude Code wired to the fleet:

```bash
mac-app/build.sh http://<control-plane-host>:8787 "<admin-token>"
```

---

## 🏗 Architecture

```
            submit / dispatch                    long-poll for work
  operator ───────────────────▶   control plane   ◀───────────────────  worker · worker · worker
 (CLI / captain agent)            FastAPI+SQLite      lease · heartbeat · result     (pull model)
```

| Module | Role |
|--------|------|
| [`roost/server.py`](roost/server.py) | Control plane: enrollment, placement, leasing, heartbeats, liveness facts, credential provisioning. |
| [`roost/worker.py`](roost/worker.py) | Worker loop: capability detection (CPU/GPU/tools/Docker), pull/lease/run/report, the three executors, creds refresh. |
| [`roost/matcher.py`](roost/matcher.py) | `requires:` ↔ capability matching. |
| [`roost/captain.py`](roost/captain.py), [`roost/mcp.py`](roost/mcp.py) | The captain agent (`roost dispatch`) and its MCP tools. |
| [`roost/cli.py`](roost/cli.py) | The `roost` command. |
| [`roost/service.py`](roost/service.py) | Install a worker/CP as a durable service (systemd / launchd). |
| [`.claude/skills/`](.claude/skills) | Claude Code skills: `roost-quickstart`, `roost-onboard`, `roost-oversee`. |

---

## 🛠 Development

```bash
uv tool install --python 3.12 --with pytest .    # or: pip install -e ".[dev]"
python -m pytest -q                               # run the suite
```

Contributions welcome. Keep credentials and machine-specific config out of commits
(see [`.gitignore`](.gitignore)).

## 📄 License

MIT — see [LICENSE](LICENSE).
