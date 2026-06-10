# Roost Mobile — API contract

The single source of truth for what the iOS and Android apps consume. Both apps
must build against **exactly** these shapes; `fixtures/` holds golden JSON recorded
from a live control plane (`python mobile-app/record_fixtures.py`) and both app test
suites decode every fixture. If the server changes a shape, regenerate fixtures and
the app-side decode tests pinpoint the drift.

Server: `roost/server.py`, version `0.2.0`. All endpoints the apps use are listed
here — the apps use **nothing else**.

## 1. Pairing & auth

Pairing payload (QR or pasted string), produced by `roost pair`:

```
roost://pair?d=<base64url(JSON, padding stripped)>
```

```json
{"v": 1, "url": "http://192.168.1.193:8787", "token": "rst-mob-…", "name": "yang-iphone"}
```

- `v` — payload version, currently `1`. Reject larger values with "update the app".
- `name` is optional. Restore base64 padding before decoding (`len % 4`).
- Store `url` + `token` in Keychain (iOS) / Keystore-encrypted prefs (Android).

Every request: `Authorization: Bearer <token>`. The token is **mobile-scoped**:

| Allowed | Denied (403) |
|---|---|
| All reads below, `POST /jobs`, `POST /jobs/{id}/input`, `DELETE /jobs/{id}`, `POST /blobs`, `POST /publish`, `GET /publish` (§6), `POST/GET/PATCH/DELETE /schedules` (§7) | enroll-token mint, pair-token mint/list/revoke, worker delete/prune/register, `/claude-creds`, worker lease plane, job finalize, `DELETE /publish/{slug}`, `DELETE /blobs/{id}` |

Scope note (pinned by `tests/test_publish.py::test_mobile_scope_publishes_end_to_end`):
`mobile` and `agent` scopes share ONE client permission set — the scope is an
audit label, not a privilege boundary. Publishing needs no special token.

Error envelope everywhere (FastAPI): `{"detail": "<message>"}` — fixtures
`error_401.json`, `error_403_admin_endpoint.json`, `error_404_job.json`.

- **401** → token revoked/invalid: drop to the pairing screen.
- **403** → scope bug in the app: show the error, don't unpair.

Reachability probe (unauthenticated): `GET /healthz` → `{"ok": true, "version": "0.2.0"}`.

## 2. Dashboard — `GET /derived?limit=40`

Fixture: `derived.json`. Poll every 2 s foregrounded, never in background.

```
{
  "generated_at": <epoch float>,        // staleness pill if > 10 s behind now
  "fleet_verdict": {"level": "ok"|"alert", "summary": "<one line>"},
  "workers": [ <worker> ],              // fixture workers.json for the row shape
  "runs": [ <run> ]                     // newest-ish first, one per job
}
```

Run row (fixture has every field; the app renders a subset):

```
{
  "run_id": "c7dedcc11a4c",            // == job id
  "goal": "fix the flaky auth test",   // display title (≤140 chars)
  "kind": "claude"|"command"|"docker"|"auto"|"codex"|"captain", // effective executor
                                       // kind (a `command` job reads "command", not
                                       // "claude"). Older CPs omit it → clients drop
                                       // the kind segment rather than guess.
  "goal_display": "fix the flaky auth test", // glanceable verdict-bar summary:
                                       // == goal for agent jobs; for a raw `command`
                                       // it collapses the shell text to its program/
                                       // verb. Additive — fall back to `goal` if absent.
  "state": "queued"|"assigned"|"running"|"succeeded"|"failed"|"cancelled",
  "phase": state ∪ "verifying"|"self-healing",
  "health": {"status": <below>, "reason": "<≤160 chars>"},
  "worker": "<worker id>"|null,
  "verified": true|false|null, "evidence": "<str>"|null,
  "result": "<≤240 chars>",            // terminal summary or error text
  "narration": "<str>"|null,           // best live one-liner; fall back to last_activity
  "progress": 0..100|null, "eta_sec": <int>|null,
  "cost": {"tokens_used": <int>, "cost_est_usd": <float>, "budget_pct"?: <float>},
  "inputs"?: {"queued": <int>, "delivered": <int>, "dropped": <int>}, // R38 follow-ups,
                                       // present ONLY when this run has received input (§4)
  "created_at": <epoch>, "finished_at": <epoch>|null,
  "queued_sec"|"idle_sec"|"capable_workers"|"decline_count"|"diagnosis"|
  "last_activity"|"root_job_id": informational
}
```

