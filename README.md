<div align="center">

# Roost

**Your scattered machines become one agent-pluggable fleet — any agent app can run work
on it, verify it, and publish from it.**

Roost is the **personal compute-and-hosting backend that any agent plugs into**. Agent
apps — Claude Code, the Claude app, the Codex app, plain CLI, the phone apps — are the
**front doors**: they own intent capture and conversation. Roost is **the building**:
execution on hardware you own, independent verification (the trust loop), file movement,
durable serving, and receipts. The verbs are the product — **run · verify · transfer ·
serve/publish · observe · schedule** — and it's **model-vendor-neutral by construction**.

```bash
roost up                                    # zero → a running fleet-of-one
roost do "report the GPU model and free VRAM on a GPU box"
```

[Plug your agent in](#plug-your-agent-in) · [Quickstart](#quickstart) · [The trust loop](#the-trust-loop) · [Dashboard](#dashboard) · [Talk to it](#talk-to-your-fleet-mcp) · [Job kinds](#job-kinds--precise-control) · [Architecture](#architecture)

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

**Roost is that missing middle — the backend your agents plug into.** One lightweight
control plane turns your scattered machines into a fleet that *any* agent app can hand
work to. The front door (Claude Code, the Claude app, Codex, a script, a phone) owns the
conversation; Roost owns the **work**: it routes a plain-language goal to the right node,
runs it, and **verifies the result**. You don't pick a node, choose a job kind, or write
a spec.

Under the hood it's a **pull-based** orchestrator: workers long-poll the control plane,
lease a job, heartbeat while running, and report results — so a Pi on home Wi-Fi and a
cloud VM behind NAT both join with **no inbound ports** and no firewall holes. Each job
lands on a capable, free worker, and because jobs report liveness you can actually *see*
whether each one is healthy, stuck, or failed. If a control-plane outage outlives a
job's lease (60s), the job is requeued — and when the original worker reconnects, it
**aborts** its now-orphaned attempt (each heartbeat returns the jobs the server still
attributes to that worker) rather than finishing it twice; stale reports are rejected
by attempt number either way.

What makes it more than a job queue:

- **One front door.** `roost do "<goal>"` classifies, routes, runs, and verifies. That's the whole interface.
- **Verified, not just "exit 0".** Every agentic job is independently checked; "succeeded" means a verifier confirmed the goal was met — and a wrong result self-heals before failing honestly.
- **Self-selecting workers.** Hand a plain task to the fleet and a free worker's own agent decides if it's a good fit or routes it onward — correct GPU placement with zero `requires`.
- **You can watch it.** A live web panel, a terminal dashboard, and an MCP server all render the same model: what's running, its health, its cost, and its evidence.

---

## Plug your agent in

Roost is the building; your agent app is the front door. Point any agent at one control
plane and it gets the whole verb surface — **run · verify · transfer · serve/publish · observe ·
schedule**.

- **Claude Code (CLI):** `claude mcp add roost -- roost mcp` (with `ROOST_URL`/`ROOST_TOKEN`).
- **Claude app / desktop:** the same MCP server as a remote connector (needs a reachable
  LAN/tailscale URL).
- **Codex / anything HTTP:** bearer token, `POST /jobs` with `{kind: "auto", task: …}`,
  then poll `GET /jobs/{id}` or stream `/jobs/{id}/stream`.
- **Scripts / cron:** `roost do` / `roost exec` / `roost submit` one-liners; or skip
  cron — `roost schedule "<goal>" --every 6h` and the control plane runs it on the beat.

Mint a scoped token for non-admin front doors with `roost pair`. Full copy-paste recipes
per front door: **[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)**.

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

This puts a `roost` command on your PATH (`roost --version` confirms which build you
have). (Prefer pip? `pip install -e ".[dev]"` in a venv.)

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
| `roost schedule "<goal>" --every 6h` | Run work on an interval — the CP enqueues it each beat (no pile-up, no back-fill). |

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
plane to add agentic per-job narration to the story. Each active job is re-narrated at most
once per `ROOST_NARRATE_INTERVAL` seconds (default `20`; busy fleets can raise it to cut
token cost, demos can lower it); values below the `5`s floor — the sweep cadence — are
clamped, and a blank or garbage value falls back to the default.

The `$` estimate uses a per-session floor plus a per-Mtok marginal on each job's fresh
tokens (one rate ships by default). To price models differently, set `ROOST_PRICING` on the
control plane to a JSON map of model name/substring → `{base_usd, per_mtok_usd}`; unknown
models fall back to a `default` entry, and unset = today's numbers exactly. See
[`docs/DEPLOY.md`](docs/DEPLOY.md#cost-estimation-pricing-per-model-optional).

### Metrics (Prometheus)

The control plane exposes `GET /metrics` in Prometheus text exposition format
(`text/plain; version=0.0.4`) — no extra dependency, hand-rolled. It's **admin-only**:
send the admin token as a bearer header (a worker or scoped client token gets `403`; no
token gets `401`).

```
curl -H "Authorization: Bearer <admin-token>" http://<control-plane-host>:8787/metrics
```

Series include `roost_jobs{state="…"}` (queued/assigned/running/succeeded/failed/cancelled),
`roost_queue_depth`, `roost_workers_online`, `roost_workers_total`, `roost_blobs_count`,
`roost_blobs_bytes`, `roost_sites_count`, `roost_schedules_count`/`roost_schedules_enabled`,
`roost_lease_expirations_total`, and `roost_schedule_beats_total`. All values are derived
from the DB so they survive a control-plane restart — except `roost_schedule_beats_total`,
which is a process-local counter that resets on restart (the DB keeps no monotonic tick
record); `roost_lease_expirations_total` is DB-derived but ages out with log retention
(~24h). Each metric's `# HELP` line spells this out.

A minimal scrape config (Prometheus passes the bearer via `authorization`):

```yaml
scrape_configs:
  - job_name: roost
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials: <admin-token>
    static_configs:
      - targets: ["control-plane-host:8787"]
```

### Push notifications (opt-in)

So you learn a session finished without polling, the control plane can fire a
notification on **every terminal job** (succeeded / failed / cancelled). It's
**off by default** — set `ROOST_NOTIFY_URL` (or `roost serve --notify-url`) to an
ntfy.sh / UnifiedPush-style webhook to turn it on. The POST is fire-and-forget
(never on the job's critical path) and carries both a JSON body and ntfy display
headers. Full recipe and security notes: **[docs/DEPLOY.md](docs/DEPLOY.md)**.

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
`roost submit spec.yaml` (add `--detach` to not block). There are five kinds:

| Kind | What it runs | Where it lands |
|------|--------------|----------------|
| **`command`** | a shell command | any worker |
| **`claude`** | an agent job (an intent) | workers with an agent CLI configured |
| **`docker`** | an isolated container, optionally GPU | workers with Docker (and a GPU, if required) |
| **`auto`** | a plain task a free worker self-selects, then verifies | whichever worker fits — the default for `roost do` |
| **`codex`** | an agent job run via the Codex CLI | workers with `codex` configured |

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
plus optional soft `prefer: { worker: "<id|name>" }`, or a hard `target: "<worker id|name>"` that
pins a job to exactly one worker (it stays queued until that worker is free). Runnable specs
live in [`examples/`](examples/).

**Budgets & runtime caps:** `budget.max_wallclock_min`/`_sec` and `max_tokens` are
enforced by the worker. A job that sets **no** wallclock budget doesn't run forever —
a per-kind default cap applies (`command` 120 min, `claude`/`auto` 240 min, `docker`
360 min) and a capped kill reports `default_runtime_cap_exceeded` (distinct from
`wallclock_exceeded`). Override per worker via the policy key `default_wallclock_min`
(a number, or `{kind: minutes}`; `0` opts a worker out of default caps entirely).

**Inspect & control runs:**

| | |
|---|---|
| `roost jobs` | recent jobs |
| `roost history [--failed]` | recent finished runs with outcome, worker & cost; `--failed` shows only runs that failed or weren't verified ("what went wrong this week") |
| `roost status <id>` | one job's state (incl. queued/delivered/dropped input counts) |
| `roost logs <id> [--follow]` | job output |
| `roost send <id> <text> [--wait]` | send a follow-up message to a **running** job (see *Interactive follow-up* below) |
| `roost exec <worker> -- <cmd>` | run a command on one specific node — no SSH, via the job channel (great for a node whose SSH is unreachable) |
| `roost tree <root> --health` | a dispatch's whole job tree + per-job liveness; each child shows the captain's `↳ why:` plan reason when one was recorded |
| `roost cancel <id> [--tree]` | cancel a job (or its lineage) |
| `roost workers` | the live fleet (a node whose GPU probe errored shows `gpu:DETECTION-FAILED`, distinct from a genuinely GPU-less node) |
| `roost prune-workers [--days N]` | (admin) delete ghost worker rows not seen in N days (default 7); never touches live or running nodes |
| `roost capabilities` | what the fleet can do, in plain language (flags any node whose GPU detection failed) |
| `roost backup <dest.db>` | (admin) download a consistent online snapshot of the control-plane DB — works against a remote CP; restore is in [docs/DEPLOY.md](docs/DEPLOY.md) |

### Interactive follow-up (send input to a running job)

`roost send <job-id> "<message>"` queues a message for a **running** job; the worker
running it delivers it to the live process. This is a back-channel over the same pull
model everything else uses — the message lands in a durable queue, the owning worker
fetches it on its next heartbeat, delivers it, and acks the outcome.

**Delivery depends on the job kind — and Roost is honest about what it can do:**

| Kind | Live delivery? | How |
|------|----------------|-----|
| **`command`** | **yes** | written to the process's **stdin** (newline-terminated). The process must actually read stdin (e.g. `read line`, a REPL, a prompt). |
| `claude` / `auto` / `codex` | **no** | agent jobs run `claude -p` **one-shot with stdin closed** (an open stdin with no TTY hangs the CLI), so there's no live channel to write to. |
| `docker` | **no** | the container runs without an interactive stdin (`docker run`, no `-i`). |

Every input ends in one of three states, never silently lost:

- **queued** — accepted, waiting for the owning worker to pull it.
- **delivered** — the worker wrote it to the live process (command jobs).
- **dropped** — undeliverable: a kind that can't take mid-run stdin, or the process
  already exited. The reason is recorded and shown by `roost status` / `roost send --wait`.

Sending to a **terminal** job is rejected immediately (`409`) rather than dropped later.
`roost status <id>` shows `inputs: N queued · N delivered · N dropped`; `roost send --wait`
blocks until your message is delivered or dropped and tells you which.

> **Steering an agent job** is a deliberate non-goal of this slice: `claude -p` can't accept
> mid-run input. The faithful pattern today is a *follow-up job* that carries the parent's
> context (what `roost history` goal-memory and the mobile app's terminal-state composer
> already do). True agent stdin-steering waits on an interactive agent-runner mode.

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
python -m pytest -q                               # 636 tests
```

Contributions welcome. Keep credentials and machine-specific config out of commits
(see [`.gitignore`](.gitignore)).

## License

MIT — see [LICENSE](LICENSE).
