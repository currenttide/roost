#if os(macOS)
import AppKit
import RoostKit
import SwiftUI

/// Headless screenshot harness (test utility — not part of the user-facing
/// app). When the binary is launched with `ROOST_RENDER_DIR` set it does NOT
/// run the normal app: it connects to a live control plane, waits for one
/// real `GET /derived` snapshot, then renders the real SwiftUI windows to
/// PNGs by hosting them in an off-screen `NSHostingView` inside a real (but
/// never-shown) `NSWindow` and rasterizing via `cacheDisplay`.
///
/// Why it exists (R120): the fleet's Mac node runs headless under launchd —
/// zero displays, no Screen Recording/Automation TCC permissions, and those
/// permissions cannot be granted non-interactively. `screencapture` fails
/// outright there. `NSHostingView.cacheDisplay` renders the genuine AppKit
/// layer tree (ScrollViews, Tables, Menus, SF Symbols all rasterize
/// correctly, unlike `ImageRenderer`) and needs no display or TCC grant —
/// it is the only way to produce render evidence for mac-app PRs on the
/// fleet. See mac-app/README.md § "Render evidence" for how to run it via a
/// roost job (`scripts/render_shots.sh`).
///
/// Environment:
///   ROOST_RENDER_DIR    output directory for PNGs (required — gates the mode)
///   ROOST_RENDER_URL    control-plane base URL (live `GET /derived` source)
///   ROOST_RENDER_TOKEN  bearer token for that control plane
///   ROOST_RENDER_ONLY   render just one named view (the shell driver launches
///                       once per name so one hung/crashed view — e.g. a Form
///                       layout assertion in headless hosting — can't take
///                       down the rest)
@MainActor
enum RenderShots {
    /// Entry hook, called first thing from `RoostMain.main()`.
    /// Returns false (and does nothing) unless `ROOST_RENDER_DIR` is set.
    static func runIfRequested() -> Bool {
        let env = ProcessInfo.processInfo.environment
        guard let dir = env["ROOST_RENDER_DIR"] else { return false }
        // AppKit must be up (NSApplication) to host views, but stays headless.
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        try? FileManager.default.createDirectory(
            atPath: dir, withIntermediateDirectories: true)
        render(dir: dir,
               url: env["ROOST_RENDER_URL"] ?? "",
               token: env["ROOST_RENDER_TOKEN"])
        return true
    }

    private static func render(dir: String, url: String, token: String?) {
        let model = AppModel()
        if let connection = RoostConnection(urlString: url, token: token) {
            // configure() starts the real poll loop; pump the run loop until
            // the first live /derived snapshot lands (same data path the app
            // uses — no injection seam needed in FleetStore).
            model.store.configure(connection)
            pump(until: { model.store.snapshot != nil }, seconds: 10)
        }
        log("render: \(model.store.workers.count) workers, "
            + "\(model.store.snapshot?.runs.count ?? 0) runs in snapshot")

        let only = ProcessInfo.processInfo.environment["ROOST_RENDER_ONLY"]
        func want(_ n: String) -> Bool { only == nil || only == n }

        // The run the detail surfaces show: prefer a failed run (the richest
        // detail — diagnosis, tinted stderr), else anything.
        let detailRun = model.store.recentRuns.first { $0.state == "failed" }
            ?? model.store.activeRuns.first
            ?? model.store.recentRuns.first

        if want("popover") {
            shot(dir, "popover", 360, 560) { PopoverRootView().environment(model) }
        }
        if want("workspace") {
            let ws = WorkspaceModel()
            ws.selectedRunID = detailRun?.id
            shot(dir, "workspace", 980, 640) {
                WorkspaceWindowView().environment(model).environment(ws)
            }
        }
        for section in FleetSection.allCases {
            let name = "fleet-\(section.rawValue.lowercased())"
            if want(name) {
                let fleet = FleetWindowModel(section: section)
                shot(dir, name, 860, 600) {
                    FleetWindowView().environment(model).environment(fleet)
                }
            }
        }
        if want("run-detail"), let run = detailRun {
            shot(dir, "run-detail", 720, 640) {
                ScrollView { RunDetailView(runID: run.id, compact: false) }
                    .environment(model)
            }
        }
        if want("onboarding") {
            shot(dir, "onboarding", 460, 470) { OnboardingView().environment(model) }
        }
        if want("settings") {
            shot(dir, "settings", 440, 560) { SettingsView().environment(model) }
        }
        log("render: done")
    }

    /// Pump the main run loop until `done()` (or the deadline) — lets async
    /// Tasks (the poll loop, `.task` fetches, SSE) make progress headlessly.
    private static func pump(until done: () -> Bool, seconds: TimeInterval) {
        let deadline = Date().addingTimeInterval(seconds)
        while !done() && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
    }

    private static func shot<V: View>(
        _ dir: String, _ name: String, _ w: CGFloat, _ h: CGFloat,
        @ViewBuilder _ view: () -> V
    ) {
        let root = view()
            .frame(width: w, height: h)
            .background(Color(nsColor: .windowBackgroundColor))
        let hosting = NSHostingView(rootView: root)
        hosting.frame = NSRect(x: 0, y: 0, width: w, height: h)
        // A real (off-screen) window so layout, ScrollView content, Tables,
        // Menus and symbols all materialize the way they do on screen. Titled
        // (not borderless): NavigationSplitView only populates its sidebar
        // column inside a normal titled window.
        let window = NSWindow(
            contentRect: hosting.frame,
            styleMask: [.titled, .resizable], backing: .buffered, defer: false)
        window.contentView = hosting
        window.orderBack(nil)          // realized but never shown to a user
        window.displayIfNeeded()
        // Let layout + .task/.onAppear loads (pane fetches, log streams) settle.
        pump(until: { false }, seconds: 1.5)
        hosting.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        guard let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else {
            log("FAILED to alloc rep for \(name)"); return
        }
        hosting.cacheDisplay(in: hosting.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else {
            log("FAILED to encode \(name)"); return
        }
        let path = (dir as NSString).appendingPathComponent("\(name).png")
        try? png.write(to: URL(fileURLWithPath: path))
        log("wrote \(name).png (\(png.count) bytes, \(rep.pixelsWide)x\(rep.pixelsHigh))")
        window.orderOut(nil)
    }

    private static func log(_ s: String) {
        FileHandle.standardError.write(Data((s + "\n").utf8))
    }
}
#endif
