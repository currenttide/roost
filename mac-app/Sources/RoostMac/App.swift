// Roost for Mac — entry point.
//
// Classic AppKit shell with SwiftUI content: an AppDelegate owns the status
// item, popover, and windows (full programmatic control over the menu bar
// icon and hotkey-driven popover, which MenuBarExtra doesn't give us).
// An explicit @main (rather than main.swift top-level code) so the setup is
// main-actor isolated under Swift 6 toolchains.
// On non-macOS platforms this target compiles to a stub so `swift test`
// can exercise RoostKit anywhere (e.g. CI on Linux).

#if os(macOS)
import AppKit

@main
@MainActor
enum RoostMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
#else
@main
enum RoostMain {
    static func main() {
        print("RoostMac is a macOS app; this platform builds RoostKit only.")
    }
}
#endif
