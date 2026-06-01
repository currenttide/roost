---
name: roost-quickstart
description: >-
  Guided first-run setup for Roost — take a user from zero to a working fleet:
  install the roost CLI, start a control plane, enroll this machine as the first
  worker, set up Claude auth for agent jobs (an explicit user choice), and run a
  smoke test. Guides AND runs each step with the user's consent, pausing before
  anything that installs software or touches credentials. Use when someone is new
  to Roost and wants to get started / set it up / "how do I run this", has just
  cloned the repo, or asks to stand up / bootstrap a control plane and first node.
  For adding MORE machines after the first, use roost-onboard instead.
---

# Roost quickstart

Your job: get the user from nothing to a **running fleet they can dispatch work to**,
explaining each step and running it for them — but **pausing for explicit consent**
before anything that installs software, opens a network port, or touches credentials.
You orchestrate; the user stays in control of their machine.

Work through the steps in order. After each, confirm it actually worked (don't assume).
Keep the user oriented with a one-line "what this does / why" before each action.

## 0. Orient & detect

Tell the user, in two sentences, what they're about to set up: a **control plane** on
this machine plus this machine as the **first worker**, after which they can
`roost dispatch "<goal>"`. Then detect the environment:

- OS (`uname -s`), shell, whether this is a Linux box, macOS, or WSL.
- Is `roost` already on PATH? `command -v roost && roost --help >/dev/null`
- Is `uv` available? `command -v uv` (it's the recommended installer).
- Is `claude` (Claude Code) installed? `command -v claude` — only needed for **agent**
  (`claude`) jobs; `command` and `docker` jobs don't need it.

## 1. Install the roost CLI (if missing)

If `roost` isn't installed, install it from this repo. **Ask first** (it installs a
tool on their system). Pin Python 3.12 — newer interpreters can break the async client:

```bash
uv tool install --python 3.12 .          # from the repo root
```

If `uv` is missing, offer to install it (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
or fall back to `pip install -e .` in a venv. Verify: `roost --help` runs.

## 2. Start the control plane

The control plane is the FastAPI+SQLite server workers pull from. It needs an **admin
token** — a shared secret you'll reuse for operator commands. Generate one and start it:

```bash
export ROOST_TOKEN=$(openssl rand -hex 12)      # the admin token — SAVE THIS
export ROOST_URL=http://127.0.0.1:8787
roost serve --host 0.0.0.0 --port 8787 &        # backgrounded; --provision-auth is on by default
```

- `--host 0.0.0.0` lets other machines on the network reach it later; use `127.0.0.1`
  if you only want a single-machine fleet for now. **Mention the security implication**:
  `0.0.0.0` exposes the port on the LAN — fine on a trusted home network, but the admin
  token is the only thing protecting it, so keep it secret.
- **Persist the operator env** so future shells/commands work without re-exporting.
  With consent, write `~/.roost/env`:
  ```bash
  mkdir -p ~/.roost && chmod 700 ~/.roost
  printf 'export ROOST_URL=%s\nexport ROOST_TOKEN=%s\n' "$ROOST_URL" "$ROOST_TOKEN" > ~/.roost/env
  chmod 600 ~/.roost/env
  ```
  Tell them to `source ~/.roost/env` in new shells (or add it to their shell profile).
  Operator commands (`roost workers/jobs/dispatch`, `scripts/fleet`) authenticate with
  this **admin** token via `ROOST_TOKEN`.
- Verify: `roost ping` → should report the control plane is up. If it fails, check the
  server didn't error (port in use? run it in the foreground once to see logs).

## 3. Enroll this machine as the first worker

Mint a single-use join token, enroll, and start the worker loop:

```bash
TOKEN=$(roost enroll-token --label local --no-curl | grep -oE 'token: \S+' | awk '{print $2}')
roost enroll "$TOKEN" --url "$ROOST_URL" --name local
```

`enroll` writes the worker's own config to `~/.config/roost/config.toml`. Then start it
— **ask which durability the user wants**:

- **Quick / foreground** (just trying it out): `roost worker &`
- **Durable service** (survives logout/reboot): `roost service install --start`
  (systemd `--user` on Linux, launchd on macOS). Recommended for anything beyond a test.

Verify: `roost workers` shows `local` as `idle` within a few seconds.

## 4. Set up Claude auth for agent jobs

Only needed if the user wants **`claude` (agent) jobs** (plain `roost dispatch` uses a
captain agent and benefits from it). This is a **credential decision — ask the user**
with AskUserQuestion; never copy credentials silently. Present the methods:

- **Use this machine's existing Claude login** *(default when the operator already runs
  Claude Code here)* — the worker is the same machine, so its `claude` is already
  authenticated; nothing to copy. Just confirm `claude` is installed and logged in.
- **Per-worker API key** — set `ANTHROPIC_API_KEY` in the worker's environment / service
  unit. Revocable, bills API credits, no token spreading.
- **Interactive login** — run `claude` once and log in by hand.
- **Skip for now** — the worker runs only `command` / `docker` jobs.

(When you add *remote* machines later via `/roost-onboard`, the control plane's
`--provision-auth` can copy the host's credentials to them at enroll time — but that
replicates a token to another box, so it's always an explicit choice there too.)

Confirm with a tiny agent job once auth is set (see step 5).

## 5. Smoke test

Prove the fleet works end to end:

```bash
roost submit examples/echo.yaml          # trivial command job on any worker
```
Watch it reach `succeeded` (`roost jobs`, or `roost status <id>`, or `roost logs <id>`).

Then, if Claude auth is set up, try the plain-language path:
```bash
roost dispatch "print the OS and CPU count on a worker, then summarize the result"
```
Use `/roost-oversee` to watch a dispatch if it's non-trivial.

Show the user their dashboards:
- Terminal: `scripts/fleet` (workers, GPUs, recent jobs).
- Web: `http://<this-host>:8787/panel?token=<admin-token>` — live "who's doing what".

## 6. Wrap up — what they have and what's next

Summarize plainly: control plane up at `$ROOST_URL`, `local` worker running (note
foreground vs durable), auth method chosen, smoke test result. Then point them on:

- **Add more machines:** `/roost-onboard` (Pis, servers, GPU boxes, cloud VMs — they
  pull from this control plane; remote nodes reach it at `http://<this-host>:8787`).
- **Monitor jobs:** `/roost-oversee`.
- **Run in Docker / a full GPU stack:** `docker/docker-compose.yml`, `docker/stack.yml`.
- **macOS control app:** `mac-app/build.sh`.

## Notes & gotchas

- **Pin Python 3.12** for any roost install (`uv tool install --python 3.12`).
- The admin token is the *only* thing protecting the control plane — keep it secret,
  never commit it (`.gitignore` blocks `admin_token`, `*.db`, `*.credentials.json`).
- For a real (non-test) deployment, point the control-plane DB at a persistent path
  (`roost serve --db <path>` or `ROOST_DB`) so enrollments + history survive a restart,
  and run the worker as a durable service (step 3).
- If `roost ping` fails after `serve`, the server likely errored on startup — re-run
  `roost serve` in the foreground to read the error (port already in use is common).
- Remote workers need to *reach* this host's `:8787`. On a LAN that's the host's IP;
  across networks you'll need a tunnel/VPN or a publicly reachable control plane.
