# Roost Mobile — design doc

**A lightweight native app for iPhone and Android that lets you vibe-code against your
Roost fleet: speak or type an intent, dispatch it as an agent job, watch it stream live,
and steer a whole dashboard of concurrent sessions from your pocket.**

Status: v1 implemented + fully verified 2026-06-05 (M0–M4: server pairing scope,
`API.md` contract + fixtures, iOS + Android apps incl. offline cache — see
`README.md`). Android: compiles + 31 unit tests via Gradle (Linux). iOS: compiles
(Xcode 26.2) + full test suite on an iOS 26.3 simulator — built remotely on the
fleet's Mac via a Roost job — plus 32 pure-layer tests on the Linux harness.
Decided 2026-06-05: Roost-fleet backend · native per-platform (SwiftUI / Jetpack
Compose) · on-device dictation · multi-session dashboard.

---

## 1. Vision & principles

The phone is a **thin client**. All judgment and execution live in the fleet — Claude
Code running on real machines with repos, tools, and credentials. The app's only jobs:

1. Turn a spoken or typed intent into a Roost job (`POST /jobs`).
2. Show what the fleet is doing, live (`/derived`, `/jobs/{id}/stream`).
3. Let you react fast: cancel, retry, follow up, glance.

**Principles**

- **Lightweight is a feature.** Target < 10 MB installed per platform. Zero third-party
  runtime dependencies in v1 — OS networking, OS speech, OS UI toolkit. If a feature
  needs a heavyweight dep, it's a v2 feature.
- **Mechanical client, judging fleet.** Mirrors the Roost philosophy: the app never
  interprets agent output beyond rendering it. No on-phone LLM calls in v1.