`health.status` (closed enum, map to UI):
`verified` ✓ · `done` ✓ · `unverified` ⚠ · `failed` ✗ · `cancelled` − ·
`running` ▶ · `verifying` ▶ · `self-healing` ▶ · `queued` ○ · `waiting` ◔ ·
`unplaceable` ⚠ · `stuck?` ⚠. Unknown value → render as plain text, don't crash.

Sort for display: running/assigned first, then by `created_at` desc.

Worker rows (`workers.json`): render `name`, `status`
(`idle`|`busy`|`stale`|`offline` — full vocabulary + row shape in §2a),
`last_seen`. Count `idle`+`busy` as live for the "N nodes" chip.

### 2a. Fleet — `GET /workers` (R121)

The Fleet screen reads the full worker list — the **same rows** `/derived`
embeds (one server function feeds both, so the two surfaces can't diverge).
Already inside the §1 client permission set — mobile and agent tokens read it
with no extra scope (pinned by `tests/test_tokens.py::test_mobile_token_unchanged`
and `::test_agent_token_can_observe_and_submit`). Fixture: `workers.json`
(a busy, an idle-with-GPU and an offline row).

Array of worker objects, newest-registered first. The apps render:

```
{
  "id": "a7bb28a6fcf1", "name": "fixture-node",
  "status": "idle"|"busy"|"stale"|"offline",  // see staleness note below
  "last_seen": <epoch float>,
  "capacity": <int>,            // concurrency slots (>= 1)
  "running": <int>,             // in-flight jobs — render "running/capacity"
  "capabilities": { … },        // free-form map: summarize known keys, ignore
                                // the rest. Common: hostname, os, arch, cpus,
                                // ram_gb, gpu_vram_gb, gpu_count, tools[],
                                // gpu_detection ("failed" = broken GPU probe
                                // on a node that HAS nvidia-smi — flag it,
                                // it's not a bare node), load {loadavg1, …}
  "registered_at"|"last_assigned_at"|"enroll_id"|"policy"|"revoked": informational
}
```

**Staleness (the R75 pattern).** The server recomputes `status` from
`last_seen` at read time: a heartbeat gap ≥ **45 s** reads `stale`, ≥ **120 s**
reads `offline`. Clients mirror those thresholds against their own
ticker-driven wall clock (exactly like the §2 staleness pill), so a node that
dies while a payload sits in hand degrades honestly on screen — idle/busy →
stale pill → offline pill — instead of staying green. The server's word always
wins in the offline direction (a `status: "offline"` row is offline however
fresh the payload); unknown `status` values render as plain text and never
count as up (§9).

## 3. Submit — `POST /jobs`

The app sends ONLY these fields (fixture: `job_submit_response.json` for the response):

```json
{
  "intent": "<the spoken/typed prompt>",
  "kind": "claude",                      // "command" if the user flips the toggle
  "requires": {},                        // {} = auto-place; {"worker": "<id>"} = pin
  "command": "<raw shell>",              // only when kind == "command", instead of intent
  "hierarchy": {"can_dispatch": true}    // agent jobs only — see below
}
```

`hierarchy.can_dispatch` is REQUIRED on `kind: "claude"` submits: it makes the
worker inject the roost MCP server, so the agent can see the fleet (workers,
runs, capabilities) and dispatch sub-jobs under the server's depth/tree-budget
guardrails. Without it the agent runs fleet-blind and answers fleet questions
with "I don't know". Never sent for `kind: "command"`.

Response = full job object; navigate to its `id` immediately. Job objects contain
`spec` (echo of the submit), `state`, timestamps, `worker_id`, `result`, `error`,
`exit_code`, `tokens_used` — see `job_detail_*.json` for all three states.

## 4. Session view

> **Job-id prefix lookup (additive, read paths only).** The READ routes here —
> `GET /jobs/{id}`, `…/derived`, `…/logs`, `…/tree`, `…/inputs`, `…/stream` —
> resolve `{id}` as an **unambiguous prefix** of ≥ 6 chars as well as the full
> 12-char id, so a copy-pasted short id resolves server-side. A **full id is itself
> an unambiguous prefix**, so apps that always send full ids are unaffected — this
> is purely additive. New error shapes (same `{"detail": …}` envelope as §1):
> **400** if the prefix is under 6 chars, **409** if it matches more than one job
> (the `detail` text lists the colliding ids). The **write** routes
> `DELETE /jobs/{id}` and `POST /jobs/{id}/input` deliberately do **not**
> prefix-resolve (a fuzzy match to the wrong running job is a footgun) — send the
> full id there; a prefix is a plain 404 (input) / 409 (cancel).

- Detail: `GET /jobs/{id}` (fixtures `job_detail_{queued,running,succeeded}.json`).
- One-line story for the header: `GET /jobs/{id}/derived` (fixture `job_derived_running.json`) — same run shape as §2.
- Catch-up: `GET /jobs/{id}/logs?since=<seq>&limit=1000` → fixture `job_logs.json`:
  `{"job_id", "state", "logs": [{"seq": <int>, "stream": "stdout"|"stderr"|"event", "data": "<line>", "ts": <epoch>}]}`.
  `since` is EXCLUSIVE (rows with `seq > since`). `job_logs_since_2.json` shows a resumed page.
- Children: `GET /jobs/{id}/tree` (fixture `job_tree.json`). Each node is a job
  object and carries the same optional `inputs: {queued, delivered, dropped}` as
  `GET /jobs/{id}` — present only on nodes that have received follow-up input.
- Cancel: `DELETE /jobs/{id}` → `{"cancelled": <n>}`; `?tree=true` cascades.
- Retry (client-side): re-`POST /jobs` with the failed job's `spec` fields from §3.
- Follow-up input (R38): `POST /jobs/{id}/input` with `{"text": "<message>"}` →
  `{"input_id", "job_id", "state": "queued"}`. A **terminal** job is rejected `409`;
  empty text `400`; over **64 KiB** `413`. Poll `GET /jobs/{id}/inputs` →
  `{"job_id", "state", "inputs": [{"id", "state": "queued"|"delivered"|"dropped",
  "detail", "created_at", "delivered_at", "created_by"}]}` for the outcome (the same
  counts ride `GET /jobs/{id}`, every `/derived` run row (§2), and each tree node
  as an optional `inputs: {queued, delivered, dropped}` object, present only when
  the job has received input). **Delivery is honest about
  kind:** only `command` jobs receive it live (written to the process stdin); agent
  (`claude`/`auto`/`codex`) and `docker` jobs run with stdin closed, so their input is
  marked `dropped` with a reason — show that, don't pretend it landed. (This is the
  minimal v2 steering slice; a finished-session "follow up" is still a new job with
  the parent's context per the mobile DESIGN.md §3.2 composer.)

### Log rendering

`stream: "stdout"|"stderr"` → show `data` as a monospaced line (strip ANSI codes).
`stream: "event"` → `data` is lifecycle JSON (`{"type": "started"|"succeeded"|…}`);
render as a subtle divider ("started", "succeeded"), or skip unparseable ones.

For agent jobs (`kind: claude`/`auto`), each `stdout` `data` line is one line of
Anthropic `stream-json`. The wire shape is **unchanged** — the server still relays
raw lines verbatim — but clients **distil this stream CLIENT-SIDE by default**
(assistant text, `→ tool` calls, `⎿` results, phase dividers; signatures/reasoning/
rate-limit noise suppressed) with a raw passthrough toggle. Distillation is purely a
client-render concern; the contract is in
[`fixtures/distilled/SPEC.md`](fixtures/distilled/SPEC.md) (CLI/iOS/Android mirror it).

### Log bounds (server-enforced; transcripts can cap)

- One log line ≤ **64 KiB**; the worker drops longer lines with an
  `oversized output line dropped…` event divider in their place.
- A job's stdout/stderr rows cap at **5000** mid-run (and a retention sweep
  prunes old rows after ~24 h) — a very chatty job's transcript may be
  partial. Lifecycle `event` rows are **exempt** from the row cap, so the
  terminal divider ("succeeded"/"failed") always lands. Don't treat a capped
  transcript as a stalled job — `state`/`health` stay authoritative.

