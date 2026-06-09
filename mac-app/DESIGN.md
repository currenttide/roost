# Roost for Mac — Design Document

**Status:** Draft v1 · 2026-06-05
**Audience:** anyone building or reviewing the macOS app
**Replaces:** the current `mac-app/` proof of concept (pywebview panel + Terminal launcher)

---

## 1. Vision

> **Your fleet in the menu bar. Say what you want, watch it happen, get told when it's done.**

Roost's whole pitch is "state a goal in plain language and trust the result." The Mac app
is the everyday surface for that promise: a glanceable fleet indicator that lives in the
menu bar, a goal box one keystroke away, and run cards that show — without any terminal —
whether work is queued, running, verifying, or done, with the verifier's evidence attached.

It is **not** a replacement for the CLI. Power flows (enrollment, service management,
captain budgets, job specs) stay in `roost …`. The app covers the 95% daily loop:

1. *Is my fleet OK?* — one glance at the menu bar icon.
2. *Do this for me.* — type a goal, hit ⏎.
3. *How's it going?* — live phase, narration, progress, logs.
4. *Did it actually work?* — verified badge + evidence, and a notification when it lands.

### Design principles

- **Lightweight is a feature.** Native SwiftUI, zero third-party dependencies, no bundled
  runtime, no Electron, no Python. Target: < 10 MB app bundle, < 60 MB resident, ~0% CPU
  when the popover is closed.
- **Easy means fewer concepts, not more chrome.** The app exposes *goals* and *runs*, not
  job specs, leases, or placement scores. Job kinds, `requires:` filters, and budgets are
  progressive disclosure — visible only when expanded.
- **The app is mechanical; judgment stays in agents.** (Project principle.) The app renders
  what the control plane already derives (`/derived` fleet verdict, run health, narration)
  and submits goals. It does not re-implement triage, health heuristics, or planning.
- **Read-mostly, write-carefully.** Reads poll freely. Writes are: submit a goal, cancel a
  run. Both are explicit user actions. Nothing destructive happens implicitly.

### Non-goals (v1)

- Enrolling new workers from the app (the `roost-onboard` skill + CLI own this; see §10).
- Editing raw job specs / YAML. Use `roost submit`.
- Bundling or supervising the control plane / worker processes. The app is a *client*.
- An iOS/iPadOS port (the API-client layer should keep this possible, but it is not designed for here).
- Multi-fleet management UI (single active control plane per app instance; switching is a settings action).

---

## 2. Form factor

**A menu bar app (`MenuBarExtra`) with an optional full window.** No Dock icon by default
(`LSUIElement`), with a setting to show one.

Why menu-bar-first:

- Fleet state is *ambient* information — the right UX is a status item you glance at, not
  a window you manage. This is the Mac-native equivalent of the web `/panel`.
- The submit box being one click / one global hotkey away is what makes the app the path
  of least resistance versus opening a terminal.
- It keeps the lightweight promise structurally: a popover scales the app *down*; a
  document-style app would pressure us to fill space with dashboards.

The **main window** (opened from the popover or `⌘O`) is the same content with more room:
full run history with filters, a wider log viewer, worker detail. One codebase of views,
two presentations.

### 2.1 Menu bar icon — the fleet verdict

The icon *is* the smallest unit of UI and maps 1:1 to `GET /derived → fleet_verdict`:

| State | Icon | Source |
|---|---|---|
| All good, idle | bird glyph, template (monochrome) | `fleet_verdict.level == "ok"`, no active runs |
| Working | bird + subtle activity dot | any run in `running` / `verifying` |
| Alert | bird + orange badge | `fleet_verdict.level == "alert"` |
| Unreachable | bird, 40% opacity, slash | control plane not responding |

No red/green dots competing for attention; one glyph, three modifiers.

### 2.2 The popover

