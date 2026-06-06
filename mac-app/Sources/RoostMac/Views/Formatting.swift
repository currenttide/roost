#if os(macOS)
import RoostKit
import SwiftUI

// Shared presentation helpers. Status colors are used only on the small
// dot/badge — never to fill the UI (DESIGN.md §7).

enum Format {
    static func timeAgo(_ epoch: Double?) -> String {
        guard let epoch, epoch > 0 else { return "—" }
        let seconds = max(0, Date().timeIntervalSince1970 - epoch)
        switch seconds {
        case ..<60: return "\(Int(seconds))s ago"
        case ..<3600: return "\(Int(seconds / 60))m ago"
        case ..<86_400: return "\(Int(seconds / 3600))h ago"
        default: return "\(Int(seconds / 86_400))d ago"
        }
    }

    static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds >= 0 else { return "—" }
        switch seconds {
        case ..<60: return "\(Int(seconds))s"
        case ..<3600: return "\(Int(seconds / 60))m"
        default: return String(format: "%.1fh", seconds / 3600)
        }
    }

    /// Elapsed wall-clock of a run: started (or created) → finished (or now).
    static func elapsed(_ run: Run) -> String {
        guard let start = run.createdAt else { return "—" }
        let end = run.finishedAt ?? Date().timeIntervalSince1970
        return duration(end - start)
    }

    static func tokens(_ count: Int) -> String {
        switch count {
        case ..<1000: return "\(count) tok"
        case ..<1_000_000: return String(format: "%.1fk tok", Double(count) / 1000)
        default: return String(format: "%.1fM tok", Double(count) / 1_000_000)
        }
    }

    static func cost(_ usd: Double) -> String {
        usd < 0.005 ? "<$0.01" : String(format: "$%.2f", usd)
    }

    static func costLine(_ cost: Run.Cost) -> String {
        guard cost.tokensUsed > 0 else { return "" }
        var line = "\(tokens(cost.tokensUsed)) · ~\(self.cost(cost.costEstUSD))"
        if let pct = cost.budgetPct {
            line += String(format: " · budget %.0f%% used", pct)
        }
        return line
    }

    static func eta(_ seconds: Int?) -> String? {
        guard let seconds, seconds > 0 else { return nil }
        return "eta ~\(duration(Double(seconds)))"
    }
}

extension Run {
    /// Glyph for terminal rows (✓ verified / ✓ done / ✗ failed / ⊘ cancelled).
    var verdictGlyph: String {
        switch health.status {
        case "verified", "done", "unverified": return "✓"
        case "cancelled": return "⊘"
        case "failed": return "✗"
        default: return "◉"
        }
    }

    var statusColor: Color {
        switch health.status {
        case "verified", "done": return .green
        case "running", "queued": return .blue
        case "verifying", "self-healing": return .purple
        case "waiting", "stuck?", "unplaceable", "unverified": return .orange
        case "failed": return .red
        case "cancelled": return .secondary
        default: return state == "failed" ? .red : .blue
        }
    }

    /// One compact metadata line for list rows.
    func metaLine(workerName: String?) -> String {
        var parts: [String] = [phase]
        if let workerName { parts.append(workerName) }
        parts.append(isTerminal
            ? Format.timeAgo(finishedAt ?? createdAt)
            : Format.elapsed(self))
        return parts.joined(separator: " · ")
    }
}

extension Worker {
    var statusColor: Color {
        switch status {
        case .idle: return .green
        case .busy: return .blue
        case .stale: return .orange
        case .offline, .unknown: return .secondary
        }
    }

    var statusLine: String {
        switch status {
        case .busy: return "busy \(running)/\(capacity)"
        case .idle: return running > 0 ? "idle \(running)/\(capacity)" : "idle"
        case .stale: return "stale"
        case .offline: return "offline · last seen \(Format.timeAgo(lastSeen))"
        case .unknown: return statusRaw
        }
    }
}

/// A small filled status dot with a VoiceOver label (DESIGN.md §7).
struct StatusDot: View {
    let color: Color
    let label: String
    var filled = true

    var body: some View {
        Circle()
            .strokeBorder(color, lineWidth: filled ? 0 : 1.5)
            .background(Circle().fill(filled ? color : .clear))
            .frame(width: 8, height: 8)
            .accessibilityLabel(label)
    }
}
#endif
