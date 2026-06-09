#if os(macOS)
import Foundation
import Observation
import RoostKit

/// Fleet-window segments (Workers is demoted here — DESIGN.md §2.4 / redesign).
enum FleetSection: String, CaseIterable, Identifiable {
    case transfers = "Transfers"
    case publish = "Publish"
    case schedules = "Schedules"
    case workers = "Workers"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .transfers: "arrow.up.arrow.down"
        case .publish: "globe"
        case .schedules: "clock.arrow.circlepath"
        case .workers: "server.rack"
        }
    }
}

/// Per-window state for the Workspace window (Runs). Lives for that window's
/// lifetime — each window owns its own selection so multiple monitors don't
/// fight over one global selection (redesign §Architecture).
@MainActor
@Observable
final class WorkspaceModel {
    var selectedRunID: String?
    var stateFilter = "all"   // all · active · failed
}

/// Per-window state for the Fleet window (which segment is showing).
@MainActor
@Observable
final class FleetWindowModel {
    var section: FleetSection = .transfers
    init(section: FleetSection = .transfers) { self.section = section }
}

/// Root object graph, owned by the AppDelegate and injected into every window.
/// Holds only SHARED fleet truth; per-window selection/section state lives in
/// WorkspaceModel / FleetWindowModel, owned by the window registry.
@MainActor
@Observable
final class AppModel {
    let settings: AppSettings
    let store: FleetStore
    let updates = UpdateChecker()
    @ObservationIgnored private(set) var transfers: TransferManager!
    @ObservationIgnored private(set) var console: ConsoleSession!

    /// The window registry / router, wired by the AppDelegate after creation.
    /// Views open windows through the thin façade methods below.
    @ObservationIgnored var router: WindowManager!

    init() {
        settings = AppSettings()
        store = FleetStore()
        if settings.hasCompletedOnboarding, let connection = settings.connection {
            store.configure(connection)
        }
        transfers = TransferManager(store: store)
        console = ConsoleSession(model: self)
    }

    // MARK: window façade (views call these; routing lives in WindowManager)

    func openWorkspace() { router.open(.workspace) }

    /// Focus the Workspace and select a run (deep links, popover taps).
    func openRun(_ runID: String, inNewWindow: Bool = false) {
        router.openRun(runID, inNewWindow: inNewWindow)
    }

    /// Open the Console window, optionally with a prepared prompt typed (not
    /// submitted) into the session.
    func openConsole(prompt: String? = nil) {
        if let prompt { console.queue(prompt: prompt) }
        router.open(.console)
    }

    func openFleet(_ section: FleetSection? = nil) {
        router.openFleet(section)
    }

    func openSettings() { router.open(.settings) }
    func openOnboarding() { router.open(.onboarding) }
    func dismissOnboarding() { router.close(.onboarding) }

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