```
┌──────────────────────────────────────────────┐
│  🐦 Roost            hubbase · 4 workers  ⚙︎ │
│  ── fleet: OK ─────────────────────────────  │
│                                              │
│  ┌────────────────────────────────────────┐  │
│  │  Tell your fleet what to do…        ⏎  │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  ACTIVE                                      │
│  ◉ retrain embeddings on the GPU box         │
│     running · dgx-1 · 12m · ▓▓▓▓▓░░░ 61%     │
│     "fine-tuning epoch 3/5, loss 0.041"      │
│  ◉ lint the roost repo                       │
│     verifying · pi-4 · 2m                    │
│                                              │
│  RECENT                                      │
│  ✓ report free VRAM on a GPU box   ·  4m ago │
│     verified ✓ · 3.1k tok · $0.04            │
│  ✗ build docs on windows box       · 31m ago │
│     "npm not installed on win-wsl"           │
│                                              │
│  WORKERS                                     │
│  ● dgx-1   busy 1/2   gpu A100 · 79 GB free  │
│  ● pi-4    idle       arm64 · 4 cpu          │
│  ● macmini idle       darwin · claude ✓      │
│  ◌ win-wsl offline    last seen 2d ago       │
│                                              │
│  Open Roost ⌘O          History · Workers    │
└──────────────────────────────────────────────┘
```

Anatomy, top to bottom:

1. **Header** — app name, control plane name/host, worker count, settings gear.
2. **Goal box** — a single text field. ⏎ submits (§4). Focused automatically when the
   popover opens via the global hotkey.
3. **Active runs** — every run in a non-terminal phase, newest first. Each card: goal,
   phase, worker, elapsed, progress bar + narration *iff the backend provides them*
   (`progress`, `narration`, `eta_sec` from `/derived` — render nothing when absent,
   never invent).
4. **Recent runs** — last N terminal runs: verdict glyph (✓ verified / ✓ succeeded /
   ✗ failed / ⊘ cancelled), cost line (`tokens_used`, `cost_est_usd`), and for failures
   the worker-supplied `diagnosis` — the single most useful string we have.
5. **Workers** — one line each: status dot (`idle`/`busy n/cap`/`stale`/`offline`), name,
   headline capabilities (GPU > claude > cpus, pick two).
6. **Footer** — open main window, jump links.

Clicking any run card opens **Run Detail**.

### 2.3 Run detail

The drill-down view, shown as a popover push (back button) or a window pane:

```
┌──────────────────────────────────────────────┐
│ ←  retrain embeddings on the GPU box         │
│    running on dgx-1 · attempt 1 · 12m        │
│    ▓▓▓▓▓▓░░░░ 61% · eta ~8m                  │
│                                              │
│  queued → assigned → running → verifying →   │
│  ────────────────────●                       │
│                                              │
│  "fine-tuning epoch 3/5, loss 0.041"         │
│                                              │
│  LOGS                              follow ⏸  │
│  ┌────────────────────────────────────────┐  │
│  │ [12:01:33] epoch 3 step 1200 loss .041 │  │
│  │ [12:01:35] epoch 3 step 1250 loss .040 │  │
│  │ …                                      │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  3.1k tokens · ~$0.04 · budget 12% used      │
│                                              │
│            [ Cancel Run… ]   [ Copy job id ] │
└──────────────────────────────────────────────┘
```

- **Phase rail** mirrors the derived `phase` (`queued · running · verifying ·
  self-healing · succeeded/failed/cancelled`) — the trust loop made visible.
- **Logs** stream over the existing SSE endpoint (§5). Follow-mode autoscrolls; any
  manual scroll pauses it. `stderr` tinted, `event` lines collapsed behind a disclosure.
- **Terminal states** replace the log header with the outcome block:
  - succeeded + verified → "✓ Verified" with the verifier's `evidence` text, then `result.output`.
  - succeeded, unverified → "✓ Succeeded (not verified)".
  - failed → "✗ Failed" + `diagnosis` (prominent) + `error`, with a **Retry** button that
    resubmits the same goal (a *new* run — the app never mutates a finished job).
- **Cancel** confirms once, then `DELETE /jobs/{id}?tree=true` for runs with children
  (offer "this run only" vs "run + sub-jobs" only when `tree` is non-trivial).
- A **tree disclosure** appears for captain runs: indented child rows from
  `GET /jobs/{id}/tree`, each row a mini run card, tap to drill in.

### 2.4 Main window

Three sidebar sections, same view components as the popover, more room:

