#if os(macOS)
import AppKit
import SwiftUI

/// The independently-placeable windows of the multi-window app. Each archetype
/// is a single reused NSWindow a heavy user can park on its own monitor;
/// `runDetail` is the one kind that gets a fresh (cascaded) window per run.
enum WindowKind: Hashable {
    case workspace            // Runs master/detail — ⌘O / ⌘1 / Dock click
    case console              // persistent PTY — ⌘T / ⌘2
    case fleet                // Transfers · Publish · Schedules · Workers — ⌘3
    case runDetail(String)    // detachable run logs (associated: runID)
    case settings
    case onboarding
}

/// Lazily creates, caches, and routes the app's windows. Replaces the old
/// single-`mainWindow` model: a registry keyed by `WindowKind`, with per-window
/// view models it owns for the window's lifetime (redesign §Architecture).
///
/// Hosting note: every window is SwiftUI-in-`NSHostingController` EXCEPT the
/// Console, whose contentView is the raw, app-owned terminal NSView so the PTY
/// survives navigation and hide/show (the one place `NSViewRepresentable`'s
/// "I own this view" contract fights us).
@MainActor
final class WindowManager: NSObject, NSWindowDelegate {
    private let model: AppModel
    private var windows: [WindowKind: NSWindow] = [:]
    /// Per-window models (WorkspaceModel / FleetWindowModel / RunDetailModel),
    /// owned for the window's lifetime — NOT recreated on hide/show.
    private var models: [WindowKind: AnyObject] = [:]
    private var kindByWindow: [ObjectIdentifier: WindowKind] = [:]
    private var detailCascade = 0

    /// Recompute `store.uiVisible` whenever any window's visibility changes.
    var onVisibilityChange: (() -> Void)?

    init(model: AppModel) {
        self.model = model
    }

    /// True when any archetype window is on screen (drives poll cadence — a
    /// detached log window on a second monitor must keep the 2 s cadence).
    var anyWindowVisible: Bool {
        windows.values.contains { $0.isVisible }
    }

    // MARK: routing

    /// Show (creating if needed) and bring a window to the front.
    func open(_ kind: WindowKind) {
        let window = windows[kind] ?? create(kind)
        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
        onVisibilityChange?()
    }

    /// Focus an existing window without creating one (no-op if absent).
    func focus(_ kind: WindowKind) {
        guard let window = windows[kind] else { return }
        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
        onVisibilityChange?()
    }

    func close(_ kind: WindowKind) {
        windows[kind]?.close()
    }

    /// Deep-link to a run: select it in the Workspace, or tear off its own
    /// logs window (⌥-click / "Open in New Window").
    func openRun(_ runID: String, inNewWindow: Bool = false) {
        if inNewWindow {
            open(.runDetail(runID))
        } else {
            open(.workspace)
            (models[.workspace] as? WorkspaceModel)?.selectedRunID = runID
        }
    }

    func openFleet(_ section: FleetSection? = nil) {
        open(.fleet)
        if let section { (models[.fleet] as? FleetWindowModel)?.section = section }
    }

    /// The run selected in the key window, for the "Open in New Window" command.
    func selectedRunInKeyWindow() -> String? {
        guard let key = NSApp.keyWindow,
              let kind = kindByWindow[ObjectIdentifier(key)] else { return nil }
        switch kind {
        case .workspace: return (models[.workspace] as? WorkspaceModel)?.selectedRunID
        case .runDetail(let id): return id
        default: return nil
        }
    }

    // MARK: construction

