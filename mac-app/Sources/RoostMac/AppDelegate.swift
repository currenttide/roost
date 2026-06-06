#if os(macOS)
import AppKit
import RoostKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var model: AppModel!
    private var statusItem: StatusItemController!
    private var windows: WindowManager!
    private var notifications: NotificationManager!
    private var hotkey: HotkeyManager!
    private var hotkeyWasEnabled: Bool?

    func applicationDidFinishLaunching(_ notification: Notification) {
        model = AppModel()
        NSApp.setActivationPolicy(model.settings.showDockIcon ? .regular : .accessory)
        buildMainMenu()

        windows = WindowManager(model: model)
        statusItem = StatusItemController(model: model)
        notifications = NotificationManager(settings: model.settings)
        hotkey = HotkeyManager { [weak self] in
            self?.statusItem.showPopover()
        }

        // wiring
        let recomputeVisibility = { [weak self] in
            guard let self else { return }
            self.model.store.uiVisible =
                self.statusItem.isPopoverShown || self.windows.isMainWindowVisible
        }
        statusItem.onVisibilityChange = recomputeVisibility
        windows.onVisibilityChange = recomputeVisibility

        model.store.onDiff = { [weak self] diff in
            self?.notifications.handle(diff)
        }
        notifications.openRun = { [weak self] runID in
            self?.windows.showMain(selecting: runID)
        }
        model.openMainWindow = { [weak self] runID in
            self?.windows.showMain(selecting: runID)
        }
        model.openSettingsWindow = { [weak self] in
            self?.windows.showSettings()
        }
        model.openOnboardingWindow = { [weak self] in
            self?.windows.showOnboarding()
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
            windows.showOnboarding()
        }
    }

    /// Dock-icon mode: clicking the Dock icon opens the main window.
    func applicationShouldHandleReopen(
        _ sender: NSApplication, hasVisibleWindows flag: Bool
    ) -> Bool {
        if !flag { windows.showMain() }
        return true
    }

    // MARK: main menu
    // A minimal menu so standard shortcuts work everywhere (⌘C/⌘V in the
    // token field, ⌘W, ⌘Q) — LSUIElement apps get none for free.

    private func buildMainMenu() {
        let main = NSMenu()

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

        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(withTitle: "Open Roost",
                         action: #selector(openMainAction), keyEquivalent: "o")
            .target = self
        fileMenu.addItem(withTitle: "Console",
                         action: #selector(openConsoleAction), keyEquivalent: "t")
            .target = self
        fileMenu.addItem(withTitle: "Close Window",
                         action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

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

        NSApp.mainMenu = main
    }

    @objc private func openMainAction() {
        windows.showMain()
    }

    @objc private func openConsoleAction() {
        model.openConsole()
    }

    @objc private func openSettingsAction() {
        windows.showSettings()
    }
}
#endif