- **Runs** — full history (`/jobs` paged), filter by state/text, sortable. Selecting a
  run shows Run Detail in the right pane with a full-height log viewer.
- **Workers** — table: name, status, capabilities chips, running/capacity, last seen,
  load. Right pane: raw capabilities (pretty-printed), policy, recent runs on that
  worker. Admin action (token permitting): *Prune ghost workers…* (`POST /workers/prune`).
- **Activity** *(post-v1, §10)* — fleet-level timeline.

---

## 3. Architecture

```
┌───────────────────────── RoostApp (SwiftUI) ─────────────────────────┐
│                                                                      │
│  MenuBarExtra ── PopoverView ─┐                                      │
│  WindowGroup ─── MainWindow ──┼──▶  FleetStore (@Observable)         │
│  Notifications ◀──────────────┘        │        │                    │
│                                        ▼        ▼                    │
│                                  RoostClient   StreamHub             │
│                                  (REST, async) (SSE per open run)    │
└───────────────────────────────────│──────────────│──────────────────┘
                                    ▼              ▼
                         control plane  GET /derived · POST /jobs ·
                                        GET /jobs/{id}/stream (SSE) · …
```

| Layer | Responsibility | Notes |
|---|---|---|
| `RoostClient` | Typed async wrapper over the HTTP API | `URLSession` only. One method per endpoint. `Codable` models tolerant of unknown fields (the backend evolves faster than the app). |
| `StreamHub` | SSE connections for runs being watched | `URLSession.bytes(for:)` line parser — SSE is trivial, **no dependency**. One stream per open run detail, torn down on dismiss. Reconnects with `?since=<last seq>` (the API supports resume). |
| `FleetStore` | Single source of truth: fleet verdict, workers, runs | Fed by a poll loop on `/derived` (§5). Diffs against previous snapshot to drive notifications. `@Observable`, main-actor. |
| Views | Pure render of `FleetStore` | No view talks to the network. Popover and window share components. |

**Stack:** Swift 5.10+, SwiftUI, macOS 14+ (Sonoma) minimum — gets us `MenuBarExtra`,
`@Observable`, and modern `URLSession` without back-compat shims. **No third-party
packages**, with exactly one carve-out: **SwiftTerm** (MIT, pure Swift, no transitive
dependencies) for the Console's terminal emulator (§13) — a faithful xterm emulator is
the one component that is not honestly hand-rollable, and a half-faithful one would make
Claude Code's TUI unusable, which is worse than the rule it bends. Everything else stays
dependency-free; the entire app is one SwiftPM/Xcode target plus a small `RoostKit`
library target (client + models) kept UI-free so it stays portable (CLI tests, future iOS).

Why native instead of evolving the pywebview PoC or wrapping `/panel` in a WKWebView:

- *Lightweight*: the PoC drags a Python venv + pyobjc (~200 MB installed); a WKWebView
  wrapper is better but still a browser per window and can't do a proper status item,
  notifications, Keychain, or offline states. Native is the only option that genuinely
  honors "lightweight."
- *Easy to use*: menu bar + notifications + global hotkey + Keychain are exactly the
  affordances web content can't reach.
- The web `/panel` stays — it serves Linux/Windows operators. The Mac app supersedes only
  the `mac-app/` launcher scripts.

---

## 4. Submitting work — the goal box

The single most important interaction. Behavior mirrors `roost do`'s spirit while keeping
judgment out of the app:

1. User types a goal, hits ⏎.
2. App `POST /jobs` with:
   ```json
   {
     "task": "<goal text>",
     "intent": "<goal text>",
     "kind": "auto",
     "verify": true
   }
   ```
   `kind: auto` + bare-worker triage means *the fleet* decides who's capable — the app
   does not pick a node or classify the task.
3. The run card appears in **Active** immediately (optimistic insert from the POST
   response, reconciled on next `/derived` poll).

**Progressive disclosure** — an "options" chevron on the goal box expands, collapsed by
default and *remembering nothing* between submissions (defaults are the contract):