## 5. Live stream — `GET /jobs/{id}/stream?since=<seq>`

`text/event-stream`; transcript fixture: `stream_succeeded.sse.txt`. Events:

```
event: state   data: {"state": "<job state>"}            // on every change, incl. first
event: log     data: <same row shape as /logs>           // one per line
event: done    data: {"state", "exit_code", "error", "result", "tokens_used"}
event: error   data: {"error": "job not found"}
```

After `done` the server closes the stream. Frames are `\n\n`-separated; `data:` is
a single line of JSON. Hand-rolled parser rules: split frames on blank line, take
`event:` and `data:` prefixes, ignore anything else (comments, retry hints).

**Resume protocol** (the core of pocket-survival):
1. Track the max `seq` seen across `/logs` and `log` events; persist per job id.
2. On foreground/reconnect: page `GET /logs?since=<last>` until caught up, then
   attach `GET /stream?since=<last>`.
3. Reconnect on drop with exponential backoff 1 s → 30 s, jittered; reset on success.
4. Duplicate suppression: drop any `log` with `seq <= last seen` (the boundary
   between catch-up page and stream attach can overlap).

## 6. Publish — built thing → live URL

A phone (or a phone-driven agent) ships a static site; the token from §1 is all
it needs. Two interchangeable flows, both yielding the same `Site` object — pick
by what you have in hand:

