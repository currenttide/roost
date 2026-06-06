#if os(macOS)
import Foundation
import Observation
import RoostKit

/// Main-window sidebar sections.
enum MainSection: String, CaseIterable, Identifiable {
    case runs = "Runs"
    case workers = "Workers"
    case console = "Console"
    case transfers = "Transfers"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .runs: "play.circle"
        case .workers: "server.rack"
        case .console: "terminal"
        case .transfers: "arrow.up.arrow.down.circle"
        }
    }
}

/// Root object graph, owned by the AppDelegate and injected into every view.
@MainActor
@Observable
final class AppModel {
    let settings: AppSettings
    let store: FleetStore
    let updates = UpdateChecker()
    @ObservationIgnored private(set) var transfers: TransferManager!
    @ObservationIgnored private(set) var console: ConsoleSession!

    /// Deep-link target: set when a notification or popover row asks the main
    /// window to show a specific run.
    var selectedRunID: String?

    /// Which main-window section is showing; deep links steer this.
    var mainSection: MainSection = .runs

    // Window-opening hooks, filled in by the AppDelegate so SwiftUI views can
    // reach AppKit windows without knowing about them.
    @ObservationIgnored var openMainWindow: ((String?) -> Void)?
    @ObservationIgnored var openSettingsWindow: (() -> Void)?
    @ObservationIgnored var openOnboardingWindow: (() -> Void)?

    init() {
        settings = AppSettings()
        store = FleetStore()
        if settings.hasCompletedOnboarding, let connection = settings.connection {
            store.configure(connection)
        }
        transfers = TransferManager(store: store)
        console = ConsoleSession(model: self)
    }

    /// Open the main window at the Console, optionally with a prepared prompt
    /// typed (not submitted) into the session.
    func openConsole(prompt: String? = nil) {
        if let prompt { console.queue(prompt: prompt) }
        mainSection = .console
        openMainWindow?(nil)
    }

    /// (Re)connect after onboarding or a settings change.
    func applyConnection(urlString: String, token: String?) {
        settings.urlString = urlString
        settings.token = token
        settings.hasCompletedOnboarding = true
        store.configure(settings.connection)
    }
}

/// Re-runs `work` whenever any @Observable state it reads changes —
/// the AppKit-side bridge for the status icon and window titles.
@MainActor
func continuouslyTrack(_ work: @escaping @MainActor () -> Void) {
    withObservationTracking {
        work()
    } onChange: {
        Task { @MainActor in continuouslyTrack(work) }
    }
}
#endif
