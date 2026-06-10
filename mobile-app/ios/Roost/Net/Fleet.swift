import Foundation

/// Pure presentation layer for the Fleet screen (R121, API.md §2a): status
/// pills, capability summaries, load and last-seen lines, and the display
/// sort. Foundation-only so the Linux harness exercises it, and an exact
/// mirror of the Android `Fleet` object — `FleetTests` /  `FleetTest.kt`
/// assert the same example strings on both platforms so the two phones can
/// never describe the same fleet differently.
enum Fleet {

    // MARK: Staleness (the R75 pattern, API.md §2a)

    /// Mirror of the server's read-time thresholds (`roost/server.py`
    /// STALE_AFTER / OFFLINE_AFTER): a heartbeat gap ≥ 45 s reads stale,
    /// ≥ 120 s reads offline. The client re-applies them against its own
    /// ticker-driven wall clock so a node that dies while a payload sits in
    /// hand degrades honestly on screen instead of staying green.
    static let staleAfter: Double = 45
    static let offlineAfter: Double = 120

    /// The row's warning pill, or nil when the node is fresh.
    enum Pill: String {
        case stale, offline
    }

    /// Decide the pill for a worker row (API.md §2a):
    /// 1. the server's word wins in the offline direction (`status: "offline"`
    ///    is offline however fresh the payload);
    /// 2. otherwise a client-clock heartbeat gap ≥ 120 s is offline;
    /// 3. `status: "stale"` or a gap ≥ 45 s is stale;
    /// 4. else no pill.
    static func pill(status: String, lastSeen: Double?, now: Double) -> Pill? {
        if status == "offline" { return .offline }
        let gap = lastSeen.map { now - $0 }
        if let gap, gap >= offlineAfter { return .offline }
        if status == "stale" { return .stale }
        if let gap, gap >= staleAfter { return .stale }
        return nil
    }

    /// A worker counts as UP only when the server says live (idle|busy) AND
    /// the client clock agrees (no stale/offline pill). Unknown statuses
    /// render as text but never count as up (API.md §9).
    static func isUp(status: String, lastSeen: Double?, now: Double) -> Bool {
        (status == "idle" || status == "busy")
            && pill(status: status, lastSeen: lastSeen, now: now) == nil
    }

    /// The screen headline: "3 of 4 up".
    static func headline(up: Int, total: Int) -> String {
        "\(up) of \(total) up"
    }

    // MARK: Capability summary (API.md §2a)

    /// One glanceable line from the free-form capability map, mirroring the
    /// `roost workers` CLI summary: hostname, GPU (VRAM, or the R41
    /// "detection failed" flag — a broken probe is NOT a bare node), arch,
    /// CPU count. Unknown keys are ignored (additive contract); nil when
    /// nothing summarizable is present.
    static func capsSummary(_ caps: [String: JSONValue]?) -> String? {
        guard let caps else { return nil }
        var parts: [String] = []
        if case .string(let host)? = caps["hostname"] { parts.append(host) }
        if case .string("failed")? = caps["gpu_detection"] {
            parts.append("gpu: detection failed")
        } else if case .number(let vram)? = caps["gpu_vram_gb"] {
            parts.append("gpu \(num(vram))GB")
        }
        if case .string(let arch)? = caps["arch"] { parts.append(arch) }
        if case .number(let cpus)? = caps["cpus"] { parts.append("\(num(cpus)) cpu") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// Integral numbers drop the decimal ("16"), fractional keep it ("30.7") —
    /// identical formatting on both platforms.
    private static func num(_ v: Double) -> String {
        v == v.rounded() ? String(Int(v)) : String(v)
    }

    // MARK: Load + last-seen lines

    /// "1/4 running" — in-flight jobs against capacity (capacity ≥ 1 always;
    /// older CPs may omit either field → honest zeros).
    static func loadText(running: Int?, capacity: Int?) -> String {
        "\(max(running ?? 0, 0))/\(max(capacity ?? 1, 1)) running"
    }

    /// Compact relative heartbeat age: "just now" (< 5 s), then s/m/h/d.
    /// Driven by a 1 s ticker in the view (R75: the clock must keep advancing
    /// even when no new payload arrives).
    static func lastSeenText(_ lastSeen: Double?, now: Double) -> String {
        guard let seen = lastSeen else { return "never seen" }
        let gap = max(now - seen, 0)
        if gap < 5 { return "just now" }
        if gap < 60 { return "seen \(Int(gap))s ago" }
        if gap < 3600 { return "seen \(Int(gap / 60))m ago" }
        if gap < 86400 { return "seen \(Int(gap / 3600))h ago" }
        return "seen \(Int(gap / 86400))d ago"
    }

    // MARK: Display sort

    /// Working nodes first, dead ones last, alphabetical within a band:
    /// busy(0) < idle(1) < stale(2) < unknown(3) < offline(4).
    static func rank(_ status: String) -> Int {
        switch status {
        case "busy": return 0
        case "idle": return 1
        case "stale": return 2
        case "offline": return 4
        default: return 3
        }
    }

    /// Sort for the Fleet list: rank, then case-insensitive name, then id
    /// (a total order, so rows never jump between equal polls).
    static func sorted(_ workers: [Worker]) -> [Worker] {
        workers.sorted { a, b in
            let ra = rank(a.status), rb = rank(b.status)
            if ra != rb { return ra < rb }
            let na = a.displayName.lowercased(), nb = b.displayName.lowercased()
            if na != nb { return na < nb }
            return a.id < b.id
        }
    }
}
