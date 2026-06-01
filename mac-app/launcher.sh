#!/bin/bash
# RoostFleet.app launcher — opens the floating fleet panel + a Claude console.
# Config (control-plane URL + token) is read from ~/.roost/fleet.conf.
RES_DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
CONF="$HOME/.roost/fleet.conf"
[ -f "$CONF" ] && source "$CONF"
CP_URL="${ROOST_CP_URL:-http://127.0.0.1:8787}"
TOKEN="${ROOST_ADMIN_TOKEN:-}"
PANEL_URL="$CP_URL/panel?token=$TOKEN"
VENV="$HOME/.roost/panel-venv"
CONSOLE="$HOME/roost-console"   # claude runs here so it has the fleet CLAUDE.md

# Make sure ~/.local/bin (uv tools: claude, roost) is reachable.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# 1) Floating always-on-top panel.
if [ -x "$VENV/bin/python" ]; then
  "$VENV/bin/python" "$RES_DIR/panel_window.py" "$PANEL_URL" >/dev/null 2>&1 &
else
  open "$PANEL_URL"
fi

# 2) Claude console in Terminal, with the roost env so it can drive the fleet.
/usr/bin/osascript <<OSA
tell application "Terminal"
  activate
  do script "cd '$CONSOLE' 2>/dev/null; export ROOST_URL='$CP_URL' ROOST_TOKEN='$TOKEN' PATH=\"\$HOME/.local/bin:\$PATH\"; clear; echo '  Roost Fleet console — Claude drives your fleet. e.g.  roost dispatch \"…\"  or just ask.'; claude"
end tell
OSA
