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
