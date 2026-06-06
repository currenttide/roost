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
| All reads below, `POST /jobs`, `DELETE /jobs/{id}`, `POST /blobs`, `POST /publish`, `GET /publish` (§6) | enroll-token mint, pair-token mint/list/revoke, worker delete/prune/register, `/claude-creds`, worker lease plane, job finalize, `DELETE /publish/{slug}`, `DELETE /blobs/{id}` |

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
  "state": "queued"|"assigned"|"running"|"succeeded"|"failed"|"cancelled",
  "phase": state ∪ "verifying"|"self-healing",
  "health": {"status": <below>, "reason": "<≤160 chars>"},
  "worker": "<worker id>"|null,
  "verified": true|false|null, "evidence": "<str>"|null,
  "result": "<≤240 chars>",            // terminal summary or error text
  "narration": "<str>"|null,           // best live one-liner; fall back to last_activity
  "progress": 0..100|null, "eta_sec": <int>|null,
  "cost": {"tokens_used": <int>, "cost_est_usd": <float>, "budget_pct"?: <float>},
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

Worker rows (`workers.json`): render `name`, `status` (`idle`|`busy`|`offline`),
`last_seen`. Count `idle`+`busy` as live for the "N nodes" chip.

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

- Detail: `GET /jobs/{id}` (fixtures `job_detail_{queued,running,succeeded}.json`).
- One-line story for the header: `GET /jobs/{id}/derived` (fixture `job_derived_running.json`) — same run shape as §2.
- Catch-up: `GET /jobs/{id}/logs?since=<seq>&limit=1000` → fixture `job_logs.json`:
  `{"job_id", "state", "logs": [{"seq": <int>, "stream": "stdout"|"stderr"|"event", "data": "<line>", "ts": <epoch>}]}`.
  `since` is EXCLUSIVE (rows with `seq > since`). `job_logs_since_2.json` shows a resumed page.
- Children: `GET /jobs/{id}/tree` (fixture `job_tree.json`).
- Cancel: `DELETE /jobs/{id}` → `{"cancelled": <n>}`; `?tree=true` cascades.
- Retry (client-side): re-`POST /jobs` with the failed job's `spec` fields from §3.

### Log rendering

`stream: "stdout"|"stderr"` → show `data` as a monospaced line (strip ANSI codes).
`stream: "event"` → `data` is lifecycle JSON (`{"type": "started"|"succeeded"|…}`);
render as a subtle divider ("started", "succeeded"), or skip unparseable ones.

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

A phone (or a phone-driven agent) ships a static site in two calls; the token
from §1 is all it needs.

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

3. **List sites** — `GET /publish` → array of the same site shape
   (fixture `publish_list.json`). Sorted by `updated_at` desc.

Errors: 400 missing `blob_id`/bad name · 404 blob unknown or expired ·
409 blob upload unfinished · 410 blob file missing · 403 on
`DELETE /publish/{slug}` (unpublish is admin-only; don't offer it in the app).
The blob expires after publish — the site lives on; don't surface blob TTL
as site TTL.

## 7. Golden fixtures

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
| `workers.json`, `healthz.json` | fleet list, reachability probe |
| `stream_succeeded.sse.txt` | full SSE transcript incl. `event` log rows + `done` |
| `job_cancel_response.json` | cancel ack |
| `blob_upload_response.json` | staged bundle (`POST /blobs`) |
| `publish_response.json` | published site (`POST /publish`) |
| `publish_list.json` | site list (`GET /publish`) |
| `error_401/403/404*.json` | error envelope |

## 8. Versioning

The contract is additive-only: servers may ADD fields; apps must ignore unknown
fields and unknown enum values (render, don't crash). Removing/renaming a field
here requires bumping the pairing payload `v`.
