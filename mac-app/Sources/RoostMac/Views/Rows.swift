#if os(macOS)
import RoostKit
import SwiftUI

/// A calm run row — two lines, max (redesign §Decluttered job display). The dot
/// carries status; the status line is goal-agnostic metadata; a single pill or
/// inline progress sits at the trailing edge. Narration moves to hover, not a
/// third line.
struct RunRowView: View {
    let run: Run
    let workerName: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                StatusDot(color: run.statusColor,
                          label: run.health.status,
                          filled: run.health.status != "cancelled")
                Text(run.displayGoal.isEmpty ? run.id : run.displayGoal)
                    .font(.body)
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
            HStack(spacing: 6) {
                Text(statusLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 4)
                trailing
            }
            .padding(.leading, 16)
        }
        .padding(.vertical, 3)
        .contentShape(Rectangle())
        .help(run.narration ?? "")
    }

    private var statusLine: String {
        var parts = [run.statusWord]
        if let workerName { parts.append(workerName) }
        parts.append(run.isTerminal
            ? Format.timeAgo(run.finishedAt ?? run.createdAt)
            : Format.elapsed(run))
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var trailing: some View {
        if let pill = run.attention {
            StatusPill(text: pill)
        } else if run.isActive, let progress = run.progress {
            HStack(spacing: 5) {
                ProgressView(value: Double(progress), total: 100)
                    .controlSize(.small)
                    .frame(width: 56)
                Text("\(progress)%")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
        }
    }
}

/// A calm worker row: dot · name · status · headline capabilities.
struct WorkerRowView: View {
    let worker: Worker

    var body: some View {
        HStack(spacing: 8) {
            StatusDot(color: worker.statusColor,
                      label: "\(worker.name) \(worker.status.rawValue)",
                      filled: worker.status != .offline)
            Text(worker.name).font(.body).lineLimit(1)
            Text(worker.statusLine).font(.caption).foregroundStyle(.secondary)
            Spacer()
            Text(worker.headline)
                .font(.caption)
                .foregroundStyle(.tertiary)
                .lineLimit(1)
        }
        .padding(.vertical, 2)
    }
}
#endif
