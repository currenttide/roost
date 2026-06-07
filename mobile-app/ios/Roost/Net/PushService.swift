#if canImport(UIKit)
import Foundation
import UIKit

/// DEVICE-ONLY half of push notifications (R37 / DESIGN.md §6 v1.1). UNTESTED on
/// the Linux harness — there is no device, no notification center, and no ntfy
/// subscription here. The PURE routing/topic logic it leans on (`NotifyRouter`,
/// `NtfyTopic`) IS Linux-tested; this file is the thin, obvious binding around it.
///
/// DESIGN.md picks "ntfy.sh self-hosted or UnifiedPush-style webhooks" over APNs
/// to stay dependency-light. iOS has no UnifiedPush distributor (that's an Android
/// concept), so the honest v1.1 iOS path is:
///   - the user pastes their ntfy topic (a SETTING — `NtfyTopic`), and
///   - they subscribe to it in the ntfy iOS app, whose notifications already
///     deep-link via the `click` header the server could add later, OR
///   - a future build wires APNs (needs an Apple paid account + a push relay),
///     deliberately deferred.
///
/// What this type CAN do without extra infra: when the app itself is foregrounded
/// and learns a job went terminal, post a LOCAL notification whose tap routes via
/// `NotifyRouter`. That keeps the tap→Session deep link real and exercised in the
/// app even before remote push is provisioned. Remote (background) delivery is the
/// capped, device-only piece.
@MainActor
final class PushService: NSObject, ObservableObject {
    /// Set by the app's notification-center delegate when a notification is
    /// tapped; the root view observes it and navigates. Kept here (not buried in
    /// AppState) so the routing seam is obvious and the pure router is the only
    /// decision-maker.
    @Published var pendingRoute: NotifyRoute?

    /// Post a local notification for a terminal job we observed in-app. The
    /// payload mirrors the server's R37 body so the SAME `NotifyRouter` handles
    /// local and (future) remote notifications identically.
    func postLocalTerminal(jobId: String, state: String, intent: String) {
        let content = UNMutableNotificationContent()
        content.title = "Roost job \(jobId) \(state)"
        content.body = "\(state): \(intent)"
        content.sound = .default
        // The userInfo IS the R37 payload shape, so taps route through the same
        // pure logic the cross-contract test pins.
        content.userInfo = ["event": "job_terminal", "job_id": jobId,
                            "state": state, "intent": intent]
        let req = UNNotificationRequest(
            identifier: "roost-\(jobId)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    /// Ask for notification permission (local notifications need it too). The
    /// result is best-effort; the app degrades to foreground SSE if denied.
    func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]) { _, _ in }
    }

    /// Resolve a tapped notification's userInfo into a route via the pure router.
    /// Called from the `UNUserNotificationCenterDelegate` (wired at app launch).
    func handleTap(userInfo: [AnyHashable: Any]) {
        // Re-serialize the userInfo to JSON so the ONE decoder
        // (`NotifyRouter.decode`) is the single source of routing truth.
        if let data = try? JSONSerialization.data(withJSONObject: userInfo),
           let payload = NotifyRouter.decode(data) {
            pendingRoute = NotifyRouter.route(payload)
        } else {
            pendingRoute = .dashboard
        }
    }
}
#endif