    private func create(_ kind: WindowKind) -> NSWindow {
        let window: NSWindow
        switch kind {
        case .workspace:
            let wm = WorkspaceModel()
            models[kind] = wm
            window = host(WorkspaceWindowView().environment(model).environment(wm),
                          title: "Roost", size: NSSize(width: 980, height: 640),
                          min: NSSize(width: 720, height: 440),
                          autosave: "RoostWorkspaceWindow")

        case .fleet:
            let fm = FleetWindowModel()
            models[kind] = fm
            window = host(FleetWindowView().environment(model).environment(fm),
                          title: "Fleet", size: NSSize(width: 860, height: 600),
                          min: NSSize(width: 640, height: 420),
                          autosave: "RoostFleetWindow")

        case .console:
            window = makeWindow(title: "Console", size: NSSize(width: 820, height: 560),
                                min: NSSize(width: 520, height: 320),
                                autosave: "RoostConsoleWindow")
            window.contentView = model.console.contentView   // raw terminal — survives everything

        case .runDetail(let runID):
            let dm = RunDetailModel(runID: runID, store: model.store)
            dm.start()
            Task { await dm.loadSelfJob() }
            models[kind] = dm
            let title = model.store.run(id: runID)?.displayGoal ?? "Run"
            window = host(DetachedRunDetailView().environment(model).environment(dm),
                          title: title.isEmpty ? "Run" : title,
                          size: NSSize(width: 720, height: 640),
                          min: NSSize(width: 480, height: 400),
                          autosave: "RoostRunDetailWindow")
            // Shared autosave name + cascade so a second detached window doesn't
            // land exactly on the first (per-run autosave names would leak
            // UserDefaults entries unboundedly).
            detailCascade += 1
            window.cascadeTopLeft(from: NSPoint(x: 20 * CGFloat(detailCascade % 6),
                                                y: 20 * CGFloat(detailCascade % 6)))

        case .settings:
            window = host(SettingsView().environment(model),
                          title: "Roost Settings", size: nil, min: nil,
                          autosave: nil, style: [.titled, .closable])

        case .onboarding:
            window = host(OnboardingView().environment(model),
                          title: "Welcome to Roost", size: nil, min: nil,
                          autosave: nil, style: [.titled, .closable])
        }

        windows[kind] = window
        kindByWindow[ObjectIdentifier(window)] = kind
        window.delegate = self
        return window
    }

    /// A SwiftUI-hosted window.
    private func host(
        _ rootView: some View, title: String, size: NSSize?, min: NSSize?,
        autosave: String?, style: NSWindow.StyleMask = [.titled, .closable, .miniaturizable, .resizable]
    ) -> NSWindow {
        let window = NSWindow(contentViewController: NSHostingController(rootView: rootView))
        configure(window, title: title, size: size, min: min, autosave: autosave, style: style)
        return window
    }

    /// A bare window (contentView assigned by the caller — used for the Console).
    private func makeWindow(
        title: String, size: NSSize?, min: NSSize?, autosave: String?
    ) -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: size ?? NSSize(width: 800, height: 560)),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        configure(window, title: title, size: size, min: min, autosave: autosave,
                  style: [.titled, .closable, .miniaturizable, .resizable])
        return window
    }

    private func configure(
        _ window: NSWindow, title: String, size: NSSize?, min: NSSize?,
        autosave: String?, style: NSWindow.StyleMask
    ) {
        window.title = title
        window.styleMask = style
        window.isReleasedWhenClosed = false
        if let size { window.setContentSize(size) }
        if let min { window.minSize = min }
        if let autosave {
            window.setFrameAutosaveName(autosave)
            // Restore the user's last frame if one was saved; else center.
            if !window.setFrameUsingName(autosave) { window.center() }
        } else {
            window.center()
        }
    }

    // MARK: NSWindowDelegate — hide-vs-close policy

    /// Workspace / Fleet / Console hide (orderOut) instead of closing: they are
    /// cached, and the Console especially must keep its PTY alive. Detail /
    /// settings / onboarding close normally.
    nonisolated func windowShouldClose(_ sender: NSWindow) -> Bool {
        MainActor.assumeIsolated {
            guard let kind = kindByWindow[ObjectIdentifier(sender)] else { return true }
            switch kind {
            case .workspace, .fleet, .console:
                sender.orderOut(nil)
                onVisibilityChange?()
                return false
            case .runDetail, .settings, .onboarding:
                return true
            }
        }
    }

    nonisolated func windowWillClose(_ notification: Notification) {
        let window = notification.object as? NSWindow
        Task { @MainActor in self.handleWillClose(window) }
    }

    private func handleWillClose(_ window: NSWindow?) {
        defer { onVisibilityChange?() }
        guard let window, let kind = kindByWindow[ObjectIdentifier(window)] else { return }
        // runDetail is the one archetype we truly tear down — stop its SSE
        // stream deterministically and release the window.
        if case .runDetail = kind {
            (models[kind] as? RunDetailModel)?.stop()
            models[kind] = nil
            kindByWindow[ObjectIdentifier(window)] = nil
            windows[kind] = nil
        }
    }
}
#endif
