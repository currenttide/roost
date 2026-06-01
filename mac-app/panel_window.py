#!/usr/bin/env python3
"""Floating always-on-top window that shows the Roost fleet panel.

Usage: panel_window.py <panel-url>
Renders the control plane's /panel page (live "which node is doing what") in a
small native macOS window pinned above other windows. Falls back to opening the
URL in the default browser if pywebview isn't available.
"""
import sys

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787/panel"

try:
    import webview  # pywebview
except ImportError:
    import webbrowser
    webbrowser.open(URL)
    sys.exit(0)

webview.create_window(
    "Roost Fleet",
    URL,
    width=460,
    height=680,
    on_top=True,          # floating / always-on-top
    frameless=False,
    easy_drag=True,
    background_color="#0b0e14",
)
webview.start()
