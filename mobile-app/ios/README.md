# Roost — iOS app

A lightweight SwiftUI iPhone client for a Roost fleet: pair with a control plane,
watch the dashboard live, dispatch agent/command jobs (type or hold-to-talk), and
stream job logs over a hand-rolled SSE client. iOS 17+, Swift 5.9+, **zero
third-party dependencies** (Foundation / SwiftUI / AVFoundation / Speech / Security).

This builds against the pinned contract in [`../API.md`](../API.md); the design is
in [`../DESIGN.md`](../DESIGN.md).

## Distilled session view (R108)

The session log **defaults to a distilled rendering** of an agent job's
`stream-json`: assistant text, `→ Tool: summary`, truncated `⎿` results, and
`🔎/✓/✗` phase dividers — base64 signatures, reasoning blobs, rate-limit pings,
and roost-internal envelopes are suppressed. A **"Distilled / Raw" toggle** in the
session footer (default off = distilled) reveals the unfiltered firehose; it
re-renders the rows already in hand, no re-fetch. The transform is the pure
`DistilledLine.from(_:)` (`Roost/Net/Distill.swift`), an exact mirror of the
language-neutral contract in [`../fixtures/distilled/SPEC.md`](../fixtures/distilled/SPEC.md)
and the CLI reference impl (`roost.cli.distill_log_line`). `DistilledTests` loads
the shared golden fixtures (`../fixtures/distilled/cases.json`) and asserts the iOS
transform produces the committed output for **every** case — the cross-platform
consistency guarantee that keeps CLI, iOS, and Android byte-identical.

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
base64url padding restoration and `v>1` rejection), check the
`health.status → glyph` map (incl. the unknown-status fallback), and cover the
push-notification client logic (`NotificationsTests`: ntfy-topic derivation +
R37 payload → deep-link route, with a cross-contract block parsing payload
literals copied from the server's `tests/test_notify.py`).

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

## UI smoke suite (XCUITest, R84)

The unit suite above is pure-logic; it never launches the app. The **UI smoke
suite** (`RoostUITests/`, scheme `RoostUI`) closes that gap — it drives the real
app through the accessibility tree, so the tap-gated screens (New-session sheet,
Session, Notifications, Schedules) are actually exercised, not just their stores.
The flows:

1. **Pair via launch-arg → live dashboard.** Launches with `ROOST_PAIR_URI` (the
   same `roost://pair?d=…` code path as a scanned QR, no system dialog — see
   "Simulator demo / automation hook" below), then asserts the dashboard renders
   live data: the verdict bar (only present when `/derived` returned a verdict)
   and ≥1 run row from the seeded jobs.
2. **New-session sheet** opens; prompt field + Dispatch button assert.
3. **Open a Session** (deep-link via `ROOST_OPEN_SESSION=<job-id>`, or by tapping
   the first run row): the session header + Tree button assert, and the follow-up
   composer (R76) when the job is non-terminal.
4. **Notifications + Schedules sheets** open from the dashboard overflow menu.

Screenshots are attached to the result bundle at each step (kept on success).

### Running it (headless, on the fleet Mac)

XCUITest **does** run headless on the launchd Mac worker — unlike `screencapture`,
the simulator's automation bridge does not need a host window-server/AX session
(verified 2026-06-07 on mac-mini-m4: `** TEST EXECUTE SUCCEEDED **` from a Roost
job, no interactive login). The flows need a control plane with live-ish data;
**never point the UI tests at production** — stand up a scratch CP:

```sh
# 1. scratch 0.2.0 CP bound to the LAN + a worker + a couple of seeded jobs
roost serve --host 0.0.0.0 --port 8800 --token "$TOK" --db /tmp/scratch.db &
ROOST_URL=http://127.0.0.1:8800 ROOST_TOKEN=$TOK roost worker --name smoke &
echo '{"kind":"command","command":"sleep 600"}' | \
  ROOST_URL=http://127.0.0.1:8800 ROOST_TOKEN=$TOK roost submit - --detach
# 2. mint a pairing URI bound to the LAN address the simulator can reach
PAIR_URI=$(roost --url http://<LAN>:8800 pair --label smoke | sed -n 's/^pairing uri: //p')
```

`SmokeTests` reads `ROOST_PAIR_URI` from the **test runner's** process environment
and forwards it into `app.launchEnvironment`. **Without a pairing URI every flow
`XCTSkip`s** (a green run with skips, not a red one) — so a developer can run the
scheme from Xcode with no CP and not get spurious failures; only a real regression
turns it red.

> **Xcode 26 gotcha (verified 2026-06-07, the gating detail).** On Xcode 26.2,
> neither a plain `env ROOST_PAIR_URI=… xcodebuild` prefix **nor** the
> `TEST_RUNNER_ROOST_PAIR_URI=…` build-setting reaches the simulator-side test
> runner — `xcodebuild` accepts both but the runner's `ProcessInfo.environment`
> sees neither, so every flow silently `XCTSkip`s (a green-with-skips run that
> looks fine but tests nothing). The injection that actually works is to **patch
> the generated `.xctestrun`** and run `test-without-building` against it:

```sh
cd mobile-app/ios && xcodegen generate
SIM=$(xcrun simctl create "iPhone-17-Pro-mine" \
  com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro \
  "$(xcrun simctl list runtimes | grep -oE 'iOS-[0-9-]+' | tail -1 | sed 's/^/com.apple.CoreSimulator.SimRuntime./')")
DEST="platform=iOS Simulator,id=$SIM"

# build once, then patch the runner env into the .xctestrun
xcodebuild build-for-testing -project Roost.xcodeproj -scheme RoostUI \
  -destination "$DEST" -derivedDataPath DerivedData
XR=$(find DerivedData/Build/Products -name '*.xctestrun' | head -1)
PB=/usr/libexec/PlistBuddy
$PB -c "Add :RoostUITests:EnvironmentVariables:ROOST_PAIR_URI string $PAIR_URI" "$XR"
$PB -c "Add :RoostUITests:EnvironmentVariables:ROOST_OPEN_SESSION string <running-job-id>" "$XR"
# default is deleteOnSuccess — keep the screenshots even when the run is green
$PB -c "Set :RoostUITests:UserAttachmentLifetime keepAlways" "$XR"

xcodebuild test-without-building -xctestrun "$XR" -destination "$DEST" \
  -resultBundlePath ui.xcresult
```

Screenshots and the `.xcresult` are the artifacts. Pull the screenshots out with:

```sh
xcrun xcresulttool export attachments --path ui.xcresult --output-path ./shots
```

### Running it via a Roost job (the fleet path)

The home box can't reach the Mac's `localhost`, so stage the iOS source as a blob,
have the Mac fetch + build + test, and stage the result bundle + screenshots back:

```sh
tar czf ios-src.tgz -C mobile-app ios fixtures
GET=$(curl -s -X POST "$CP/blobs?name=ios-src.tgz" -H "Authorization: Bearer $ADMIN" \
      --data-binary @ios-src.tgz | jq -r .get_url)        # use the LAN host the Mac reaches
roost submit - <<YAML        # pin to the Mac; fetch, generate, build, patch, test
kind: command
target: mac-mini-m4
command: |
  curl -s -o /tmp/src.tgz '$GET' && mkdir -p /tmp/b && tar xzf /tmp/src.tgz -C /tmp/b
  cd /tmp/b/ios && /opt/homebrew/bin/xcodegen generate
  SIM=\$(xcrun simctl create "iPhone-17-Pro-mine" \\
    com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro \\
    "\$(xcrun simctl list runtimes | grep -oE 'iOS-[0-9-]+' | tail -1 | sed 's#^#com.apple.CoreSimulator.SimRuntime.#')")
  DEST="platform=iOS Simulator,id=\$SIM"
  xcodebuild build-for-testing -project Roost.xcodeproj -scheme RoostUI \\
    -destination "\$DEST" -derivedDataPath DerivedData
  XR=\$(find DerivedData/Build/Products -name '*.xctestrun' | head -1)
  /usr/libexec/PlistBuddy -c "Add :RoostUITests:EnvironmentVariables:ROOST_PAIR_URI string $PAIR_URI" "\$XR"
  xcodebuild test-without-building -xctestrun "\$XR" -destination "\$DEST" \\
    -resultBundlePath ui.xcresult
  # stage ui.xcresult + extracted screenshots back to \$CP/blobs (see scripts)
YAML
```

> **Simulator coordination:** create your own named simulator
> (`xcrun simctl create "iPhone-17-Pro-mine" <devtype> <runtime>`) and target it
> by UDID rather than sharing whatever is `Booted` — parallel jobs must not fight
> over one device. Delete it when done. (The home box can't reach the Mac's
> `localhost`; use the LAN host in the blob `get_url`.)

The accessibility identifiers the suite keys on are defined additively in the
views (`dashboard-list`, `run-row-<id>`, `verdict-bar`, `new-session-button`,
`new-session-prompt`/`-dispatch`/`-cancel`, `session-header`/`-tree`/`-cancel`,
`session-composer-field`/`-send`, `overflow-menu` + `overflow-{publish,
notifications,schedules,unpair}`, `notifications-topic-field`/`-done`,
`schedules-task-field`/`-done`).

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
| `Roost/Net/` | `ApiClient`, hand-rolled `SSEParser` + `LogStream` resume loop, Keychain, pairing decode, ANSI strip, **`DistilledLine` (R108) — the pure distilled-stream transform** |
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

The Models/Net parsing layer is pure Foundation, so the full pure layer (the
whole test suite minus the UI) runs on Linux via the recipe below — re-run it for
the live count:

```sh
# one-time: toolchain from swift.org into /tmp/swift-toolchain
mkdir -p /tmp/ios-linux-check/{Sources/Roost,Tests/RoostTests} && cd /tmp/ios-linux-check
# Package.swift: target Roost + testTarget RoostTests (see ../../mac-app/Package.swift for the pattern)
# symlink → Sources/Roost: all of Models/*.swift and Net/*.swift EXCEPT the three
#   Apple-only/Linux-incompatible files (see below). Excluding-by-name keeps this
#   recipe correct as the pure layer grows — new Foundation-only files are picked up.
# symlink → Tests/RoostTests: all of RoostTests/*.swift
ROOST_FIXTURES=$REPO/mobile-app/fixtures /tmp/swift-toolchain/usr/bin/swift test
```

Three `Net/` files are excluded from the Linux harness because they can't compile
under swift-corelibs-foundation:
- `PushService.swift` — UIKit-only (`#if canImport(UIKit)`); the routing it calls
  into lives in `Notifications.swift`, which is pure Foundation and covered.
- `Keychain.swift` — `import Security` (Apple-only framework).
- `LogStream.swift` — uses `URLSession.bytes`, unavailable on Linux Foundation.

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