| Option | Default | Maps to |
|---|---|---|
| Multi-step plan (captain) | off | `kind: "captain"` via `roost dispatch` semantics |
| Verify result | **on** | `verify` |
| Prefer worker | (auto) | `prefer: {"worker": id}` — picker fed from `/workers` |
| Model | (fleet default) | `model` |
| Token budget | (none) | `budget.max_tokens` |

**What the app deliberately does not do:** `roost do`'s destructive-goal classification
currently lives in the CLI (client-side judgment). Re-implementing it in Swift would fork
that logic. v1 ships without it and submits `kind: auto` directly; the right fix is a
server-side `POST /do` (or a `classify` flag on `POST /jobs`) so CLI and app share one
implementation — tracked as the only backend ask in §9.

**Global hotkey** (default ⌥⌘R, configurable): opens the popover with the goal box
focused. Goal → ⏎ → done, without touching the mouse.

---

## 5. Data flow: polling & streaming

The backend offers polling + SSE (no websockets). The app uses three cadences:

| Loop | Endpoint | Cadence | When |
|---|---|---|---|
| Snapshot | `GET /derived?limit=40` | every **2 s** | popover or window visible |
| Snapshot (ambient) | same | every **20 s** | UI closed, ≥1 active run *or* last verdict ≠ ok |
| Snapshot (idle) | same | every **60 s** | UI closed, fleet quiet |
| Run logs | `GET /jobs/{id}/stream` (SSE) | server-pushed | a run detail is open |
| Reachability | `GET /healthz` | backoff 5 s → 60 s | snapshot loop is failing |

Notes:

- `/derived` is **one request** that powers the icon, popover, and notifications — by
  design we add no N+1 calls. Worker rows come from the same payload.
- Cadence shifts are driven by `NSApplication` occlusion + run-state, so a closed, quiet
  app costs one tiny GET per minute. On `NSWorkspace` sleep/wake notifications the loop
  suspends/resumes immediately (no wake-from-sleep error spam).
- SSE streams exist only while a detail view is open. Closing it cancels the task. The
  `done` event finalizes the view without waiting for the next snapshot.
- All polling is jittered ±10% to avoid sync storms when several Macs watch one CP.

### Notifications (`UNUserNotificationCenter`)

Driven by snapshot diffs in `FleetStore`, so they work with the UI closed:

| Event | Notification | Default |
|---|---|---|
| Run reaches terminal state | "✓ Verified: *goal*" / "✗ Failed: *goal* — *diagnosis*" | on |
| Fleet verdict flips ok → alert | "Fleet alert: *summary*" | on |
| Worker goes offline | "Worker *name* went offline" | off (noisy on flaky Wi-Fi) |
| Run stuck (`health.status` from backend) | "Run may be stuck: *goal*" | on |

Clicking a notification deep-links to that run's detail. Only backend-derived health is
surfaced — the app never invents its own "stuck" heuristics (mechanical client).

---

## 6. Connection, auth & onboarding

### First run

```
┌────────────────────────────────────────────┐
│            Welcome to Roost                │
│                                            │
│  ◉ Use this Mac's Roost config             │
│     found ~/.config/roost/config.toml      │
│     → http://hubbase:8787 · worker "mini"  │
│                                            │
│  ○ Connect to a control plane              │
│     URL  [ http://…:8787        ]          │
│     Token [ ••••••••••          ]          │
│                                            │
│  ○ I don't have a fleet yet                │
│     → guides to `roost up` / quickstart    │
│                                            │
│                              [ Connect ]   │
└────────────────────────────────────────────┘
```

- **Auto-detect**: if `~/.config/roost/config.toml` exists, offer its `url` (and token if
  the CLI stored one) as the pre-selected option. One click to a working app for anyone
  who already uses the CLI. (Requires no sandbox — see Distribution.)
- **Manual**: URL + token, validated live against `GET /healthz` then an authed
  `GET /derived` (catches wrong-token before "Connect" completes). Clear inline errors:
  unreachable vs unauthorized vs not-a-roost-server.
- **No fleet yet**: don't fake it — show the two-line `uv tool install` + `roost up`
  snippet with a copy button and a "recheck" button. The app must never install software.

### Token handling

- Token stored in **Keychain** (`kSecClassGenericPassword`, service `com.roost.mac`),
  never in defaults/files. The config-toml import *copies* the token to Keychain; the app
  does not write to `~/.config/roost/`.
