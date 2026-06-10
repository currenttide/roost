#if os(macOS)
import AppKit
import RoostKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    private var model: AppModel!
    private var statusItem: StatusItemController!
    private var windows: WindowManager!
    private var notifications: NotificationManager!
    private var hotkey: HotkeyManager!
    private var hotkeyWasEnabled: Bool?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Single-instance guard (R124): a second launch should focus the running
        // Roost, not start a second menu-bar bird. LaunchServices already
        // enforces this for re-opens of the SAME .app bundle, but a copy at
        // another path (or a directly exec'd binary) slips through. The decision
        // (which process survives — seniority by launch date, raced launches
        // converge on one winner) is RoostKit's `SingleInstance`, Linux-tested;
        // only the AppKit glue lives here. Dev runs (`swift run`) have no bundle
        // identifier and skip the guard. Runs before any state is built so the
        // yielding process touches nothing.
        if let bundleID = Bundle.main.bundleIdentifier {
            let peers = NSRunningApplication
                .runningApplications(withBundleIdentifier: bundleID)
                .filter { !$0.isTerminated }
            if let winner = SingleInstance.instanceToYieldTo(
                selfPID: ProcessInfo.processInfo.processIdentifier,
                instances: peers.map {
                    .init(pid: $0.processIdentifier, launchedAt: $0.launchDate)
                }) {
                peers.first { $0.processIdentifier == winner }?
                    .activate(options: [.activateAllWindows])
                NSApp.terminate(nil)
                return
            }
        }

        model = AppModel()
        NSApp.setActivationPolicy(model.settings.showDockIcon ? .regular : .accessory)
        buildMainMenu()

        windows = WindowManager(model: model)
        model.router = windows
        statusItem = StatusItemController(model: model)
        notifications = NotificationManager(settings: model.settings)
        hotkey = HotkeyManager { [weak self] in
            self?.statusItem.showPopover()
        }

        // Poll cadence follows the popover OR any archetype window being visible
        // (a detached run-detail window on a second monitor must keep 2 s).
        let recomputeVisibility = { [weak self] in
            guard let self else { return }
            self.model.store.uiVisible =
                self.statusItem.isPopoverShown || self.windows.anyWindowVisible
        }
        statusItem.onVisibilityChange = recomputeVisibility
        windows.onVisibilityChange = recomputeVisibility

        model.store.onDiff = { [weak self] diff in
            self?.notifications.handle(diff)
        }
        notifications.openRun = { [weak self] runID in
            self?.model.openRun(runID)
        }

        // react to settings toggles (hotkey on/off, poll cadence)
        continuouslyTrack { [weak self] in
            guard let self else { return }
            let enabled = self.model.settings.hotkeyEnabled
            self.model.store.visibleCadence = self.model.settings.visibleCadence
            guard enabled != self.hotkeyWasEnabled else { return }
            self.hotkeyWasEnabled = enabled
            if enabled { self.hotkey.enable() } else { self.hotkey.disable() }
        }

        model.store.start()
        Task { await self.model.updates.check() }  // silent daily, if configured

        if !model.settings.hasCompletedOnboarding {
            windows.open(.onboarding)
        }
    }

    /// Kill the Console PTY on quit so no orphaned agent is left behind.
    func applicationWillTerminate(_ notification: Notification) {
        model?.console.shutdown()
    }

    /// Dock-icon mode: clicking the Dock icon opens the Workspace.
    func applicationShouldHandleReopen(
        _ sender: NSApplication, hasVisibleWindows flag: Bool
    ) -> Bool {
        if !flag { windows.open(.workspace) }
        return true
    }

    // MARK: main menu
    // A full menu so standard shortcuts work everywhere (⌘C/⌘V, ⌘W, ⌘Q) and the
    // multi-window app gets a proper Window menu — LSUIElement apps get none free.

    private func buildMainMenu() {
        let main = NSMenu()

        // App menu
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Roost",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Settings…",
                        action: #selector(openSettingsAction), keyEquivalent: ",")
            .target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Roost",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        // File menu — the window-opening verbs
        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(withTitle: "Open Roost",
                         action: #selector(openWorkspaceAction), keyEquivalent: "o").target = self
        fileMenu.addItem(withTitle: "Console",
                         action: #selector(openConsoleAction), keyEquivalent: "t").target = self
        fileMenu.addItem(withTitle: "Fleet",
                         action: #selector(openFleetAction), keyEquivalent: "").target = self
        fileMenu.addItem(.separator())
        let newWin = fileMenu.addItem(
            withTitle: "Open Run in New Window",
            action: #selector(openRunInNewWindowAction), keyEquivalent: "O")
        newWin.keyEquivalentModifierMask = [.command, .shift]
        newWin.target = self
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "Close Window",
                         action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

        // Edit menu
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All",
                         action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        // Window menu — AppKit appends the live window list once windowsMenu is set
        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Minimize",
                           action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Zoom",
                           action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowMenu.addItem(.separator())
        windowMenu.addItem(withTitle: "Roost",
                           action: #selector(openWorkspaceAction), keyEquivalent: "1").target = self
        windowMenu.addItem(withTitle: "Console",
                           action: #selector(openConsoleAction), keyEquivalent: "2").target = self
        windowMenu.addItem(withTitle: "Fleet",
                           action: #selector(openFleetAction), keyEquivalent: "3").target = self
        windowMenu.addItem(.separator())
        windowMenu.addItem(withTitle: "Bring All to Front",
                           action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: "")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }

    // MARK: menu validation

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(openRunInNewWindowAction) {
            return windows.selectedRunInKeyWindow() != nil
        }
        return true
    }

    @objc private func openWorkspaceAction() { model.openWorkspace() }
    @objc private func openConsoleAction() { model.openConsole() }
    @objc private func openFleetAction() { model.openFleet() }
    @objc private func openSettingsAction() { model.openSettings() }
    @objc private func openRunInNewWindowAction() {
        if let runID = windows.selectedRunInKeyWindow() {
            model.openRun(runID, inNewWindow: true)
        }
    }
}
#endif
