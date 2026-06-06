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
