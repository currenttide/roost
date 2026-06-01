#!/bin/bash
# Build + install RoostFleet.app on this Mac.
#   ./build.sh <control-plane-url> <admin-token>
# e.g. ./build.sh http://<control-plane-host>:8787 "<admin-token>"
# Produces /Applications/RoostFleet.app — double-click to get the floating fleet
# panel + a Claude console wired to the fleet.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CP_URL="${1:-http://127.0.0.1:8787}"
TOKEN="${2:-}"
APP="/Applications/RoostFleet.app"

echo "[build] control plane: $CP_URL"

# --- config ---
mkdir -p "$HOME/.roost"
cat > "$HOME/.roost/fleet.conf" <<CONF
ROOST_CP_URL="$CP_URL"
ROOST_ADMIN_TOKEN="$TOKEN"
CONF
chmod 600 "$HOME/.roost/fleet.conf"

# --- panel venv (pywebview for the floating window) ---
if [ ! -x "$HOME/.roost/panel-venv/bin/python" ]; then
  echo "[build] creating panel venv + installing pywebview…"
  if command -v uv >/dev/null 2>&1; then
    uv venv "$HOME/.roost/panel-venv" >/dev/null
    "$HOME/.roost/panel-venv/bin/python" -m ensurepip -q 2>/dev/null || true
    uv pip install --python "$HOME/.roost/panel-venv/bin/python" -q pywebview pyobjc-framework-WebKit
  else
    python3 -m venv "$HOME/.roost/panel-venv"
    "$HOME/.roost/panel-venv/bin/pip" install -q pywebview pyobjc-framework-WebKit
  fi
fi

# --- Claude console dir (so claude in the terminal knows the fleet) ---
mkdir -p "$HOME/roost-console"
cat > "$HOME/roost-console/CLAUDE.md" <<'MD'
# Roost fleet console
You drive a live fleet of remote machines through the `roost` CLI (already on PATH,
configured via ROOST_URL/ROOST_TOKEN in this shell). The floating panel beside this
terminal shows which node is doing what in real time.

- See the fleet: `roost workers`  ·  recent jobs: `roost jobs`
- Run work in plain language: `roost dispatch "<goal>"` — a captain agent splits it
  and places each piece on the best node (GPU work → GPU boxes; bulk → CPU boxes).
  It returns a merged result.
- Direct: `roost submit <spec.yaml>` (kinds: command / claude / docker GPU).
- Watch a run: `roost tree <root-id> --health` ; cancel: `roost cancel <id> [--tree]`.
Run `roost workers` to see what's in your fleet. Just tell me a goal and I'll dispatch it.
MD

# --- assemble the .app ---
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
cp "$HERE/panel_window.py" "$APP/Contents/Resources/panel_window.py"
cp "$HERE/launcher.sh" "$APP/Contents/MacOS/RoostFleet"
chmod +x "$APP/Contents/MacOS/RoostFleet"

echo "[build] installed $APP — open it from Launchpad/Applications (or: open '$APP')"
