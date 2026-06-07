import Foundation

/// R82: a signed relative-time formatter that renders both past and future
/// instants correctly. The old `Format.timeAgo` (in RoostMac) was past-tense
/// only — it clamped the interval to `max(0, now - epoch)`, so any FUTURE
/// timestamp collapsed to `0s`. The Transfers pane then string-swapped
/// "ago" → "from now", so every staged blob read "expires 0s from now" even
/// with hours of TTL left (see user-testing/mac-app/mainwindow-transfers.png).
///
/// This formatter is pure (`epoch` + injectable `now`) so the Linux harness
/// can pin both directions deterministically, mirroring the R81 pattern of
/// moving the decision into RoostKit and leaving the SwiftUI view a dumb
/// renderer. Foundation-only.
public enum RelativeTime {
    /// Format `epoch` (Unix seconds) relative to `now` (defaults to wall clock).
    /// Past instants read "Xs/m/h/d ago"; future instants read
    /// "in Xs/m/h/d"; `nil`/non-positive epochs read "—". The magnitude buckets
    /// match the old `timeAgo` exactly (s < 60, m < 3600, h < 86400, else d), so
    /// past-tense output is unchanged for existing call sites — except sub-second
    /// instants, which now read "now" instead of "0s ago" (an intentional, minor
    /// improvement, applied consistently in both directions).
    public static func signed(_ epoch: Double?, now: Double = Date().timeIntervalSince1970)
        -> String
    {
        guard let epoch, epoch > 0 else { return "—" }
        let delta = epoch - now
        let magnitude = abs(delta)
        // Within the sub-second floor, treat as "now" in either direction rather
        // than "0s ago"/"in 0s", which read as a bug to users.
        if magnitude < 1 { return "now" }
        let unit = bucket(magnitude)
        return delta >= 0 ? "in \(unit)" : "\(unit) ago"
    }

    /// Convenience alias for past-only call sites that want the historic phrasing.
    /// Identical output to the old `Format.timeAgo` for past instants.
    public static func ago(_ epoch: Double?, now: Double = Date().timeIntervalSince1970)
        -> String
    {
        signed(epoch, now: now)
    }

    /// The bare "Xs/Xm/Xh/Xd" magnitude string with no direction.
    private static func bucket(_ seconds: Double) -> String {
        switch seconds {
        case ..<60: return "\(Int(seconds))s"
        case ..<3600: return "\(Int(seconds / 60))m"
        case ..<86_400: return "\(Int(seconds / 3600))h"
        default: return "\(Int(seconds / 86_400))d"
        }
    }
}