- **One-shot** (§6a) — POST the `tar.gz` *as the request body* in a single call.
  Preferred for the phone: nothing is staged, so a dropped connection can't leave
  a dangling blob.
- **Two-step** (§6b) — stage a blob, then publish it by `blob_id`. Kept for
  callers that already have a blob in flight.

Both honor `?name=` (required for one-shot, optional for two-step) and return
the **same** `Site` shape (`publish_response.json` / `publish_oneshot_response.json`
are byte-identical in structure; one slug differs). `GET /publish` (§6c) lists
sites for either flow.

### 6a. One-shot — `POST /publish?name=<slug>` (bundle is the body)

One transactional call. The `tar.gz` is the **raw request body**; set
`Content-Type: application/gzip` (or `application/octet-stream` — anything but
`application/json` selects this path). `name` is **required** here (there's no
blob name to default from) and is slugified (`^[a-z0-9][a-z0-9-]{0,39}$`).
Re-publishing an existing slug replaces the site atomically. Response = a `Site`
(§6b), fixture `publish_oneshot_response.json`:

```
{
  "slug": "phone-oneshot",
  "url": "http://<cp>/pub/phone-oneshot/",        // LAN URL, always present
  "public_url": "https://phone-oneshot.<domain>/",// ONLY when the CP has a publish domain
  "files": <int>, "size": <bytes>,
  "created_at": <epoch>, "updated_at": <epoch>
}
```

