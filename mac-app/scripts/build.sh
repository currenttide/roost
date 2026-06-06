#!/usr/bin/env bash
# Build Roost.app from the SwiftPM package (no Xcode project needed).
#
#   ./scripts/build.sh                # release build → build/Roost.app
#   ./scripts/build.sh --install     # …and copy to /Applications
#   ./scripts/build.sh --universal   # arm64 + x86_64 fat binary
#
# Signing / notarization (optional, for distribution — DESIGN.md M3):
#   CODESIGN_IDENTITY="Developer ID Application: …" ./scripts/build.sh
#   NOTARY_PROFILE=<keychain-profile> …            # runs notarytool + staple
#
# Without CODESIGN_IDENTITY the bundle is ad-hoc signed, which is fine for
# a personal build on this machine.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$(uname)" != "Darwin" ]]; then
    echo "error: Roost.app must be built on macOS (this repo checkout also runs"
    echo "       'swift test' for RoostKit on Linux, but the app needs AppKit)." >&2
    exit 1
fi

ARCH_FLAGS=()
INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --universal) ARCH_FLAGS=(--arch arm64 --arch x86_64) ;;
        --install)   INSTALL=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

echo "==> swift build -c release ${ARCH_FLAGS[*]:-}"
swift build -c release ${ARCH_FLAGS[@]+"${ARCH_FLAGS[@]}"}

BIN="$(swift build -c release ${ARCH_FLAGS[@]+"${ARCH_FLAGS[@]}"} --show-bin-path)/RoostMac"
APP="build/Roost.app"

echo "==> assembling ${APP}"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/RoostMac"
cp Info.plist "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

IDENTITY="${CODESIGN_IDENTITY:--}"
echo "==> codesign (identity: ${IDENTITY})"
codesign --force --options runtime --sign "$IDENTITY" "$APP"

if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    echo "==> notarizing (profile: $NOTARY_PROFILE)"
    ditto -c -k --keepParent "$APP" build/Roost.zip
    xcrun notarytool submit build/Roost.zip --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP"
    rm -f build/Roost.zip
fi

if [[ "$INSTALL" == 1 ]]; then
    echo "==> installing to /Applications/Roost.app"
    rm -rf /Applications/Roost.app
    cp -R "$APP" /Applications/Roost.app
fi

du -sh "$APP" | awk '{print "==> done: " $2 " (" $1 ")"}'
