#!/usr/bin/env bash
# Headless render harness driver (R120) — PNGs of the real app views, rendered
# off-screen via RenderShots.swift. Works on a Mac with NO display and NO
# Screen Recording/Automation TCC permissions (i.e. the fleet's headless Mac
# node, where `screencapture` fails) — see mac-app/README.md § Render evidence.
#
#   ./scripts/render_shots.sh [out-dir] [--stage]
#
#   out-dir   where PNGs + per-view logs land (default: build/render-shots)
#   --stage   after rendering, POST each PNG to the control plane as a blob
#             ("fleet clipboard") and print "blob <id> <name> <size>" lines —
#             the return path when running on a remote node via a roost job.
#
# Connection (the live `GET /derived` the views render from):
#   ROOST_RENDER_URL / ROOST_RENDER_TOKEN if set, else url + token/credential
#   from ~/.config/roost/config.toml (a worker credential is accepted).
#
# Each view renders in its own process (ROOST_RENDER_ONLY) under a watchdog,
# so one hung view (e.g. a Form layout assertion in headless hosting) cannot
# take down the rest. Exits nonzero unless at least 3 views rendered.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$(uname)" != "Darwin" ]]; then
    echo "error: the render harness needs macOS (AppKit)" >&2
    exit 1
fi

OUT="$PWD/build/render-shots"
STAGE=0
for arg in "$@"; do
    case "$arg" in
        --stage) STAGE=1 ;;
        -*) echo "unknown flag: $arg" >&2; exit 2 ;;
        *) OUT="$arg" ;;
    esac
done
mkdir -p "$OUT"

CONFIG="${HOME}/.config/roost/config.toml"
toml_get() {
    [[ -f "$CONFIG" ]] || return 0
    sed -n "s/^$1[[:space:]]*=[[:space:]]*\"\(.*\)\"[[:space:]]*$/\1/p" "$CONFIG" | head -1
}
URL="${ROOST_RENDER_URL:-$(toml_get url)}"
TOKEN="${ROOST_RENDER_TOKEN:-$(toml_get token)}"
[[ -n "$TOKEN" ]] || TOKEN="$(toml_get credential)"
if [[ -z "$URL" ]]; then
    echo "error: no control plane — set ROOST_RENDER_URL or have $CONFIG" >&2
    exit 1
fi
echo "==> control plane: $URL"

echo "==> swift build -c release"
swift build -c release 2>&1 | tail -2
BIN="$(swift build -c release --show-bin-path)/RoostMac"

VIEWS=(popover workspace fleet-transfers fleet-publish fleet-schedules
       schedule-create fleet-workers run-detail onboarding settings)
TIMEOUT="${ROOST_RENDER_TIMEOUT:-90}"

for name in "${VIEWS[@]}"; do
    echo "==> render $name"
    ROOST_RENDER_DIR="$OUT" ROOST_RENDER_ONLY="$name" \
    ROOST_RENDER_URL="$URL" ROOST_RENDER_TOKEN="$TOKEN" \
        "$BIN" >"$OUT/r_$name.log" 2>&1 &
    pid=$!
    finished=0
    for _ in $(seq 1 "$TIMEOUT"); do
        if ! kill -0 "$pid" 2>/dev/null; then finished=1; break; fi
        sleep 1
    done
    if [[ $finished -eq 0 ]]; then
        echo "    TIMEOUT after ${TIMEOUT}s — killed (see r_$name.log)"
        kill -9 "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
    tail -1 "$OUT/r_$name.log" | sed 's/^/    /'
done

shopt -s nullglob
PNGS=("$OUT"/*.png)
echo "==> ${#PNGS[@]} PNGs in $OUT"
ls -la "${PNGS[@]}" 2>/dev/null || true

if [[ $STAGE -eq 1 ]]; then
    echo "==> staging PNGs as blobs on $URL"
    for png in "${PNGS[@]}"; do
        name="r120-$(basename "$png")"
        resp="$(curl -sS -X POST "$URL/blobs?name=$name&ttl_sec=86400" \
            -H "Authorization: Bearer $TOKEN" \
            --data-binary @"$png")"
        id="$(printf '%s' "$resp" | sed -n 's/.*"id":[[:space:]]*"\([^"]*\)".*/\1/p')"
        size="$(stat -f%z "$png")"
        if [[ -n "$id" ]]; then
            echo "blob $id $name $size"
        else
            echo "stage FAILED for $name: $resp" >&2
        fi
    done
fi

if [[ ${#PNGS[@]} -lt 3 ]]; then
    echo "error: fewer than 3 views rendered — see $OUT/r_*.log" >&2
    exit 1
fi
echo "done"