One-shot errors: **400** missing `name` / bad slug / empty body / body isn't a
valid `tar.gz` · **413** bundle over the size cap · **401** missing/invalid
token. (No staged blob exists, so the 404/409/410 blob states below don't apply.)

### 6b. Two-step — stage a blob, then publish

1. **Stage the bundle** — `POST /blobs?name=<site>.tar.gz`, raw `tar.gz` body
   (`Content-Type: application/octet-stream`). Fixture: `blob_upload_response.json`:

```
{
  "id": "d71603a73f9b", "name": "phone-site.tar.gz",
  "size": <bytes>, "sha256": "<hex>", "state": "ready",
  "created_at": <epoch>, "expires_at": <epoch>,   // staged blobs expire (~24 h default)
  "get_url": "<presigned download URL>"           // not needed for publishing
}
```

2. **Publish it** — `POST /publish` with `{"blob_id": "<id>", "name": "<site name>"?}`.
   `name` is optional: it defaults to the blob name minus `.tar.gz`/`.tgz`/`.tar`,
   then is slugified (`^[a-z0-9][a-z0-9-]{0,39}$`; 400 if it can't be).
   Re-publishing an existing slug replaces the site atomically.
   Fixture: `publish_response.json`:

```
{
  "slug": "phone-site",
  "url": "http://<cp>/pub/phone-site/",     // LAN URL, always present
  "public_url": "https://phone-site.<domain>/",  // ONLY when the CP has a publish domain
  "files": <int>, "size": <bytes>,
  "created_at": <epoch>, "updated_at": <epoch>
}
```

Two-step errors: 400 missing `blob_id`/bad name · 404 blob unknown or expired ·
409 blob upload unfinished · 410 blob file missing. The blob expires after
publish — the site lives on; don't surface blob TTL as site TTL.

### 6c. List sites — `GET /publish`

Array of the `Site` shape above (fixture `publish_list.json`), sorted by
`updated_at` desc — covers sites from either flow. `DELETE /publish/{slug}` is
**403** for app tokens (unpublish is admin-only; don't offer it in the app).

**Paginated (bounded):** the response is one page. Optional query params
`limit` (default 100, range 1–500; out-of-range is **422**) and `offset`
(default 0, ≥0) page through; the total count comes back in an additive
`X-Total-Count` response header (you've reached the end once
`offset + len(page) >= X-Total-Count`). The body shape is unchanged — a client
that ignores both params still gets a valid `Site` array (the first 100).

## 7. Schedules — interval jobs (`POST/GET/PATCH/DELETE /schedules`)

The `schedule` verb: the control plane re-submits a stored job spec on a fixed
interval (a phone front door scheduling recurring work is the point). A client
token from §1 manages schedules through the **same** client permission set as
publish — scope is an audit label, not a privilege boundary (pinned by
`tests/test_schedules.py::test_mobile_scope_manages_schedules_end_to_end`).
The worker plane may **not** (a job must not mint standing load): worker tokens
get **403**.

A `Schedule` object (fixtures `schedule_create_response.json`, `schedules_list.json`):

```
{
  "id": "a1b2c3d4e5f6",
  "name": "nightly-tidy"|null,
  "spec": { … },               // echo of the stored job spec (the §3 submit shape)
  "interval_sec": 1800,        // the parsed `every`, in seconds
  "enabled": true,
  "next_run_at": <epoch>,      // when the next job fires
  "last_run_at": <epoch>|null, // last real enqueue (null until the first fires)
  "last_job_id": "<job id>"|null,
  "created_at": <epoch>
}
```

### 7a. Create — `POST /schedules`

```json
{
  "spec": {"intent": "tidy the repo", "kind": "claude", "requires": {},
           "hierarchy": {"can_dispatch": true}},
  "every": "30m",              // seconds (number) or "<N>[smhd]" — "90s"/"30m"/"6h"/"1d"/"1.5h"
  "name": "nightly-tidy",      // optional label
  "enabled": true              // optional, default true
}
```

`spec` follows the §3 submit rules (must carry `intent`, or `command`, or
`kind: docker` + `image`, or `kind: auto` + `task`). The **first run fires one
interval from now** — never immediately. Returns the `Schedule` object.

- **400** — `every` unparseable, `every` below the **30 s floor**, a `spec` that
  fails the §3 shape rules, or a `spec` carrying `parent_job_id`/`captain_root`
  (schedules mint **root** jobs only).
- **403** — worker token. **401** — missing/invalid token.

### 7b. List — `GET /schedules`

Array of `Schedule`, newest-`created_at` first (fixture `schedules_list.json`).

### 7c. Enable / disable — `PATCH /schedules/{id}`

`{"enabled": true|false}` → the updated `Schedule`. **Re-enabling restarts the
clock**: `next_run_at` becomes one interval from now, so a long-disabled
schedule can't fire the instant it's re-enabled. **404** if the id is unknown.

### 7d. Delete — `DELETE /schedules/{id}`

→ `{"deleted": true, "id": "<id>"}`. **404** if the id is unknown.

### Tick semantics (server-enforced; the app just renders the clock)

The CP enqueues one job per due schedule per tick from the stored `spec` (each
run's spec carries a `schedule_id` for provenance). **No backfill:** an overdue
schedule (CP was down for several intervals) fires **once**, and `next_run_at`
advances in whole intervals so the original cadence is preserved — not a burst.
**No pile-up:** if the schedule's previous job is still in flight
(queued/assigned/running) the beat is skipped, but the clock still advances.

## 8. Golden fixtures

| File | What it pins |
|---|---|
| `pair_token_response.json` | `roost pair` mint response (the QR feeds off this) |
| `derived.json` | dashboard payload: alert verdict, running + queued + succeeded runs |
| `jobs_list.json` | `GET /jobs` |
| `job_submit_response.json` | freshly queued job object |
| `job_detail_succeeded/running/queued.json` | job object in each lifecycle stage |
| `job_logs.json`, `job_logs_since_2.json` | full + resumed log page |
| `job_tree.json` | lineage tree |
| `job_derived_running.json` | single-run story (session header) |
| `workers.json`, `healthz.json` | fleet list (busy + idle-GPU + offline rows, §2a), reachability probe |
| `stream_succeeded.sse.txt` | full SSE transcript incl. `event` log rows + `done` |
| `job_cancel_response.json` | cancel ack |
| `blob_upload_response.json` | staged bundle (`POST /blobs`) |
| `publish_response.json` | published site, two-step (`POST /publish` w/ `blob_id`) |
| `publish_oneshot_response.json` | published site, one-shot (`POST /publish?name=` + body) |
| `publish_list.json` | site list (`GET /publish`) |
| `schedule_create_response.json` | created interval schedule (`POST /schedules`) |
| `schedules_list.json` | schedule list (`GET /schedules`) |
| `error_401/403/404*.json` | error envelope |

## 9. Versioning

The contract is additive-only: servers may ADD fields; apps must ignore unknown
fields and unknown enum values (render, don't crash). Removing/renaming a field
here requires bumping the pairing payload `v`.
