import SwiftUI

/// Small presentation helpers shared across views. Kept here so the views stay
/// declarative and the mappings (status→color, epoch→"4m 12s") live in one place.
enum UIFormat {
    /// Compact elapsed/relative duration like "4m 12s", "1h 03m", "12s".
    static func duration(_ seconds: Double) -> String {
        let s = max(0, Int(seconds))
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m \(String(format: "%02d", s % 60))s" }
        return "\(s / 3600)h \(String(format: "%02d", (s % 3600) / 60))m"
    }

    /// Elapsed since an epoch timestamp, against now.
    static func elapsed(since epoch: Double?) -> String? {
        guard let epoch else { return nil }
        return duration(Date().timeIntervalSince1970 - epoch)
    }

    /// Human byte size like "32 B", "1.2 KB", "3.4 MB" (publish sheet, §6).
    /// Decimal units to match how sizes read on the web.
    static func bytes(_ count: Int) -> String {
        let n = Double(max(0, count))
        if n < 1000 { return "\(Int(n)) B" }
        let units = ["KB", "MB", "GB", "TB"]
        var value = n / 1000
        var unit = 0
        while value >= 1000 && unit < units.count - 1 {
            value /= 1000
            unit += 1
        }
        return String(format: "%.1f %@", value, units[unit])
    }
}

extension HealthStatus {
    /// Tint for the glyph/row. Conservative: green / red / orange / secondary.
    var color: Color {
        switch self {
        case .verified, .done: return .green
        case .failed: return .red
        case .unverified, .unplaceable, .stuckQuestion: return .orange
        case .running, .verifying, .selfHealing: return .blue
        case .cancelled: return .secondary
        case .queued, .waiting: return .secondary
        case .unknown: return .secondary
        }
    }
}
