#if os(macOS)
import AppKit
import SwiftUI

/// Lazily creates and reuses the app's windows (main, onboarding, settings).
/// Windows are plain NSWindows hosting SwiftUI — one codebase of views, two
/// presentations (DESIGN.md §2).
@MainActor
final class WindowManager: NSObject, NSWindowDelegate {
    private let model: AppModel
    private var mainWindow: NSWindow?
    private var onboardingWindow: NSWindow?
    private var settingsWindow: NSWindow?

    var onVisibilityChange: (() -> Void)?

    init(model: AppModel) {
        self.model = model
    }

    var isMainWindowVisible: Bool {
        mainWindow?.isVisible ?? false
    }

    // MARK: main window

    func showMain(selecting runID: String? = nil) {
        if let runID {
            model.selectedRunID = runID
            model.mainSection = .runs
        }
        if mainWindow == nil {
            let host = NSHostingController(
                rootView: MainWindowView().environment(model))
            let window = NSWindow(contentViewController: host)
            window.title = "Roost"
            window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            window.setContentSize(NSSize(width: 920, height: 600))
            window.minSize = NSSize(width: 720, height: 420)
            window.setFrameAutosaveName("RoostMainWindow")
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.center()
            mainWindow = window
        }
        NSApp.activate()
        mainWindow?.makeKeyAndOrderFront(nil)
        onVisibilityChange?()
    }

    // MARK: onboarding

    func showOnboarding() {
        if onboardingWindow == nil {
            let host = NSHostingController(
                rootView: OnboardingView().environment(model))
            let window = NSWindow(contentViewController: host)
            window.title = "Welcome to Roost"
            window.styleMask = [.titled, .closable]
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.center()
            onboardingWindow = window
        }
        NSApp.activate()
        onboardingWindow?.makeKeyAndOrderFront(nil)
    }

    func closeOnboarding() {
        onboardingWindow?.close()
    }

    // MARK: settings

    func showSettings() {
        if settingsWindow == nil {
            let host = NSHostingController(
                rootView: SettingsView().environment(model))
            let window = NSWindow(contentViewController: host)
            window.title = "Roost Settings"
            window.styleMask = [.titled, .closable]
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.center()
            settingsWindow = window
        }
        NSApp.activate()
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    // MARK: NSWindowDelegate

    nonisolated func windowWillClose(_ notification: Notification) {
        Task { @MainActor in self.onVisibilityChange?() }
    }
}
#endif
