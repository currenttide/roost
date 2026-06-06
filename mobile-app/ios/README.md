# Roost — iOS app

A lightweight SwiftUI iPhone client for a Roost fleet: pair with a control plane,
watch the dashboard live, dispatch agent/command jobs (type or hold-to-talk), and
stream job logs over a hand-rolled SSE client. iOS 17+, Swift 5.9+, **zero
third-party dependencies** (Foundation / SwiftUI / AVFoundation / Speech / Security).

This builds against the pinned contract in [`../API.md`](../API.md); the design is
in [`../DESIGN.md`](../DESIGN.md).

## The repo contains no `.xcodeproj`

The Xcode project is a *generated artifact*. The source of truth is
[`project.yml`](project.yml) plus the Swift files under `Roost/`. We generate the
`.xcodeproj` with [XcodeGen](https://github.com/yonaskolb/XcodeGen) so the repo
stays diffable and never carries a hand-edited `project.pbxproj`. The generated
project (and `DerivedData/`) are git-ignored.

## Build (on a Mac)

```sh
brew install xcodegen          # one-time
cd mobile-app/ios
xcodegen generate              # produces Roost.xcodeproj from project.yml
open Roost.xcodeproj           # then ⌘R to run on a simulator or device
```

Or fully from the command line:

```sh
xcodebuild \
  -project Roost.xcodeproj \
  -scheme Roost \
  -destination 'platform=iOS Simulator,name=iPhone 15' \
  build
```

To run on a physical device, set `DEVELOPMENT_TEAM` in `project.yml` (or in Xcode's
Signing & Capabilities) and re-run `xcodegen generate`.

## Run the tests

The unit tests decode **every** golden fixture in `../fixtures/`, parse the SSE
transcript through the frame parser, exercise the pairing-URI decode (incl.
base64url padding restoration and `v>1` rejection), and check the
`health.status → glyph` map (incl. the unknown-status fallback).

```sh
cd mobile-app/ios
xcodegen generate
xcodebuild test \
  -project Roost.xcodeproj \
  -scheme Roost \
  -destination 'platform=iOS Simulator,name=iPhone 15'
```

> The fixtures are added to the test bundle by **reference** to the single repo copy
> in `../fixtures` (see the `RoostTests.resources` entry in `project.yml`), so there
> is exactly one copy of the golden data in the repo.

## Pairing (no in-app QR scanner)

On the control-plane host:

```sh
roost pair                     # prints a QR encoding {url, token} + the raw code
```

Then either:

- **Scan the QR with the system Camera app.** It opens `roost://pair?d=…`, which
  launches the app straight into pairing. (We deliberately don't ship an in-app
  scanner — the OS camera is the thin-client choice.)
- **Paste the raw code** (`roost://pair?d=…` or just the base64url blob) into the
  manual field on the pairing screen.

The app probes `GET /healthz` before accepting, stores `url`+`token` in the
**Keychain**, and uses a **mobile-scoped** token (no enroll minting, no worker
deletion, no `/claude-creds`). A `401` anywhere later unpairs back to this screen;
a `403` shows an error but stays paired (it's a scope bug, not a revoked token).

## What's where

| Path | Purpose |
|---|---|
| `Roost/App/` | `@main` app + root routing (paired vs pairing) |
| `Roost/Models/` | Codable models, the failable `HealthStatus`, type-erased `JSONValue` |
| `Roost/Net/` | `ApiClient`, hand-rolled `SSEParser` + `LogStream` resume loop, Keychain, pairing decode, ANSI strip |
| `Roost/Speech/` | on-device dictation (Speech + AVAudioEngine), haptics |
| `Roost/Stores/` | one `ObservableObject` per screen + `AppState` |
| `Roost/Views/` | Pairing, Dashboard, Session, New-session sheet |
| `RoostTests/` | fixture-decode, SSE, pairing, glyph tests |

## Known build risks

**Verified 2026-06-05**: full app compiles (`xcodebuild build`, Xcode 26.2) and the
complete test suite passes on an iOS 26.3 simulator (`xcodebuild test`, iPhone 17
Pro) — run remotely on the fleet's mac-mini-m4 via a pinned Roost job. The items
below were the pre-verification watchlist; none materialized beyond four trivial
errors (3× `catch`-binding shadowing of an `error` property, 1× `.tint` in a
`Color` ternary), all fixed. Kept for context if SDKs shift:

- **`AVAudioApplication.requestRecordPermission` (iOS 17+).** If the deployment
  target is lowered or this symbol is unavailable, replace the mic-permission call
  in `Speech/Dictation.swift` with the deprecated
  `AVAudioSession.sharedInstance().requestRecordPermission { … }`.
- **`safeAreaPadding(.bottom:)` on `List` (iOS 17+).** If it doesn't resolve on
  `List` in `Views/DashboardView.swift`, swap it for `.padding(.bottom, 72)` or a
  `.safeAreaInset(edge: .bottom)` spacer.
- **`ContentUnavailableView` (iOS 17+).** Used in `Views/SessionView.swift`'s tree
  sheet; if unavailable, replace with a plain `Text("No child jobs")`.
- **`onChange(of:) { _, _ in }` two-parameter closure (iOS 17+).** If building
  against an older SDK, use the single-parameter `onChange(of:) { _ in }` form in
  `DashboardView.swift` and `SessionView.swift`.
- **Folder-reference resource flattening.** `project.yml` adds `../fixtures` as a
  `type: folder` reference, so fixtures resolve at `Bundle/fixtures/<name>`.
  `RoostTests/Fixtures.swift` already falls back to a flat bundle layout if Xcode
  flattens it; if a fixture still isn't found, change that `resources` entry to
  `type: group` (copies files individually) and the flat fallback will catch them.
- **SSE transport.** `LogStream` iterates the raw `URLSession.bytes` byte
  sequence and assembles lines itself (deliberately NOT `AsyncLineSequence`,
  which drops the blank lines that delimit SSE frames on some SDKs).
- **Plain-http over Tailscale.** ATS's `NSAllowsLocalNetworking` covers LAN
  (RFC 1918, `.local`) but NOT Tailscale's 100.64/10 CGNAT range. If your CP is
  plain http over a tailnet, either serve HTTPS (e.g. `tailscale serve`) or add
  an `NSExceptionDomains` entry for the host in `project.yml` — don't enable
  `NSAllowsArbitraryLoads`.

## Linux pure-layer harness

The Models/Net parsing layer is pure Foundation, so the full test suite minus
UI runs on Linux (verified 2026-06-05, 32/32 green on Swift 6.0.3):

```sh
# one-time: toolchain from swift.org into /tmp/swift-toolchain
mkdir -p /tmp/ios-linux-check/{Sources/Roost,Tests/RoostTests} && cd /tmp/ios-linux-check
# Package.swift: target Roost + testTarget RoostTests (see mac-app for the pattern)
# symlink: Models/*.swift, Net/{SSE,Pairing,Ansi,ApiClient,OfflineCache}.swift → Sources/Roost
#          RoostTests/*.swift → Tests/RoostTests
ROOST_FIXTURES=$REPO/mobile-app/fixtures /tmp/swift-toolchain/usr/bin/swift test
```

`ROOST_FIXTURES` points `Fixtures.swift` at the repo copy (no bundle on Linux).

## Simulator demo / automation hook

`AppState` honors a `ROOST_PAIR_URI` env var (unpaired launches only) so demos
and UI tests can pair without tapping through the system open-URL dialog:

```sh
roost pair --label sim-demo        # mint a token, take the roost://pair?d=… URI
xcrun simctl install booted Roost.app
SIMCTL_CHILD_ROOST_PAIR_URI='roost://pair?d=…' xcrun simctl launch booted rs.roost.mobile
```

Verified 2026-06-05 end-to-end on the fleet Mac's iPhone 17 Pro simulator:
paired to the live control plane over the LAN and rendered the real fleet.
(This run also caught a real bug: the pairing view's `.task(id:)` consumed its
own id before awaiting, so SwiftUI cancelled the in-flight healthz probe —
`transport("cancelled")` — which would have broken camera-QR pairing on real
devices too. Fixed by clearing `pendingPairURL` after the attempt.)
