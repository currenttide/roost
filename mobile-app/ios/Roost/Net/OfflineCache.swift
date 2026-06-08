import Foundation

/// On-device offline cache (DESIGN §5): the last good `/derived` body plus one
/// capped rendered-log file per job, so the app repaints last-known state (with
/// the staleness pill) when the control plane is unreachable — and so a cold
/// start restores session lines together with their resume cursor (lines and
/// cursor must come from one artifact; a bare cursor would hide history).
///
/// Raw `/derived` bytes are cached, not decoded models — load goes back through
/// the same tolerant Codable path, so the cache can never drift from the
/// contract. Pure Foundation with an injectable directory (Linux-testable).
/// Cleared on unpair: fleet goals/logs shouldn't outlive the pairing.
final class OfflineCache {
    static let shared = OfflineCache()
    static let lineCap = 500
    private static let maxLogFiles = 30

    private let dir: URL
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    init(directory: URL? = nil) {
        let base = directory ?? FileManager.default
            .urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("roost-cache")
        dir = base
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    // MARK: Dashboard

    func saveDerivedRaw(_ data: Data) {
        try? data.write(to: dir.appendingPathComponent("derived.json"), options: .atomic)
    }

    func loadDerivedRaw() -> Data? {
        try? Data(contentsOf: dir.appendingPathComponent("derived.json"))
    }

    // MARK: Per-job rendered lines

    func saveLines(_ jobId: String, _ lines: [DisplayLine]) {
        let tail = Self.cap(lines)
        guard let data = try? encoder.encode(tail) else { return }
        try? data.write(to: logURL(jobId), options: .atomic)
        prune()
    }

    func loadLines(_ jobId: String) -> [DisplayLine] {
        guard let data = try? Data(contentsOf: logURL(jobId)),
              let lines = try? decoder.decode([DisplayLine].self, from: data)
        else { return [] }
        return lines
    }

    // MARK: Per-job raw rows (R108)
    //
    // The session caches the RAW wire `LogRow`s, not pre-rendered `DisplayLine`s,
    // so a cold start can re-render history in whichever mode the distilled/raw
    // toggle is in (distilling is a pure view concern; the rows are the truth).
    // Stored in a separate file so the older rendered-line cache (and its
    // contract test) is untouched.

    func saveRows(_ jobId: String, _ rows: [LogRow]) {
        let tail = rows.count > Self.lineCap ? Array(rows.suffix(Self.lineCap)) : rows
        guard let data = try? encoder.encode(tail) else { return }
        try? data.write(to: rowsURL(jobId), options: .atomic)
        prune()
    }

    func loadRows(_ jobId: String) -> [LogRow] {
        guard let data = try? Data(contentsOf: rowsURL(jobId)),
              let rows = try? decoder.decode([LogRow].self, from: data)
        else { return [] }
        return rows
    }

    /// Wipe everything (called on unpair).
    func clear() {
        guard let files = try? FileManager.default
            .contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return }
        files.forEach { try? FileManager.default.removeItem(at: $0) }
    }

    /// Pure cap rule (unit-tested): keep the newest `lineCap` by position.
    static func cap(_ lines: [DisplayLine]) -> [DisplayLine] {
        lines.count > lineCap ? Array(lines.suffix(lineCap)) : lines
    }

    // MARK: Internals

    // Job ids are server-issued hex, but sanitize anyway — never trust a path part.
    private func safe(_ jobId: String) -> String {
        jobId.filter { $0.isLetter || $0.isNumber }
    }
    private func logURL(_ jobId: String) -> URL {
        dir.appendingPathComponent("logs_\(safe(jobId)).json")
    }
    private func rowsURL(_ jobId: String) -> URL {
        dir.appendingPathComponent("rows_\(safe(jobId)).json")
    }

    /// Keep the newest `maxLogFiles` per-job cache files (rendered-line and raw-row
    /// files are pruned independently); old jobs age out naturally.
    private func prune() {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: [.contentModificationDateKey]
        ) else { return }
        for prefix in ["logs_", "rows_"] {
            let group = files.filter { $0.lastPathComponent.hasPrefix(prefix) }
            guard group.count > Self.maxLogFiles else { continue }
            let dated = group.map { url -> (URL, Date) in
                let d = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?
                    .contentModificationDate ?? .distantPast
                return (url, d)
            }
            dated.sorted { $0.1 < $1.1 }
                .prefix(group.count - Self.maxLogFiles)
                .forEach { try? fm.removeItem(at: $0.0) }
        }
    }
}
