# Roost for Mac

**Your fleet in the menu bar.** A native, zero-dependency macOS app over the Roost
control plane: glance at the bird for fleet health, hit ⌥⌘R, type a goal, press ⏎.
Run cards show phase / narration / progress live; notifications tell you when work
is verified or failed (with the worker's diagnosis attached).

Full design rationale: [DESIGN.md](DESIGN.md).

## Build & install (on a Mac)

```bash
cd mac-app
./scripts/build.sh --install        # → /Applications/Roost.app
```

Requires Xcode 15.4+ command line tools (Swift 5.10) and macOS 14+.
No third-party dependencies — `swift build` is the whole story; the script
just assembles the `.app` bundle and (ad-hoc) signs it.

For distribution: set `CODESIGN_IDENTITY` (Developer ID) and optionally
`NOTARY_PROFILE` (a `notarytool` keychain profile) — see `scripts/build.sh`.

## First run

The app auto-detects `~/.config/roost/config.toml` if you already use the
`roost` CLI on this Mac — one click and you're connected. Otherwise paste a
control-plane URL + token, or follow the two-line `roost up` quickstart.
The token is stored in the Keychain, never on disk.

## Layout

```
mac-app/
  DESIGN.md             the design document this app implements
  Package.swift         SwiftPM package (no Xcode project)
  Sources/RoostKit/     API client + models — UI-free, builds & tests on Linux too
  Sources/RoostMac/     the app: AppKit shell + SwiftUI views
  Tests/RoostKitTests/  fixture-pinned decoding, SSE parser, config tests
  Info.plist            bundle manifest (LSUIElement menu bar app)
  scripts/build.sh      build → assemble → sign → (optionally) notarize
```

## Development

```bash
swift test          # RoostKit tests — also runs on Linux
swift run RoostMac  # dev run (note: notifications need the real .app bundle)
open Package.swift  # or open the folder in Xcode and run the RoostMac scheme
```

The app is a thin, mechanical client (DESIGN.md §1): everything renders from
`GET /derived`, logs stream over SSE from `GET /jobs/{id}/stream`, and the
only writes are `POST /jobs` and `DELETE /jobs/{id}`. Judgment stays in the
agents on the fleet.

## Verifying mac-app changes (the Linux gate cannot)

`swift test` on Linux exercises **RoostKit only**. The `RoostMac` target is
entirely `#if os(macOS)` and imports `AppKit` / `SwiftUI` / `Carbon` /
`ServiceManagement` — frameworks that **do not exist on Linux** — so on Linux
the app target compiles to nothing. A Linux green run therefore says nothing
about whether the app itself compiles. (R73: a `? .secondary : .red` ternary
that mixes `HierarchicalShapeStyle` and `Color` shipped to master green because
the Linux gate never type-checked the view code.)

**Rule: any PR touching `mac-app/Sources/RoostMac/**` must be built on macOS
before claiming the app compiles.** Two ways, either suffices:

- **CI** — `.github/workflows/mac-app.yml`'s `app-macos` job runs `swift build`
  + `swift test` + `./scripts/build.sh` on a `macos-14` runner. This is the
  source of truth on every mac-app PR.
- **Mac node (loop / offline)** — build on the fleet's Mac node, e.g.
  `roost exec mac-mini-m4 'cd <checkout>/mac-app && swift build 2>&1 | tail'`
  and capture `Compiling`/`Build complete!` (or `swift test`). The build-log
  tail is the artifact for a compile-only fix.

Until one of those is green, a mac-app change is at most
"compiles on Linux (RoostKit only), needs-mac-verify" — never "the app builds".
