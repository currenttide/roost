# Deploying the control plane (hubbase)

The fleet control plane on **hubbase** (`192.168.1.193:8787`) runs **in Docker**, not
from a host install — so the dashboard (`/panel`) and `/derived` are served by the code
baked into the `roost-worker:latest` image, and editing files on the host has no effect
until the image is rebuilt.

## Source of truth

This git repo **is** the source of truth. As of this writing the deployed container's
`roost/server.py` and `roost/panel.html` are **byte-identical to `master`** (verified with
`diff`), so there is no hand-patch drift on hubbase — dashboard changes are made here and
deployed by rebuilding the image. (The stale, `/derived`-less wheel referenced in older
notes was the macOS console package, a separate artifact.)

## Where things live (container `docker-control-plane-1`)

| What | Path |
|------|------|
| Serve command | `roost serve --host 0.0.0.0 --port 8787 --db /data/roost.db --provision-auth` |
| Deployed package | `/usr/local/lib/python3.12/site-packages/roost` (in the image) |
| DB (host-mounted) | `/home/yang/roost-fleet/data/roost.db` → `/data/roost.db` |
| Compose | [`docker/stack.yml`](../docker/stack.yml) |

## Rebuild + redeploy (reproducible)

From a checkout of this repo on hubbase:

```bash
# 1. rebuild the image from the current source and recreate the CP container
export ROOST_TOKEN=$(cat /home/yang/roost-fleet/admin_token)
export ROOST_DATA_DIR=/home/yang/roost-fleet/data        # keeps the existing DB
docker compose -f docker/stack.yml up -d --build control-plane

# 2. confirm the new code is live
curl -s http://127.0.0.1:8787/healthz                     # {"ok":true,...}
```

The DB is a host-mounted volume, so it survives the rebuild — no data loss, workers stay
enrolled.

## Control-plane configuration reference

Everything the control plane reads from the environment, with its default and effect.
All are **optional** except `ROOST_TOKEN` (and even that only warns when unset). Each is
read once at `roost serve` startup, so change one → recreate the CP container (the
`docker compose … up -d` above) for it to take effect. The Docker deploy passes the
optional toggles through `docker/stack.yml` (export the matching variable before
`up`); `ROOST_DB` is wired via `--db` + the `ROOST_DATA_DIR` volume instead. For the
in-depth recipe behind a row, follow its link.

