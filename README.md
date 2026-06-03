<div align="center">

# Roost

**Turn your scattered machines into one fleet you run by just saying what you want.**

Drive laptops, servers, Raspberry Pis, GPU boxes, and cloud VMs from a single control
plane. State a goal in plain language — Roost picks the right node, runs it, and an
independent verifier checks it actually worked before telling you it succeeded.

```bash
roost up                                    # zero → a running fleet-of-one
roost do "report the GPU model and free VRAM on a GPU box"
```

[Quickstart](#quickstart) · [The trust loop](#the-trust-loop) · [Dashboard](#dashboard) · [Talk to it](#talk-to-your-fleet-mcp) · [Job kinds](#job-kinds--precise-control) · [Architecture](#architecture)

</div>

---

## The problem

You've accumulated machines — a desktop with a GPU, a couple of Raspberry Pis, an old
laptop, a cloud VM or two. Most of them sit idle most of the time. The moment you have
work to spread across them — train something on the GPU box, lint a repo on a spare core,
fan a hundred agent tasks out in parallel — your options are both bad:

- **Do it by hand.** SSH into each box, remember which one has the GPU, copy files around,
  start each run, babysit it, and stitch the results back together yourself. It doesn't
  scale past a couple of machines, and you never really know whether a remote job is alive,
  stuck, or quietly dead.
- **Stand up real cluster infra** (Kubernetes, Slurm, Ray). Heavy to operate, assumes a
  uniform cluster with open inbound networking, and none of it is built for *agent* jobs —
  running an agent on the right node, with the right credentials, and knowing whether it's
  actually making progress.

Neither fits a pile of **mismatched, personally-owned machines** scattered across home
Wi-Fi, WSL, and the cloud — especially now that a lot of the work you want to run is
*agents*, not just batch commands.

## What Roost does

**Roost is that missing middle.** One lightweight control plane turns your scattered
machines into a fleet you hand work to in plain language. You don't pick a node, choose a
job kind, or write a spec — you say what you want, and Roost routes it, runs it, and
**verifies the result**.

Under the hood it's a **pull-based** orchestrator: workers long-poll the control plane,
lease a job, heartbeat while running, and report results — so a Pi on home Wi-Fi and a
cloud VM behind NAT both join with **no inbound ports** and no firewall holes. Each job
lands on a capable, free worker, and because jobs report liveness you can actually *see*
whether each one is healthy, stuck, or failed.

What makes it more than a job queue:

- **One front door.** `roost do "<goal>"` classifies, routes, runs, and verifies. That's the whole interface.
- **Verified, not just "exit 0".** Every agentic job is independently checked; "succeeded" means a verifier confirmed the goal was met — and a wrong result self-heals before failing honestly.
- **Self-selecting workers.** Hand a plain task to the fleet and a free worker's own agent decides if it's a good fit or routes it onward — correct GPU placement with zero `requires`.
- **You can watch it.** A live web panel, a terminal dashboard, and an MCP server all render the same model: what's running, its health, its cost, and its evidence.

---

## Install

Requires Python **3.10+** (3.12 recommended). Easiest with [`uv`](https://docs.astral.sh/uv/):

```bash
# from a checkout of this repo
uv tool install --python 3.12 .

# …or straight from git
uv tool install --python 3.12 "git+https://github.com/<you>/roost"
```

> **Pin Python 3.12.** `uv` may otherwise pick a newer interpreter the async HTTP
> client doesn't support yet.

This puts a `roost` command on your PATH. (Prefer pip? `pip install -e ".[dev]"` in a venv.)

---

## Quickstart

### `roost up` — zero to a running fleet

One command takes you from nothing to a working single-node fleet on this machine:

```bash
roost up
```

It starts (or reuses) a control plane, enrolls this machine as a worker, starts the
worker, runs an end-to-end smoke test, and prints the panel URL plus next steps. It's
**idempotent** and **sudo-free** — re-run it any time. Want an isolated instance alongside
an existing one? `roost up --port 8788 --db /tmp/r.db`.

Then just use it:

```bash
roost do "report the OS and free memory on a CPU box"
roost capabilities          # what can this fleet do?
roost workers               # see the fleet
```

> Using [Claude Code](https://claude.com/claude-code)? Open this repo and run
> **`/roost-quickstart`** to be walked through setup with consent at each step. Add more
> machines later with **`/roost-onboard`**; watch jobs with **`/roost-oversee`**.

---

## The trust loop

`roost do "<goal>"` is the one front door. You say what you want; Roost does the rest:

1. **Routes it.** A router (Sonnet) classifies the request — one task or many, ambiguous or
   clear, safe or destructive. It asks a clarifying question **only if it genuinely can't
   tell** what you mean, and confirms before anything destructive.
2. **Places it.** A single task goes to the best-fit free node (`kind: auto` — see below);
   a multi-part goal goes to a **captain agent** that splits it across nodes and merges the
   results. You never choose a node or write a spec.
3. **Verifies it.** This is the heart of Roost. An **independent verifier** checks that the
   goal was *actually achieved* and hands back a plain-language **evidence bundle** — not
   just an exit code.
4. **Self-heals it.** If verification fails, Roost runs a **bounded** fix-and-re-verify pass
   before failing honestly, with the evidence attached.

So `roost do "write the first 10 primes to /tmp/p.txt"` doesn't just report "exit 0" — it
reports something like:

```
✓ verified — /tmp/p.txt exists and contains exactly: 2 3 5 7 11 13 17 19 23 29
result: wrote 10 primes to /tmp/p.txt
```

…or, when it couldn't:

```
✗ NOT verified — /tmp/p.txt contained 9 numbers (missing 29); self-heal re-ran the task
  but the file still had 9 entries. Reporting failure with evidence rather than a false pass.
```

The lower-level forms, if you want them:

| Command | What it's for |
|---------|---------------|
| `roost do "<goal>"` | **The front door.** Classify → route → run → verify. Use this. |
| `roost run "<task>"` | One verified task on a self-selecting node (`kind: auto`). The single-task primitive `do` calls. |
| `roost dispatch "<goal>"` | Split a multi-part goal across nodes via the captain agent. |
| `roost submit <spec.yaml>` | The precise/expert escape hatch — exact `requires`, kind, container, budget. |

**Model policy:** Sonnet by default everywhere. Haiku is used only for tasks the router
flags trivially simple (print a value, read a small file, report a hostname). The verifier
and self-heal **always** use Sonnet — correctness is never delegated to the cheap model.

---

## Dashboard

The control plane serves a live web panel — open it at:

```
http://<control-plane-host>:8787/panel?token=<admin-token>
```

It gives you, at a glance:

- A **fleet verdict bar** — one line that answers "is anything wrong?"
- A **task board** where each job shows its *story*: its phase (running · verifying ·
  self-healing · done), a health verdict, cost in `$`/tokens, live narration, and the
  verification evidence when it finishes.
- **Node health cards** — each machine, what it's running, free VRAM, load.

The panel, the terminal dashboard (`scripts/fleet`), and the MCP inbox all render the same
single `GET /derived` model, so they never disagree. Set `ROOST_NARRATE=1` on the control
plane to add agentic per-job narration to the story.

---

## Talk to your fleet (MCP)

`roost mcp` runs an MCP server (stdio) so a Claude Code console drives the fleet
**conversationally**, with cross-turn context — *"run the tests on a GPU box"*, *"what's
running?"*, *"why did that fail?"*. Add it to any Claude Code project's `.mcp.json`:

```json
{ "mcpServers": { "roost": { "command": "roost", "args": ["mcp"] } } }
```

The tools mirror the trust loop: **`roost_do`** (say what you want), **`roost_runs`** (an
inbox of recent runs), **`roost_result`** (the verified outcome + evidence for a run), and
**`roost_capabilities`** (what the fleet can do).

---

## Job kinds & precise control

`roost do` covers the common case. When you want exact control, write a spec and
`roost submit spec.yaml` (add `--detach` to not block). There are four kinds:

| Kind | What it runs | Where it lands |
|------|--------------|----------------|
| **`command`** | a shell command | any worker |
| **`claude`** | an agent job (an intent) | workers with an agent CLI configured |
| **`docker`** | an isolated container, optionally GPU | workers with Docker (and a GPU, if required) |
| **`auto`** | a plain task a free worker self-selects, then verifies | whichever worker fits — the default for `roost do` |

**`kind: auto`** is the self-selecting path: the task goes to a free worker whose own agent
introspects the machine and either does it or **declines so it routes to a better-fit
node** — in testing this gets GPU routing right 100% of the time with no `requires` at all.

```yaml
# command job — any worker
command: "python3 -c 'import platform; print(platform.platform())'"
requires: { tools: [python3] }
budget: { max_wallclock_min: 1 }
```

```yaml
# agent job — runs where the agent CLI is configured
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
plus optional soft `prefer: { worker: "<id>" }`. Runnable specs live in
[`examples/`](examples/).

**Inspect & control runs:**

| | |
|---|---|
| `roost jobs` | recent jobs |
| `roost status <id>` | one job's state |
| `roost logs <id> [--follow]` | job output |
| `roost tree <root> --health` | a dispatch's whole job tree + per-job liveness |
| `roost cancel <id> [--tree]` | cancel a job (or its lineage) |
| `roost workers` | the live fleet |
| `roost capabilities` | what the fleet can do, in plain language |

---

## Auth for agent jobs

Agent jobs run an agent CLI on the worker, which needs credentials. The method is **always
an explicit choice** (the onboarding skill asks — never silent):

| Method | How | Tradeoff |
|--------|-----|----------|
| **Copy host credentials** (v1 default) | The control plane provisions the operator's agent creds to a worker at enroll time. | Simplest; one subscription. Replicates a token to another machine — trusted hosts only. |
| **Per-worker API key** | Set an API key on the worker; enroll with policy `{"provision_claude": false}`. | Revocable per machine, no token spreading; bills API credits. |
| **Interactive login** | Log the agent CLI in once on the worker. | No copying; needs a human there. |

When creds are copied, workers **re-pull fresh credentials periodically** so rotating OAuth
tokens don't go stale. On shared machines, set `CLAUDE_CONFIG_DIR` so Roost's credentials
don't disturb the user's own agent.

---

## Security

Roost handles credentials and runs untrusted-ish work across machines, so the defaults are
strict:

- **The control plane requires a shared admin token.** It **refuses to bind to a
  non-loopback address without one** — you'd have to pass an explicit `--insecure` flag to
  run an open server, and it warns loudly if you do.
- **Worker job env is sanitized.** A job can't set proxy or credential-redirect variables
  (`*_PROXY`, `ANTHROPIC_*`, `CLAUDE_CODE_*`, `NODE_OPTIONS`, `LD_*`, …) that could
  exfiltrate the operator's token or inject code.
- **Docker mounts are policed.** Mounting sensitive host paths (home, creds, `~/.ssh`, `/`,
  `/etc`, the Roost DB) or using `network: host` is **blocked** unless a worker's policy
  explicitly opts in (`allow_host_mounts: true`, trusted use only).
- Never commit a token, DB, or `*.credentials.json` — the [`.gitignore`](.gitignore) blocks them.

---

## Running in Docker

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

## macOS control app (optional)

[`mac-app/build.sh`](mac-app/build.sh) builds **RoostFleet.app** — a double-click app with
a floating, always-on-top panel (which node is doing what, live) plus a Terminal running an
agent console wired to the fleet via `roost mcp`:

```bash
mac-app/build.sh http://<control-plane-host>:8787 "<admin-token>"
```

---

## Architecture

```
            do / run / dispatch / submit                long-poll for work
  operator ─────────────────────────────▶  control plane  ◀──────────────────  worker · worker · worker
 (CLI · captain · MCP)                      FastAPI+SQLite    lease · heartbeat · result    (pull model)
                                                  │
                                       verifier + self-heal  →  evidence bundle
```

| Module | Role |
|--------|------|
| [`roost/server.py`](roost/server.py) | Control plane: enrollment, placement, leasing, heartbeats, liveness facts, the `/derived` model, the `/panel` dashboard, credential provisioning. |
| [`roost/worker.py`](roost/worker.py) | Worker loop: capability detection (CPU/GPU/tools/Docker), pull/lease/run/report, the executors, env sanitization, mount policy, creds refresh. |
| [`roost/verify.py`](roost/verify.py) | The trust loop: verifier prompt + verdict parsing — "succeeded" means *verified*. |
| [`roost/matcher.py`](roost/matcher.py) | `requires:` ↔ capability matching. |
| [`roost/captain.py`](roost/captain.py), [`roost/mcp.py`](roost/mcp.py) | The captain agent (`roost dispatch`) and the MCP server (`roost mcp`). |
| [`roost/cli.py`](roost/cli.py) | The `roost` command. |
| [`roost/service.py`](roost/service.py) | Install a worker/CP as a durable service (systemd / launchd). |
| [`.claude/skills/`](.claude/skills) | Claude Code skills: `roost-quickstart`, `roost-onboard`, `roost-oversee`. |

---

## Development

```bash
uv tool install --python 3.12 --with pytest .    # or: pip install -e ".[dev]"
python -m pytest -q                               # 161 tests
```

Contributions welcome. Keep credentials and machine-specific config out of commits
(see [`.gitignore`](.gitignore)).

## License

MIT — see [LICENSE](LICENSE).
