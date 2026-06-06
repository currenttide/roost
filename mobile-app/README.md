# Roost Mobile

Native iPhone + Android clients for vibe-coding against your Roost fleet: speak or
type an intent, dispatch it as an agent job, watch it stream live, steer a dashboard
of concurrent sessions from your pocket.

- **`DESIGN.md`** — the product design (screens, principles, weight budget).
- **`API.md`** — the pinned API contract both apps build against.
- **`fixtures/`** — golden JSON + SSE transcript recorded from a live control plane
  (`python mobile-app/record_fixtures.py`); both app test suites decode every file.
- **`ios/`** — SwiftUI app (iOS 17+, zero third-party deps). See `ios/README.md`.
- **`android/`** — Kotlin/Compose app (minSdk 26, androidx-only). See `android/README.md`.

## Pairing (both platforms)

On any machine with admin access to the control plane:

```
roost pair --label "yang-iphone"          # add --url http://<LAN-addr>:8787 if needed
```

Scan the printed QR with the system camera (the `roost://` link opens the app), or
paste the `roost://pair?…` string into the app's pairing screen. Tokens are
**mobile-scoped** (read + submit/cancel only — no creds, no worker admin); manage
them with `roost pair --list` / `roost pair --revoke <id>`.

## Build quickstart

| | iOS (on a Mac) | Android |
|---|---|---|
| Generate project | `brew install xcodegen && cd mobile-app/ios && xcodegen generate` | `cd mobile-app/android && gradle wrapper` |
| Open / build | `open Roost.xcodeproj` | `./gradlew :app:assembleDebug` |
| Unit tests | `xcodebuild test -scheme Roost -destination 'platform=iOS Simulator,name=iPhone 16'` | `./gradlew :app:testDebugUnitTest` |

Each app README carries a "Known build risks" section — SDK choices that couldn't be
compiled in the authoring environment, each with its one-line fix.

## Contract discipline

The server may only ADD fields (API.md §7). After any server change touching these
shapes: `python mobile-app/record_fixtures.py`, re-run both app test suites, and the
decode tests pinpoint any drift. The apps consume nothing outside API.md.