| Env var | Default | Effect when set |
|---------|---------|-----------------|
| `ROOST_TOKEN` | _(empty → no auth; logs a warning)_ | Shared admin bearer token. Also accepted via `--token`. See the auth notes in [`README.md`](../README.md). |
| `ROOST_DB` | `~/.roost/roost.db` | SQLite path for all fleet state. `--db` overrides it; the Docker deploy sets `--db /data/roost.db` and host-mounts it via `ROOST_DATA_DIR`. |
| `ROOST_PUBLISH_DOMAIN` | _(unset → LAN-only)_ | Public domain for published sites (e.g. `roost.pub`); turns on the host router + public-edge guard. `--publish-domain` overrides. → [Public publishing edge](#public-publishing-edge-roostpub) |
| `ROOST_NOTIFY_URL` | _(unset → no notifications)_ | ntfy.sh topic / UnifiedPush webhook the CP fire-and-forget POSTs on every terminal job. `--notify-url` overrides. → [Mobile push notifications](#mobile-push-notifications-opt-in) |
| `ROOST_PRICING` | _(unset → built-in single rate: `$0.018` base + `$6.00/Mtok`)_ | JSON object mapping model name/substring → `{base_usd, per_mtok_usd}`, layered over the default. Unset/blank/malformed → the built-in rate (no behavior change). → [Cost estimation pricing](#cost-estimation-pricing-per-model-optional) |
| `ROOST_NARRATE` | _(unset → off; only the literal `1` enables)_ | Turn on agentic per-job narration in the panel/`/derived` story. Adds one billed agent call per active job per narration tick. |
| `ROOST_NARRATE_INTERVAL` | `20` (seconds) | Minimum seconds between re-narrations of one active job. Clamped up to a `5`s floor (the sweep cadence); blank/garbage/NaN → `20`. No effect unless `ROOST_NARRATE=1`. |
| `ROOST_INSTALL_SOURCE` | `git+https://github.com/roost-sh/roost@main` | Package source baked into the worker installer that `GET /install.sh` serves. Override to install workers from your own fork/branch. A per-run `--source` on the installer wins over this. |

> **Admin-only endpoints** (require the `ROOST_TOKEN` bearer; a worker/scoped token gets
> `403`, no token `401`): `GET /admin/backup` streams a consistent online DB snapshot
> (see [Backup &amp; restore](#backup--restore-the-fleet-state)) and `GET /metrics`
> serves Prometheus text exposition (see the Metrics section in
> [`README.md`](../README.md#metrics-prometheus)).

## Verify deployed == repo (drift check)

```bash
C=$(docker ps -qf name=control-plane)
docker exec "$C" cat /usr/local/lib/python3.12/site-packages/roost/server.py \
  | diff - roost/server.py        # empty == in sync
docker exec "$C" cat /usr/local/lib/python3.12/site-packages/roost/panel.html \
  | diff - roost/panel.html
```

## Pruning ghost worker rows

Every re-enrolment leaves the old worker row behind; `/derived` accumulates them. Two
mechanisms:

- **Automatic** — the sweeper drops only stale *credential-less* orphan rows (it spares
  enrolled/revoked rows so a powered-off node can re-authenticate).
- **Manual** — `roost prune-workers --days N` (admin) deletes *any* row not seen in `N`
  days that owns no in-flight job — including enrolled/revoked duplicates. Use this to
  clear accumulated ghosts:

  ```bash
  roost prune-workers --days 1          # preview + confirm, then prune
  ```

  Backed by `POST /workers/prune?older_than_days=N`. A live node is never touched (its
  `last_seen` is recent); a recently-asleep node (e.g. a laptop that slept an hour ago)
  is kept and shown on the panel as **offline** rather than dropped.

## Backup &amp; restore (the fleet state)

The control plane's SQLite DB **is** the entire fleet — enrollments, job history,
schedules, tokens, blobs metadata. Back it up. A naive `cp roost.db backup.db` while the
CP is running is **unsafe**: under WAL the main file can be copied without its
uncheckpointed WAL frames, giving a torn/stale snapshot. Use `roost backup` instead — it
calls `GET /admin/backup` (admin-only), which takes a **consistent online snapshot** with
SQLite's backup API and streams it back as a single self-contained `.db` file (already
checkpointed — no separate `-wal`/`-shm` to ship). It works **without stopping the CP**
and **against a remote CP** (it's an HTTP download — no filesystem access to the server
needed).

### Back up

```bash
# From anywhere that can reach the CP, with the admin token resolved
# (flags → ROOST_URL/ROOST_TOKEN → ~/.config/roost/config.toml):
roost backup roost-$(date +%F).db
# → wrote <N> bytes to roost-2026-06-06.db
```

The snapshot is written atomically (a sibling `.part` file, renamed on success), so an
interrupted download never leaves a half-written file at the destination. Verify any
snapshot before trusting it:

```bash
sqlite3 roost-2026-06-06.db 'PRAGMA integrity_check;'   # → ok
sqlite3 roost-2026-06-06.db 'SELECT COUNT(*) FROM jobs;'
```

Schedule it with `cron` (the token comes from the config file the cron user owns) or as a
`schedule`d `command` job on a trusted node.

### Restore

Restore is a **stop → swap → start** of the control plane (a live CP holds the DB open;
swapping the file underneath it corrupts state). Roost's supervised service
(`roost service …`, `roost/service.py`) manages the **worker** unit only — the CP is run
directly, so restore steps depend on how *your* CP is launched:

**Docker (the hubbase deployment above):** the DB is the host-mounted file
`${ROOST_DATA_DIR}/roost.db` (→ `/data/roost.db` in `docker-control-plane-1`).

```bash
cd /path/to/repo                                  # where docker/stack.yml lives
export ROOST_DATA_DIR=/home/yang/roost-fleet/data # same value used to deploy
docker compose -f docker/stack.yml stop control-plane          # 1. stop CP
cp "$ROOST_DATA_DIR/roost.db" "$ROOST_DATA_DIR/roost.db.bak"   #    keep current state
rm -f "$ROOST_DATA_DIR/roost.db-wal" "$ROOST_DATA_DIR/roost.db-shm"  # drop stale WAL/SHM
cp /path/to/roost-2026-06-06.db "$ROOST_DATA_DIR/roost.db"     # 2. swap in the snapshot
docker compose -f docker/stack.yml start control-plane         # 3. start CP
```

**Host / systemd-run CP** (if you run `roost serve --db <path>` under your own unit rather
than Docker): same shape — `systemctl --user stop <your-cp-unit>` (or kill the process),
back up the current file, remove any `<path>-wal` / `<path>-shm`, copy the snapshot to
`<path>`, then start the unit again. The snapshot is a complete DB, so removing the old
WAL/SHM is safe and avoids the CP replaying stale frames over the restored file.

### Verify the restore

```bash
curl -s http://127.0.0.1:8787/healthz     # {"ok":true,...} — CP is serving
roost workers                              # the fleet you backed up is present
roost jobs --limit 5                       # recent job history is intact
```

Workers re-establish themselves by heartbeat; a node that was online at backup time shows
up (as **offline** until it next heartbeats, then **idle**/**busy**). If `roost workers`
is empty or `integrity_check` on the restored file wasn't `ok`, stop and restore the
`roost.db.bak` you set aside.

## Public publishing edge (roost.pub)

Published sites are world-reachable via a Cloudflare tunnel on hubbase
(separate from the root-owned daytether tunnel):

| What | Where |
|------|-------|
| Tunnel | `roost` (221099bb-…), user-owned |
| Config | `~/.cloudflared/config-roost.yml` (yang) |
| Service | `systemctl --user status cloudflared-roost` (linger on) |
| DNS | `roost.pub` + `*.roost.pub` → tunnel CNAMEs (Cloudflare zone) |

**The CP must be deployed with `ROOST_PUBLISH_DOMAIN=roost.pub`** (add it to the
deploy env alongside ROOST_TOKEN/ROOST_DATA_DIR) — that's what turns on the
host router and the public-edge guard (requests under roost.pub can only reach
site content, never the API). Forgetting it = published sites lose their
public URLs but nothing is exposed; the tunnel just 404s via the apex rule.

## Mobile push notifications (opt-in)

The CP can fire a notification on **every terminal job** (succeeded / failed /
cancelled) so the mobile apps (and any other receiver) learn a session finished
without polling. This is the v1.1 push feature from `mobile-app/DESIGN.md`. It is
**off by default** — set one env var (or `--notify-url`) to turn it on:

```bash
# ntfy.sh (hosted) — pick an unguessable topic and subscribe to it in the app
export ROOST_NOTIFY_URL=https://ntfy.sh/roost-7f3a91c2
# …or a self-hosted ntfy / any UnifiedPush-style webhook:
# export ROOST_NOTIFY_URL=https://ntfy.mybox.lan/roost
docker compose -f docker/stack.yml up -d --build control-plane
```

or `roost serve --notify-url https://ntfy.sh/<topic>`. CLI flag wins over env;
unset = **zero notifications, zero behavior change**.

**What's sent.** A single HTTP `POST` to that URL per terminal job, carrying a
JSON body — `{event, job_id, state, intent, duration_sec, exit_code, worker_id,
message}` — *and* ntfy's display headers (`Title`, `Priority`, `Tags`). That dual
shape is deliberate: the JSON parses cleanly for a generic UnifiedPush/webhook
receiver, while the headers make the **same** request render as a clean push in
the ntfy app (failures arrive at high priority). No APNs/FCM plumbing and **no
new dependencies** — it reuses the `httpx` the CP already ships.

**Failure isolation (by design).** The POST is **fire-and-forget**: it runs as a
detached task with a short timeout and is never awaited on the request path. A
500, a timeout, a refused connection, or DNS failure is **logged and dropped** —
it can never affect job state, the worker's report, or the request that triggered
it. There is **no retry** (a missed terminal push is recoverable by pulling
`/derived`); unbounded retries would be a worse failure mode than a dropped ping.

**Security note.** The URL is a capability — anyone who knows an ntfy topic can
read its messages. Use a long, random topic (as above), or self-host ntfy behind
your tunnel/LAN. Payloads contain the job's intent one-liner and state, not
credentials or logs.

> Subscribing the iOS/Android clients to the topic (UnifiedPush distributor on
> Android; an APNs bridge or ntfy's own app on iOS) is **client/device work
> tracked separately** — it isn't exercisable on the Linux test harness, so it
> is out of scope for this server-side change. The CP side (config + the POST on
> terminal states) is complete and tested here.

## Cost estimation pricing (per-model, optional)

Every run carries a rough `$` estimate (shown in the panel, `roost history`, and
the mobile apps). The control plane computes it from a job's fresh-token count as
`base_usd + tokens/1e6 × per_mtok_usd`. By default a single rate applies to all
models (a per-session floor of **$0.018** plus **$6.00/Mtok**) — the figures
that ship today.

To price models differently (e.g. a cheaper rate for Haiku jobs, a higher one for
Opus), set **`ROOST_PRICING`** on the CP to a JSON object mapping a model **name or
substring** → `{base_usd, per_mtok_usd}`:

```bash
export ROOST_PRICING='{
  "default": {"base_usd": 0.018, "per_mtok_usd": 6.0},
  "haiku":   {"base_usd": 0.005, "per_mtok_usd": 1.5},
  "opus":    {"base_usd": 0.030, "per_mtok_usd": 30.0}
}'
docker compose -f docker/stack.yml up -d --build control-plane
```

- **Matching:** a job's model is matched first by exact name, then by the *longest*
  key that is a substring of it — so `"haiku"` matches `claude-haiku-4-5`, while
  `"opus"` matches `claude-opus-4-1`. The job's model comes from its spec (`model`).
- **Fallback:** any model that matches no key (and any job with no model) uses the
  **`default`** entry. Omit `default` and the built-in rate above is used.
- **Partial entries** inherit the default's missing field, so you can override just
  `per_mtok_usd` and keep the standard floor.
- **Unset / malformed `ROOST_PRICING` = zero behavior change** — a missing var,
  bad JSON, or a non-object falls back to the built-in single rate (today's exact
  numbers); a bad pricing config never breaks the CP or zeroes out estimates.

The estimate is intentionally approximate: `tokens_used` counts only fresh
input+output, not the cached system-prompt reads that dominate an agent session's
bill, so a floor + small marginal tracks reality better than a flat per-token rate.
Tune the rates to your account's actual pricing rather than expecting cent accuracy.