- Plain-HTTP control planes are common on LANs/Tailscale: allowed, but settings shows a
  "token sent over HTTP — fine on a trusted network, use HTTPS otherwise" note rather
  than blocking. (`NSAllowsLocalNetworking` exception in ATS.)
- Per CLAUDE.md: the app never touches Claude credentials. Worker auth provisioning is
  CLI/skill territory.

### Degraded states

| State | UI |
|---|---|
| CP unreachable | dimmed icon; popover keeps last snapshot with "last updated 3m ago" banner + retry; goal box disabled with reason |
| Unauthorized (token revoked/rotated) | icon badge; popover swaps to a reconnect prompt |
| Admin-only action w/ worker token | buttons hidden, not broken (probe: 403 on first use → remember) |

---

## 7. Visual & interaction language

- **System-native everywhere**: SF Symbols, system colors (full dark mode for free),
  `.regularMaterial` popover background. The app should feel like Apple shipped it.
- **Status colors**, used only on the small dot/badge — never to fill the UI:
  green = idle/verified · blue (animated) = running · purple = verifying ·
  orange = stale/waiting/alert · red = failed · gray = offline/cancelled.
- **Type**: system font; goal text `.body`, metadata `.caption` secondary; logs
  `SF Mono .caption` (11 pt).
- **Motion**: progress bars animate; phase-rail transitions slide; nothing else moves.
- **Density**: popover ≈ 360 × 560 pt, content-capped (3 active + 4 recent + 6 workers,
  "show all →" overflows into the main window).
- Full keyboard path: hotkey → type → ⏎; `⌘O` window, `⌘L` jump to logs, `esc` closes.
- Accessibility: every status dot has a text label for VoiceOver; respects
  Reduce Motion / Increase Contrast.

---

## 8. Lightweight budget (acceptance criteria)

These are testable release gates, not aspirations:

| Metric | Budget |
|---|---|
| App bundle size | < 10 MB |
| Resident memory, popover closed | < 60 MB |
| Resident memory, window + 2 log streams | < 120 MB |
| CPU, UI closed, fleet quiet | < 0.1% avg (one GET/60 s) |
| Cold launch → live popover | < 500 ms on Apple silicon |
| Energy impact (Activity Monitor) | "Low" sustained |
| Third-party dependencies | **0** |

Log views virtualize (keep last 5 000 lines in memory, "load earlier" pages via
`GET /jobs/{id}/logs?since=`).

---

## 9. Backend asks

The API already supports the entire v1 — by design we surface, not extend. Two small asks,
neither blocking:

1. **`POST /do` (or `classify` on `POST /jobs`)** — server-side home for `roost do`'s
   destructive-goal classification so CLI and app share one implementation (§4). Until
   then the app submits `kind: auto` directly.
2. **`GET /derived` ETag / `If-None-Match`** — turns the idle poll into a 304 and makes
   the ambient loops near-free. Pure optimization.

---

## 10. Milestones

**M0 — Read-only fleet glance** *(the app earns its menu bar slot)*
Connection onboarding, Keychain, `/derived` poll loop, menu bar verdict icon, popover with
fleet/runs/workers, degraded states. *Exit: replaces the `/panel` browser tab for daily
glancing.*

**M1 — The loop closes** *(submit · watch · know)*
Goal box (`kind: auto`, verify on), run detail with phase rail + SSE logs, cancel, retry,
notifications on terminal/alert, global hotkey. *Exit: a full goal → verified-result cycle
without opening a terminal.*

**M2 — Depth**
Main window (history with filters/search, workers table), captain runs (options panel +
tree view), cost surfacing, prune-ghost-workers admin action, settings (hotkey, dock icon,
notification toggles, cadence override).

**M3 — Polish & distribute**
Performance gates (§8) enforced in CI, accessibility audit, Developer ID signing +
notarization, Sparkle-free update check (a `GET` against GitHub releases + "download"
link — keeping the zero-dependency rule), `README` + screenshots. Retire
`launcher.sh`/`panel_window.py`/`build.sh` from `mac-app/`.

