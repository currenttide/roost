# Plug your agent into Roost

Roost is the **building** — execution on hardware you own, independent verification,
file movement, durable serving, and receipts. Your agent app is the **front door**: it
owns the conversation; Roost owns the work. Any agent that speaks MCP or HTTP plugs in.

Every front door talks to one control plane. Point it at your URL and a token:

```bash
export ROOST_URL=http://<control-plane-host>:8787   # loopback for local, LAN/tailscale for remote
export ROOST_TOKEN=<token>                           # admin token, or a scoped pair-token
```

---

## Claude Code (CLI) — 60 seconds

```bash
ROOST_URL=$ROOST_URL ROOST_TOKEN=$ROOST_TOKEN claude mcp add roost -- roost mcp
```

That registers Roost's stdio MCP server. Your agent now has these tools (from
[`roost/mcp.py`](../roost/mcp.py)):

| Tool | What it does |
|------|--------------|
| `roost_do` | The main verb — do a plain-language **goal** on the fleet (classify → route → run → verify). Returns `{run_id, state}`. |
| `roost_runs` | The inbox: recent + in-flight runs with phase, verified flag, one-line result. |
| `roost_result` | Wait for a run and return its verified outcome `{state, verified, evidence, output}`. |
| `roost_capabilities` | What the fleet can do (nodes, cores, GPUs) in plain language. |
| `roost_submit` | Submit a precise sub-job (`kind` claude/codex/docker, `requires`, `container`, `budget`). |
| `roost_status` / `roost_wait` / `roost_logs` | One job's state + liveness facts / block to terminal / its logs. |
| `roost_cancel` | Cancel a job (`tree: true` for its descendants). |
| `roost_workers` | List workers and a capability summary. |
| `roost_exec` | Run a shell command on **one** named worker — no SSH. |

Then just talk: *"run the tests on a GPU box"*, *"what's running?"*, *"why did that
fail?"*.

---

## Claude app (claude.ai / desktop) — same server, reachable URL

The desktop and web apps connect the **same** MCP server as a remote connector. Two
notes:

- Remote MCP needs a URL the app can actually reach — a loopback `127.0.0.1` control
  plane won't work. Bind the control plane to a **LAN or tailscale** address (and run it
  with an admin token; it refuses non-loopback without one).
- Add it as a custom connector pointing at your control plane host, authenticated with a
  scoped pair-token (below) so the app never holds the admin token.

The agent gets the same verb surface as Claude Code.

---

## Codex / anything that speaks HTTP — the raw API in 6 lines

Bearer-token auth; one POST to start a verified goal, then poll or stream. Shapes are
from [`roost/server.py`](../roost/server.py) (`POST /jobs` → `JobSubmit`).

```bash
H="Authorization: Bearer $ROOST_TOKEN"
ID=$(curl -s -H "$H" -H 'Content-Type: application/json' \
  -d '{"kind":"auto","task":"report the OS and free memory on a CPU box","verify":true}' \
  $ROOST_URL/jobs | jq -r .id)                       # start (kind: auto + task → verified)
curl -s -H "$H" $ROOST_URL/jobs/$ID | jq '{state,result,error}'   # poll once
curl -sN -H "$H" "$ROOST_URL/jobs/$ID/stream"        # …or stream: SSE state/log/done events
```

- `POST /jobs` body: `kind: "auto"` + `task` runs the verified front-door path; set
  `verify: false` to skip verification. Other kinds (`command`, `claude`, `codex`,
  `docker`) take `command`/`intent`/`image` + optional `requires`, `container`, `budget`,
  `target`. Returns the job record (`{id, state, …}`).
- `GET /jobs/{id}` returns the job with liveness facts (`last_activity`, `idle_sec`,
  `capable_workers`) and, when terminal, `result`/`error`/`tokens_used`.
- `GET /jobs/{id}/stream` is Server-Sent Events: `event: state`, `event: log`, and a final
  `event: done` carrying `{state, exit_code, error, result, tokens_used}`. Pass `?since=<seq>`
  to resume.

---

## CLI for scripts / cron — one-liners

```bash
roost do "summarize today's logs in /var/log and write the digest to /tmp/digest.md"
roost exec gpu-box -- nvidia-smi          # run a command on one named node, no SSH
roost submit spec.yaml --detach           # precise spec: exact requires/kind/container/budget
```

`roost do` is the front door (classify → route → run → verify); in non-interactive
contexts pass `--yes` to skip the confirm/clarify prompts. `roost submit` reads a
YAML/JSON spec (or `-` for stdin).

---

## Scoped tokens for non-admin front doors

Don't hand the admin token to a phone or a third-party app. Mint a **scoped** token:

```bash
roost pair --label "yang-iphone"          # mints a 'mobile'-scoped token + a scannable QR
roost pair --list                         # see active pairings + last use
roost pair --revoke <token-id>            # cut one off
```

The `mobile` scope can read fleet state and submit/cancel jobs, but can **never** mint
enroll tokens, touch workers, or fetch credentials. Use it as the `ROOST_TOKEN` for any
front door that isn't the operator's own admin shell.

---

## What your agent can do (the verb surface)

The verbs are the product — model-vendor-neutral by construction.

| Verb | How | Status |
|------|-----|--------|
| **run** | `roost_do` / `roost do` / `POST /jobs` | execution on hardware you own |
| **verify** | independent verifier on every `kind: auto` run (`verify: true`) | the trust loop — returns evidence, not just exit 0 |
| **transfer** | blob store: `POST /blobs`, `PUT/GET /blobs/{id}` | move files between front door and fleet |
| **observe** | `roost_runs` / `roost_status` / `GET /derived` / `/panel` | live state, health, cost, evidence |
| **schedule** | drive `roost do` / `POST /jobs` from cron or a scheduled agent | run work on an interval |
| **serve / publish** | `roost publish ./site` / `POST /publish` → `GET /pub/<slug>/` | static site live on your own CP |

---

## publish — a built thing → a real URL, in one command

The bottleneck for anyone who just *built* something (vibe coding) is publishing it.
Roost makes it one command, end to end:

```bash
roost publish ./my-site --name demo    # → live: http://<cp>/pub/demo/
roost publish --list                   # see published sites + URLs
roost publish --unpublish demo         # take one down (admin)
```

The CLI tars the directory, uploads it via the blob store (`POST /blobs`), and the
control plane extracts the bundle into `<data_dir>/sites/<slug>/`, live immediately at
`GET /pub/<slug>/`. Rebuild and re-run to republish — the same name atomically replaces
the site. Bundles are extracted with Python's `tarfile` `data` filter (no path escape)
and capped (256 MB / 5000 files).

Agents publish too — a scoped **client** token (`roost token --scope agent`) may
`POST /publish`; unpublishing stays admin-only. Served sites are **unauthenticated** —
public is the point — so don't put secrets in a bundle, and note that a LAN/Tailscale-
exposed CP exposes the site to that network. Sites live on disk + in the `sites` table,
so they survive control-plane restarts.
