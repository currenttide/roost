import Foundation

/// Pure decision for the app's single-instance guard (R124). macOS normally
/// enforces one instance per bundle id when an .app is launched through
/// LaunchServices, but a copy of the bundle at a different path — or a direct
/// exec of the inner binary — slips through: same bundle id, second process,
/// two menu-bar birds fighting over one config. The fix is the standard seam:
/// on launch, look for a sibling via `NSRunningApplication`, activate it, and
/// quit. The AppKit glue lives in `AppDelegate`; the decision (which process
/// survives) lives here so the Linux harness covers it.
public enum SingleInstance {
    /// One running process carrying the app's bundle id, as reported by
    /// `NSRunningApplication` (`launchDate` is optional there, so it is here).
    public struct Instance: Equatable, Sendable {
        public let pid: Int32
        public let launchedAt: Date?

        public init(pid: Int32, launchedAt: Date? = nil) {
            self.pid = pid
            self.launchedAt = launchedAt
        }

        /// Seniority key: earliest launch first; an unknown launch date sorts
        /// OLDEST (the realistic nil is a long-running instance the system
        /// can't date, and the safe default for the "second launch" nit is to
        /// defer to it); pid breaks exact ties so raced launches still agree.
        var rank: (Double, Int32) {
            (launchedAt?.timeIntervalSinceReferenceDate ?? -.infinity, pid)
        }
    }

    /// Given this process's pid and ALL running instances carrying the app's
    /// bundle id (the `NSRunningApplication` query includes the current
    /// process), return the pid of the instance to yield to — or nil when this
    /// launch should proceed.
    ///
    /// The most senior instance wins: it was serving the menu bar first, and
    /// the user's intent in launching again is "show me Roost", not "give me
    /// two". Seniority (not "any sibling exists") matters for the raced case —
    /// two simultaneous launches each see the other, and a naive mutual yield
    /// would terminate BOTH; ranking by (launchDate, pid) makes every racer
    /// pick the same winner, so exactly one survives.
    public static func instanceToYieldTo(
        selfPID: Int32, instances: [Instance]
    ) -> Int32? {
        let others = instances.filter { $0.pid != selfPID }
        guard let senior = others.min(by: { $0.rank < $1.rank }) else {
            return nil  // sole instance — launch normally
        }
        let me = instances.first { $0.pid == selfPID } ?? Instance(pid: selfPID)
        return senior.rank < me.rank ? senior.pid : nil
    }
}
