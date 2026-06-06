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
