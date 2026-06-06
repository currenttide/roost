#if os(macOS)
import Foundation
import RoostKit
import UserNotifications

/// Posts user notifications from FleetStore diffs (DESIGN.md §5) and routes
/// notification clicks to the run they describe.
@MainActor
final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    private let settings: AppSettings
    private var authorized = false
    /// Notifications need a real app bundle; `swift run` from a bare binary
    /// would crash UNUserNotificationCenter, so degrade silently there.
    private let available = Bundle.main.bundleIdentifier != nil

    /// Set by the AppDelegate; called with a run id when a notification is clicked.
    var openRun: ((String) -> Void)?

    init(settings: AppSettings) {
        self.settings = settings
        super.init()
        guard available else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { [weak self] granted, _ in
            Task { @MainActor in self?.authorized = granted }
        }
    }

    func handle(_ diff: FleetDiff) {
        guard available, authorized else { return }

        if settings.notifyTerminal {
            for run in diff.finishedRuns {
                post(id: "run-\(run.id)",
                     title: terminalTitle(for: run),
                     body: terminalBody(for: run),
                     runID: run.id)
            }
        }
        if settings.notifyStuck {
            for run in diff.becameStuck {
                post(id: "stuck-\(run.id)",
                     title: "Run may be stuck",
                     body: run.goal,
                     runID: run.id)
            }
        }
        if settings.notifyFleetAlert, let verdict = diff.verdictBecameAlert {
            post(id: "fleet-alert", title: "Fleet alert", body: verdict.summary, runID: nil)
        }
        if settings.notifyWorkerOffline {
            for worker in diff.workersWentOffline {
                post(id: "offline-\(worker.id)",
                     title: "Worker went offline",
                     body: worker.name,
                     runID: nil)
            }
        }
    }

    private func terminalTitle(for run: Run) -> String {
        switch run.health.status {
        case "verified": return "✓ Verified"
        case "unverified": return "✓ Done (not verified)"
        case "done": return "✓ Done"
        case "cancelled": return "⊘ Cancelled"
        default: return "✗ Failed"
        }
    }

    private func terminalBody(for run: Run) -> String {
        if run.state == "failed", let diagnosis = run.diagnosis, !diagnosis.isEmpty {
            return "\(run.goal) — \(diagnosis)"
        }
        return run.goal
    }

    private func post(id: String, title: String, body: String, runID: String?) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        if let runID { content.userInfo = ["run_id": runID] }
        let request = UNNotificationRequest(
            identifier: id, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    // MARK: UNUserNotificationCenterDelegate

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let runID = response.notification.request.content.userInfo["run_id"] as? String
        Task { @MainActor in
            if let runID { self.openRun?(runID) }
            completionHandler()
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner])
    }
}
#endif