**Post-v1 candidates** — enrollment from the app (mint token → QR/copy install one-liner,
pairs with the `roost-onboard` skill), Activity timeline, multi-CP switching, menu-bar
mini-mode (icon-only with text status), Shortcuts/AppleScript "Run Goal" action, iOS
companion (reuses `RoostKit`).

---

## 11. Distribution & project layout

- **Channel:** Developer ID signed + notarized direct download (GitHub Releases). Not Mac
  App Store for v1 — MAS sandboxing would break reading `~/.config/roost/config.toml`
  (the one-click onboarding) and adds review latency for no distribution win with this
  audience.
- **Sandbox:** app is *not* sandboxed v1 (config detection, arbitrary-host networking).
  Revisit if MAS ever matters; everything else in the design is sandbox-compatible.
- **Layout:** *(SwiftPM package, no Xcode project — `swift build` is the whole story,
  and `RoostKit` builds and tests on Linux too, so CI for the client layer doesn't need
  a Mac)*
  ```
  mac-app/
    DESIGN.md              ← this document
    Package.swift          ← RoostKit (library) + RoostMac (app) + tests
    Sources/RoostKit/      ← API client + models (UI-free, tested)
    Sources/RoostMac/      ← app target: stores, views, AppKit shell
    Tests/RoostKitTests/   ← fixture-pinned decoding, SSE parser, config tests
    Info.plist             ← bundle manifest (LSUIElement)
    scripts/build.sh       ← swift build → assemble .app → sign → notarize
  ```
- **CI:** `.github/workflows/mac-app.yml` — `swift test` for `RoostKit` (client decodes
  recorded fixture payloads from the real server; SSE parser unit tests) runs on a Linux
  container for every PR; the full app builds + tests on a macOS runner.

---

## 12. Risks & open questions

| # | Risk / question | Position |
|---|---|---|
| 1 | `/derived` payload shape drifts as the backend evolves quickly | `Codable` models ignore unknown fields; fixture tests pin the fields we *do* read; client surfaces a "snapshot partially understood" state rather than crashing |
| 2 | SSE through proxies/Tailscale Funnel can buffer | resume-via-`since` makes reconnect cheap; fall back to `GET /jobs/{id}/logs` polling after 2 failed stream attempts |
| 3 | Is the goal box too magical without `roost do`'s destructive-check? | acceptable for v1 (verify-on default + explicit cancel); resolved properly by backend ask §9.1 |
| 4 | min macOS 14 excludes older Macs | acceptable: those Macs can be *workers* regardless; the operator console targets current macOS |
| 5 | Token in URL for `/panel` parity — do we ever embed the web panel? | no; native-only client, header auth everywhere |
| 6 | Multiple operators on one CP — write contention? | none: app's writes are POST job / DELETE job, both idempotent-ish and user-initiated |

---

## 13. Console — Claude Code, fleet-wired

> The PoC's best idea, done properly: you don't just *watch* the fleet, you *talk* to it.

A real terminal pane running **Claude Code with the fleet already in its hands**: `ROOST_URL`
+ `ROOST_TOKEN` in the environment, the `roost mcp` server attached, and a workspace
folder with fleet context. Zero setup — open Console, type "what's running on the GPU
box", and Claude drives the fleet through its MCP tools.

### UX

```
┌─ Roost ──────────────────────────────────────────────────────┐
│ ⊞ Runs      │  CONSOLE   claude · ~/RoostConsole · fleet ✓   │
│ ⊞ Workers   │  ┌────────────────────────────────────────────┐│
│ ▣ Console   │  │ > what's running on the gpu box?           ││
│ ⊞ Transfers │  │                                            ││
│             │  │ ⏺ Checking the fleet…                      ││
│             │  │   roost - list_runs (MCP)                  ││
│             │  │ The dgx-1 worker is fine-tuning…           ││
│             │  │ ▌                                          ││
│             │  └────────────────────────────────────────────┘│
│             │           [Restart]  [Open folder]  [⌘K clear] │
└──────────────────────────────────────────────────────────────┘
```

