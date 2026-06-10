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

# Single-source the app version (R124): the canonical number is the repo's
# pyproject.toml [project].version — the exact value the control plane
# self-reports as __version__ (R32 precedent). The checked-in Info.plist
# carries a 0.0.0 placeholder; the real number is stamped HERE, at bundle
# assembly, because the app is SwiftPM-built (no Xcode project/agvtool to own
# versioning) and Info.plist is only ever consumed by this copy step — at
# runtime UpdateChecker and the About panel read CFBundleShortVersionString
# from the assembled bundle. Fails loudly rather than shipping the placeholder.
VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' ../pyproject.toml | head -1)"
if [[ -z "$VERSION" ]]; then
    echo "error: could not read [project].version from ../pyproject.toml" >&2
    exit 1
fi
echo "==> stamping version ${VERSION} (from pyproject.toml)"
/usr/libexec/PlistBuddy \
    -c "Set :CFBundleShortVersionString ${VERSION}" \
    -c "Set :CFBundleVersion ${VERSION}" \
    "$APP/Contents/Info.plist"

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
