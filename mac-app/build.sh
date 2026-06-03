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

# --- Claude console dir: connect the Roost MCP server so you just TALK to the fleet ---
mkdir -p "$HOME/roost-console"
cat > "$HOME/roost-console/.mcp.json" <<MCP
{ "mcpServers": { "roost": { "command": "roost", "args": ["mcp"],
  "env": { "ROOST_URL": "$CP_URL", "ROOST_TOKEN": "$TOKEN" } } } }
MCP
cat > "$HOME/roost-console/CLAUDE.md" <<'MD'
# Roost fleet console
You drive a live fleet of remote machines by talking. The `roost` MCP server is
connected — prefer its tools over raw CLI. The floating panel beside this terminal
shows which node is doing what in real time.

- **roost_do("<goal>")** — THE tool. Do anything the user asks in plain language; a
  worker self-selects the best node, runs it, and an independent verifier checks it was
  actually achieved. Returns a run_id.
- **roost_result(run_id)** — wait for the verified outcome + evidence; report THAT to the
  user (proof, not just "it ran").
- **roost_runs()** — the inbox: what's running / how it went / why it failed.
- **roost_capabilities()** — what this fleet can do.

Hold the thread across turns: "run the tests" → "now on a GPU box too" → "why did that
fail?". Just tell me a goal and I'll do it and show you the proof.
MD

# --- assemble the .app ---
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
cp "$HERE/panel_window.py" "$APP/Contents/Resources/panel_window.py"
cp "$HERE/launcher.sh" "$APP/Contents/MacOS/RoostFleet"
chmod +x "$APP/Contents/MacOS/RoostFleet"

echo "[build] installed $APP — open it from Launchpad/Applications (or: open '$APP')"
