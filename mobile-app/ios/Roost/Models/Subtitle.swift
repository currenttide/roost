import Foundation

/// The job-kind segment of a session/dashboard subtitle (R85).
///
/// Pure (no SwiftUI/URLSession) so it is the single source of truth for the
/// segment, shared by `DashboardView.meta` and `SessionView.headerMeta` and unit-
/// testable. The server now reports a job's effective kind on every run row as
/// `kind` (API.md §2: a `command` job is "command", not "claude").
///
/// Honesty: an older control plane omits `kind` (nil) — the segment is then
/// dropped rather than guessed. Android hardcoded "claude" (wrong for command
/// jobs); iOS showed no kind at all — both now show the truthful kind when known.
enum Subtitle {
    /// The kind label to show, or nil when unknown (omit the segment).
    static func kindSegment(_ kind: String?) -> String? {
        guard let k = kind?.trimmingCharacters(in: .whitespaces), !k.isEmpty else {
            return nil
        }
        return k
    }
}