- **One keystroke away**: `Console` sidebar item in the main window; `⌘T` from anywhere
  (popover footer gains `Console ⌘T`). The terminal IS the pane — no chrome beyond a thin
  header strip: what's running (`claude` or plain `zsh`), the cwd, a fleet-env indicator,
  and a Restart button.
- **It's a real terminal** (SwiftTerm `LocalProcessTerminalView`): full xterm-256color,
  so Claude Code's TUI renders faithfully — and when you want the raw `roost` CLI instead
  of Claude, it's just… a terminal. Both audiences served by one component.
- **Run → Console deep link**: a failed run's detail gains *"Investigate in Console"* —
  opens the Console with a prepared prompt typed (not submitted): `Investigate roost run
  1a2b3c (goal: "build docs…") — it failed with: npm not installed. Diagnose and fix.`
  The user reviews, edits, hits ⏎. (Type-don't-submit is deliberate: an agent that
  auto-starts acting on a click is a footgun.)
- **Session lifecycle**: the process ends → dimmed overlay with "Session ended — Restart
  (⌘R)". App quit kills the PTY (no orphaned agents). One session v1; tabs are post-v1.

### Wiring (all generated, nothing global is touched)

| Piece | How |
|---|---|
| env | parent env + `TERM=xterm-256color` + `ROOST_URL`/`ROOST_TOKEN` from the app's connection (token from Keychain → child env: stays on this machine, user-consented by opening Console) |
| claude binary | probed: `~/.claude/local/claude`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, then `command -v claude` via login shell |
| MCP | a generated `~/Library/Application Support/Roost/console/mcp.json` (`roost mcp` + env), passed via `claude --mcp-config …` — the user's own `~/.claude` config is never edited |
| cwd | `~/RoostConsole/` (created on first open) with a generated `CLAUDE.md` describing the fleet + the roost CLI, so Claude has context from message one |
| missing pieces | `claude` not installed → session falls back to plain `zsh` with a one-line banner + copyable install command; same pattern for `roost` (MCP config simply omitted) — **the app never installs software** (§6) |

### Anti-goals

