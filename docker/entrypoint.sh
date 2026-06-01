#!/usr/bin/env bash
# Enroll once (persisted via the mounted config volume), then run the worker.
# Auth for `claude` jobs is provisioned by the control plane at enroll time
# (server started with --provision-auth), so no host credential mount is needed.
set -euo pipefail

: "${ROOST_URL:?set ROOST_URL (e.g. http://host.docker.internal:8787)}"
: "${ROOST_WORKER_NAME:=docker-$(hostname)}"

CFG="$HOME/.config/roost/config.toml"

if [ ! -f "$CFG" ]; then
    if [ -z "${ROOST_ENROLL_TOKEN:-}" ]; then
        echo "[entrypoint] no config and no ROOST_ENROLL_TOKEN — cannot enroll." >&2
        echo "[entrypoint] mint one on the control plane: roost enroll-token --label $ROOST_WORKER_NAME" >&2
        exit 1
    fi
    echo "[entrypoint] enrolling '$ROOST_WORKER_NAME' at $ROOST_URL ..."
    if ! roost enroll "$ROOST_ENROLL_TOKEN" --url "$ROOST_URL" --name "$ROOST_WORKER_NAME"; then
        # Enroll tokens are single-use. If we land here with no config, the token
        # was likely already consumed (e.g. a crash between enroll and config
        # save). Tight-looping on `restart: unless-stopped` is useless — pause
        # with a clear message so the operator can supply a fresh token.
        echo "[entrypoint] enroll FAILED and no config exists. The token may be" >&2
        echo "[entrypoint] spent — mint a fresh one and recreate with a new" >&2
        echo "[entrypoint] ROOST_ENROLL_TOKEN. Sleeping to avoid a restart loop." >&2
        sleep 3600
        exit 1
    fi
else
    echo "[entrypoint] already enrolled (config present); skipping enroll."
fi

# Headless `claude -p` silently no-ops until onboarding is marked complete in
# ~/.claude.json. That file is NOT under the persisted .claude/ volume, so set it
# idempotently on EVERY start — otherwise agent jobs quietly stop working after
# an image rebuild / container recreate.
python3 - <<'PY' || true
import json, os
p = os.path.expanduser("~/.claude.json")
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
except (OSError, json.JSONDecodeError):
    d = {}
if not d.get("hasCompletedOnboarding"):
    d["hasCompletedOnboarding"] = True
    json.dump(d, open(p, "w"))
    print("[entrypoint] marked Claude onboarding complete")
PY

echo "[entrypoint] starting worker."
exec roost worker