- **Glanceable first, readable second.** The dashboard answers "is everything OK?" in
  one second (it reuses the server's `fleet_verdict`). The session view rewards a longer
  look with the live log.
- **Survive the pocket.** Backgrounding, network flaps, and lock-screen time are the
  normal case. Every stream must resume from a cursor; nothing is lost by looking away.

## 2. Architecture

```
┌─ iPhone ──────────────┐        ┌──────────────────────────────┐
│ SwiftUI app           │  HTTPS │  Roost control plane         │
│  · URLSession (SSE)   │◄──────►│  (existing FastAPI, :8787)   │
│  · Speech.framework   │        │   POST /jobs                 │
└───────────────────────┘        │   GET  /derived              │
┌─ Android ─────────────┐        │   GET  /jobs/{id}/stream SSE │
│ Jetpack Compose app   │◄──────►│   GET  /jobs/{id}/tree       │
│  · OkHttp-free: HUC*  │        │   DELETE /jobs/{id}          │
│  · SpeechRecognizer   │        └──────────────┬───────────────┘
└───────────────────────┘                       │ long-poll lease
        * java.net.HttpURLConnection            ▼
          or minimal Ktor client         workers (the fleet)
```

Two native codebases, **one shared API contract**: `mobile-app/API.md` (to be written
alongside implementation) pins the exact request/response shapes both apps consume, with
JSON fixtures recorded from a live CP used as golden test data on both platforms. That —
not a shared runtime — is the duplication control. (If the apps grow past ~3k lines of
model/sync logic each, revisit Kotlin Multiplatform for the client core; not v1.)

### What the existing CP already provides (verified against `roost/server.py`)

| Need | Endpoint | Notes |
|---|---|---|
| Dashboard summary | `GET /derived?limit=40` | Same payload the mac panel polls: `{generated_at, fleet_verdict:{level,summary}, workers[], runs[]}` |
| Submit a job | `POST /jobs` | `claude` kind = agent job |
| Live session stream | `GET /jobs/{id}/stream?since=N` | SSE: `state` / `log` / `done` events, seq-cursor resumable — perfect for backgrounding |
| Catch-up after offline | `GET /jobs/{id}/logs?since=N` | Page the gap, then re-attach SSE |
| Job detail / children | `GET /jobs/{id}`, `/jobs/{id}/tree` | Tree covers captain-dispatched sub-jobs |
| Cancel | `DELETE /jobs/{id}` | |
| Fleet view | `GET /workers` | |

### Server gaps to fill (small, additive)

1. **Scoped mobile token.** Today the app would hold the admin token. Add a `mobile`
   token scope: everything `require_any` allows plus `POST /jobs` + `DELETE /jobs/{id}`,
   but not enroll-token minting, worker deletion, or creds provisioning. Issued via a
   QR-code pairing flow: `roost pair` prints a QR encoding `{url, token}`; the app scans
   it. One new table column (token scope) + one CLI subcommand.
2. **Follow-up input.** Jobs are currently fire-and-forget. v1 ships without mid-job
   steering: "follow up" on a finished session submits a **new job carrying the parent's
   context** (the existing `roost history` goal-memory makes this natural). True
   interactive stdin-to-agent is a v2 CP feature, deliberately deferred.
3. **Push notifications (v1.1, not v1).** A tiny CP-side notifier that fires on job
   terminal states. To stay dependency-light, v1.1 uses **ntfy.sh self-hosted or
   UnifiedPush-style webhooks** rather than APNs/FCM plumbing; native APNs/FCM only if
   that proves insufficient. v1 relies on foreground SSE + pull-to-refresh.

Nothing else changes server-side. The app is a fourth consumer of the same API that the
CLI, the mac panel, and workers already use.

## 3. The two screens (plus one sheet)

v1 is deliberately **two screens and a sheet**. Everything else is cut.

### 3.1 Dashboard (home)

```
┌──────────────────────────────┐
│ ● ALL HEALTHY        13 nodes│  ← fleet_verdict bar (green/amber/red)
│──────────────────────────────│
│ ▶ fix flaky auth test        │  ← running: spinner + node + elapsed
│   hubbase · claude · 4m 12s  │
│ ▶ bump deps in roost-oss     │
│   digitalocean · 1m 03s      │
│ ✓ write panel e2e test       │  ← done: ✓/✗ + one-line result
│   m1-mini · 12m · succeeded  │
│ ✗ migrate db schema          │
│   pi4 · failed · exit 1      │
│──────────────────────────────│
│        [ 🎤  New session ]   │  ← the one big button
└──────────────────────────────┘
```

- Single `GET /derived` poll (foreground: every 2 s; matches the mac panel's contract,
  reuses `fleet_verdict` untouched).
- Rows sorted: running first, then most-recent terminal. Tap → Session view.
- Swipe a running row → Cancel (confirm). Swipe a failed row → Retry (resubmits spec).
- **Staleness guard**: if `generated_at` drifts > 10 s behind wall clock, show a
  "data Ns old" pill instead of silently rendering a stale frame (same lesson as the
  mac panel's stale-render bug).

### 3.2 Session view

```
┌──────────────────────────────┐
│ ← fix flaky auth test   ▶ 4m │
│ hubbase · claude · running   │
│──────────────────────────────│
│ ┊ live log (SSE)             │
│ ┊ > running pytest -q ...    │
│ ┊ > 2 failed, re-reading ... │
│ ┊ > editing tests/test_auth  │
│ ┊ ▼ (auto-follow tail)       │
│──────────────────────────────│
│ [Cancel]            [Tree ▸] │
│──────────────────────────────│
│ ⌨ type…            [🎤 hold] │  ← composer (enabled when terminal:
└──────────────────────────────┘     "follow up" = new job w/ context)
```

- SSE attach with `since=<last seen seq>`; on background→foreground, page
  `/logs?since=` to fill the gap, then re-attach. Cursor persisted per job id.
- `done` event renders a result card: state, exit code, `result` summary, tokens used.
- "Tree ▸" shows child jobs for captain dispatches (`/jobs/{id}/tree`) as an indented
  list, each row tappable into its own session view.
- Log rendering is plain monospaced text with ANSI-color stripping — no markdown
  engine, no webview. Lightweight, scrolls at 60 fps.

### 3.3 New-session sheet

```
┌──────────────────────────────┐
│ New session                  │
│ ┌──────────────────────────┐ │
│ │ "refactor the matcher to │ │  ← live dictation transcript,
│ │  use capability sets…"   │ │     editable as text
│ └──────────────────────────┘ │
│ target: ⦿ auto  ○ pin node   │  ← auto = CP placement (default)
│ kind:   ⦿ agent ○ command    │
│            [ Dispatch ]      │
└──────────────────────────────┘
```

- Opens from the big dashboard button. **Hold the mic = talk; release = transcript
  lands in the editable text field.** Tap the field to type instead. Voice and text
  are the same path — voice is just a faster keyboard.
- "Dispatch" → `POST /jobs` with `kind: claude`, prompt = the text, `requires:` empty
  (auto-place) or pinned to a worker id. Then jump straight into the Session view.
- Recent prompts (last 10, stored locally) appear below the field for one-tap reuse.

## 4. Voice input

On-device OS dictation only — no audio leaves the phone, no STT infra:

| | iOS | Android |
|---|---|---|
| API | `Speech.framework` (`SFSpeechRecognizer`, `requiresOnDeviceRecognition = true` where available) | `android.speech.SpeechRecognizer` with `EXTRA_PREFER_OFFLINE` |
| UX | hold-to-talk, live partial transcript, haptic on start/stop | same |
| Permissions | mic + speech recognition | mic |
| Fallback | if recognizer unavailable → keyboard, mic button hidden | same |

Code-word accuracy ("pytest", "matcher.py") will be imperfect — acceptable because the
transcript is **always editable before dispatch**, and the agent on the other end is
good at resolving slightly-mangled references. If accuracy hurts in practice, v2 adds an
optional Whisper-on-fleet transcription job behind a setting; the UI doesn't change.

## 5. Networking & resilience

- **Transport**: plain HTTPS to the CP URL from pairing. LAN (`192.168.x.x:8787`) works
  out of the box; remote access is the user's existing tunnel/Tailscale — the app just
  takes a URL, same as the CLI.
- **SSE client**: hand-rolled on both platforms (~100 lines each: parse `event:`/`data:`
  frames off a streaming response). No SSE library dependency.
- **Reconnect policy**: exponential backoff 1 s → 30 s, jittered; always resume with
  `since=`. Dashboard poll pauses when backgrounded.
- **Offline**: last-known dashboard + logs render from a small on-device cache (single
  JSON file per job, capped at 500 lines) with the staleness pill shown.

## 6. Security

- Token stored in iOS Keychain / Android Keystore-encrypted prefs — never in plain files.
- QR pairing only displays on the machine running `roost pair`; token scope is `mobile`
  (no enroll minting, no worker deletion, **no `/claude-creds` access** — consistent with
  the repo rule that credential flows are explicit and consented).
- App pins nothing in v1 (LAN/tunnel trust model, same as CLI); cert pinning revisited
  if a public-internet deployment story emerges.
- No analytics, no third-party SDKs, nothing phones home except the CP.

## 7. Weight budget (enforced, not aspirational)

| Constraint | Target |
|---|---|
| Installed size | < 10 MB per platform |
| Runtime deps | 0 third-party (v1) |
| Screens | 2 + 1 sheet |
| Cold start → dashboard rendered | < 1 s on LAN |
| Battery | no background networking in v1; SSE only while foregrounded |

## 8. Milestones

- **M0 — contract.** Write `mobile-app/API.md` + record golden JSON fixtures from a live
  CP. Add `mobile` token scope + `roost pair` QR to the CP (the only server work in v1).
- **M1 — iOS read-only.** Pair, dashboard, session view with live SSE. (iOS first only
  because the existing mac app gives us a tested CP nearby; Android follows the frozen
  contract.)
- **M2 — iOS dispatch.** New-session sheet, dictation, cancel/retry. *Usable daily.*
- **M3 — Android parity.** Compose app against the same fixtures.
- **M4 — polish.** Job tree view, recent-prompt reuse, offline cache, staleness pill.
- **v1.1** — terminal-state notifications (ntfy/webhook). **v2** — interactive mid-job
  steering (CP feature), optional Whisper-on-fleet STT, widgets/watch complications.

### 8a. v1.1 push notifications — client wiring

The CP side shipped first (R37): on every terminal job the control plane fires a
fire-and-forget `POST` to a configured `--notify-url` carrying JSON
`{event, job_id, state, intent, duration_sec, exit_code, worker_id, message}` plus
ntfy display headers (`Title`/`Priority`/`Tags`). The canonical payload is pinned by
`tests/test_notify.py`.

The **client slice** (R55) implements the parts that are honest to build and verify
without a device, on both platforms, and keeps them as pure, Linux-tested logic:

- **Topic as a setting.** The CP does not advertise its `notify_url` over the API, so
  the app takes the ntfy topic as a manual setting (a Notifications screen reachable
  from the dashboard overflow). The user enters a bare topic (→ `https://ntfy.sh/<topic>`)
  or a full self-hosted URL; the app stores the canonical subscribe URL. Normalize +
  validate is pure logic (`NtfyTopic` — iOS `Net/Notifications.swift`, Android
  `model/Notify.kt`). The non-secret topic lives in `UserDefaults` / plain prefs (not the
  Keychain/Keystore — it's a channel name, not a bearer token).
- **Payload → deep-link route.** Given the R37 payload, the client produces the
  destination: a non-blank `job_id` opens that job's **Session** view; a malformed,
  non-JSON, or job-id-less payload falls back to the **Dashboard** (never crash, never
  guess). This routing is pure (`NotifyRouter`) and is the core covered on both Linux
  harnesses, including a **cross-contract** test that parses payload literals copied
  verbatim from `tests/test_notify.py` so server/client drift is caught.

**Capped — device-only, NOT verified here (no devices in the build env):** the actual
push transport. iOS has no UnifiedPush distributor concept, so the honest v1.1 iOS path
is the user subscribing to the same topic in the ntfy app; a `PushService`
(`#if canImport(UIKit)`) holds the seam (local-notification + tap→route) but real
remote/background delivery (APNs) is deferred. On Android the dependency-light path is
**UnifiedPush** (the ntfy app as distributor); `push/PushReceiver.kt` builds the tappable
notification and routes the tap through the same pure `NotifyRouter`, but registering with
a distributor, the foreground subscription, and consuming the tap intent in `MainActivity`
need a real device + the `unifiedpush` connector and are left for on-device work.

## 9. Cut from v1 (explicitly)

Diff viewer with syntax highlighting (the agent's `result` summary suffices; full diffs
are a desktop activity) · markdown rendering · multi-CP profiles · iPad/tablet layouts ·
themes · on-phone repo browsing · push notifications (v1.1) · interactive steering (v2).