No chat-bubble reimplementation of Claude Code (we'd be chasing its features forever);
no multiplexing/tmux; no remote-host terminals (that's SSH's job — or a transfer + job).

---

## 14. File transfer — move files like you move work

> Drag a file onto a worker. That's the whole feature.

Roost can already run anything anywhere — what's missing is getting *bytes* to and from
nodes without SSH. The design rides the existing rails: the control plane grows a small
**blob store** (staging area), and the worker-side leg of every transfer is a normal
**command job** — so transfers inherit placement, retries, liveness, history, and the
dashboard for free. Workers behind NAT need no new reachability: they already reach the CP.

### UX

**Send** (the 95% case):
1. Drag a file from Finder onto a worker row — popover or Workers table. Row highlights.
2. Drop → compact sheet: `Send report.pdf (1.2 MB) to pi5` · destination pre-filled
   `~/roost-inbox/report.pdf` (editable, remembered per worker) · **[Send]**.
3. Progress where you dropped it: the transfer appears as a run — goal *"deliver
   report.pdf → pi5"* — because the delivery leg IS a job. Phase rail: `uploading →
   queued → delivering → ✓ delivered (sha256 ✓)`. Done/failed notification like any run.

**Fetch**: worker detail → *"Fetch file…"* → remote path → the job pushes the file to the
CP blob store → save panel (default `~/Downloads`). Same run-card treatment.

**Fleet clipboard**: every uploaded blob has a copyable presigned URL with TTL. Power
move: upload `train.py`, then in the goal box — *"on the GPU box, fetch <url> and run
it"*. Blobs make files addressable to agents, not just to this app.

**Transfers pane** (sidebar, badge = active count): history of sends/fetches with
re-send, copy-URL, reveal-in-Finder; plus anything currently staged on the CP with size
+ expiry, and delete.

### Backend contract (new in `roost/server.py`, schema V10)

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /blobs?name=…&ttl_sec=…` (raw body) | bearer | stage a file → `{id, name, size, sha256, expires_at, get_url}` (`get_url` presigned) |
| `GET /blobs/{id}` | bearer **or** `?exp&sig` | download (streamed) |
| `POST /blobs/presign` `{name?, ttl_sec?}` | bearer | mint `{id, put_url}` for a **worker-side upload** (fetch flow) |
| `PUT /blobs/{id}?exp&sig` (raw body) | presigned | worker-side upload leg |
| `GET /blobs` / `DELETE /blobs/{id}` | bearer | list / delete staged blobs |

- **Presigned URLs** (HMAC over `id·exp·verb` with a per-CP secret persisted next to the
  DB, 0600) are what let the *job* curl without credentials — jobs never see tokens.
- Storage: `<data_dir>/blobs/<id>`, rows in a `blobs` table. **Default TTL 24 h**, the
  existing sweeper deletes expired rows + files; **size cap 512 MB** (`413` beyond) —
  this is a staging area, not a filesystem.
- Delivery job shape: pinned hard via `requires: {hostname: "<worker's>"}`, command =
  `mkdir -p … && curl -fsS <get_url> -o <dest> && shasum/sha256sum check`; intent set to
  *"deliver <name> → <worker>"* so every surface (panel, CLI, popover) narrates it.
- CLI parity (`roost push <file> <worker>:<path>` / `roost pull …`) falls out of the same
  endpoints — post-v1, tracked not designed here.

### Security posture

Presigned URLs are bearer-by-possession: short TTL (default = blob TTL, capped), bound
to one blob + verb, and the secret never leaves the CP. The app warns (once) when staging
through a plain-HTTP CP, same rule as §6. No path traversal server-side: blob ids are
server-minted hex; destination paths are the *worker's* business inside its normal job
sandbox/policy.

---

## 15. Milestone M4 — Console & Transfers

Console (SwiftTerm pinned, session manager, deep link from failed runs, fallbacks),
blob backend (+tests), RoostKit blob client (+tests), drag-drop send, fetch flow,
Transfers pane, popover badge. *Exit: drag a file onto a Pi and watch it verify; open
Console and have Claude explain the fleet — no terminal setup, ever.*

---

## 16. Multi-window redesign (addendum)

> The single 6-tab window became a small set of independent, state-preserving
> windows a heavy multi-monitor user can spread out — without losing the
> menu-bar glance.

The original §2.4 described one main window with a sidebar `switch`. That model
forced every surface through one window and, worse, destroyed the Console's PTY
whenever you navigated away (the detail subtree unmounted the terminal). The app
now uses a **window registry** (`WindowManager`, keyed by `WindowKind`):

- **Workspace** (`⌘O`/`⌘1`, Dock click) — Runs master/detail. Per-window
  selection lives in `WorkspaceModel`, not on the shared `AppModel`.
- **Console** (`⌘T`/`⌘2`) — its own window whose `contentView` is the raw,
  app-owned `LocalProcessTerminalView`. It is **never** wrapped in
  `NSViewRepresentable`, so navigation and hide/show can't tear down the live
  Claude session. Closing the window hides it (PTY keeps running); Restart is
  explicit (`terminate()` + rebuild); the PTY is killed only on app quit.
- **Fleet** (`⌘3`) — Transfers · Publish · Schedules · Workers behind one
  segmented header (`FleetWindowModel`). Workers is demoted here.
- **Run detail** — any run opens in its own cascaded window ("Open in New
  Window" / ⌥-click). The registry owns its `RunDetailModel` so the SSE stream
  keeps flowing while the window is hidden behind another monitor's window.

Cross-cutting rules: each window is a distinct `NSHostingController` with its own
per-window `@Observable` model injected alongside the shared `AppModel`; poll
cadence (§5) keys off *any* window being visible (not just the old single main
window); `runDetail` is the one archetype that truly closes (and stops its
stream), the rest hide and are reused. The menu-bar popover (§2.2) slimmed to an
ambient glance — verdict, goal box, a few active run cards, and footer links to
the windows. The calm visual language (one status dot per row, three type sizes,
cards over dense rows, narration on hover) lives in `Views/DesignSystem.swift`.

---

*Designed against the live API of `roost/server.py` (v0.2.0): `/derived`, `/jobs`,
`/jobs/{id}/stream` (SSE), `/jobs/{id}/tree`, `/workers`, `/workers/prune`, `/healthz`,
and (new in §14) `/blobs`.*
