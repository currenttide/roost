#if os(macOS)
import RoostKit
import SwiftUI

/// Popover content (DESIGN.md §2.2): header · goal box · active · recent ·
/// workers · footer, with run detail pushed on a NavigationStack.
struct PopoverRootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Group {
            if model.store.isConfigured {
                FleetPopoverView()
            } else {
                ConnectPromptView()
            }
        }
        .frame(width: 360)
    }
}

/// Shown in the popover until a control plane is configured.
private struct ConnectPromptView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "bird.fill")
                .font(.system(size: 28))
                .foregroundStyle(.secondary)
            Text("Roost").font(.headline)
            Text("Connect to a control plane to see your fleet.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("Connect…") {
                model.openOnboardingWindow?()
            }
            .keyboardShortcut(.defaultAction)
        }
        .padding(24)
    }
}

struct FleetPopoverView: View {
    @Environment(AppModel.self) private var model
    @State private var path: [String] = []  // run ids

    private static let maxActive = 3
    private static let maxRecent = 4
    private static let maxWorkers = 6

    var body: some View {
        @Bindable var transfers = model.transfers
        NavigationStack(path: $path) {
            VStack(alignment: .leading, spacing: 0) {
                header
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        GoalBoxView()
                        staleBanner
                        runSections
                        workerSection
                    }
                    .padding(12)
                }
                .frame(maxHeight: 480)
                Divider()
                footer
            }
            .navigationDestination(for: String.self) { runID in
                RunDetailView(runID: runID, compact: true)
            }
        }
        .sheet(item: $transfers.pendingSend) { pending in
            SendFileSheet(pending: pending)
        }
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "bird.fill")
            Text("Roost").font(.headline)
            Spacer()
            Group {
                if let host = model.store.client?.connection.baseURL.host {
                    let n = model.store.workers
                        .filter { $0.status == .idle || $0.status == .busy }.count
                    Text("\(host) · \(n) worker\(n == 1 ? "" : "s")")
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            Menu {
                Button("Open Roost") { model.openMainWindow?(nil) }
                    .keyboardShortcut("o")
                Button("Settings…") { model.openSettingsWindow?() }
                Divider()
                Button("Quit Roost") { NSApp.terminate(nil) }
                    .keyboardShortcut("q")
            } label: {
                Image(systemName: "gearshape")
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .overlay(alignment: .bottom) { verdictBar }
    }

    @ViewBuilder
    private var verdictBar: some View {
        if let verdict = model.store.verdict {
            HStack(spacing: 4) {
                StatusDot(
                    color: verdict.level == .alert ? .orange : .green,
                    label: "fleet \(verdict.level.rawValue)")
                Text(verdict.summary)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer()
            }
            .padding(.horizontal, 12)
            .offset(y: 14)
        }
    }

    // MARK: degraded state

    @ViewBuilder
    private var staleBanner: some View {
        switch model.store.reachability {
        case .unreachable:
            banner(
                icon: "wifi.slash",
                text: "Control plane unreachable — showing data from \(Format.timeAgo(model.store.lastUpdated.map(\.timeIntervalSince1970)))",
                action: ("Retry", { model.store.poke() }))
        case .unauthorized:
            banner(
                icon: "key.slash",
                text: "Unauthorized — the token may have been rotated.",
                action: ("Reconnect…", { model.openOnboardingWindow?() }))
        case .ok, .never:
            EmptyView()
        }
    }

    private func banner(
        icon: String, text: String, action: (String, () -> Void)
    ) -> some View {
        HStack(spacing: 6) {
            Image(systemName: icon).foregroundStyle(.orange)
            Text(text).font(.caption).foregroundStyle(.secondary)
            Spacer()
            Button(action.0, action: action.1)
                .controlSize(.small)
        }
        .padding(8)
        .background(.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
    }

    // MARK: run sections

    @ViewBuilder
    private var runSections: some View {
        let store = model.store
        let active = store.activeRuns
        let pendingOptimistic = store.optimisticRuns
            .filter { opt in !active.contains { $0.id == opt.id } }

        if !active.isEmpty || !pendingOptimistic.isEmpty {
            section("ACTIVE") {
                ForEach(pendingOptimistic, id: \.id) { opt in
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.mini)
                        Text(opt.goal).font(.callout).lineLimit(1)
                        Spacer()
                        Text("submitting…").font(.caption2).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 2)
                }
                ForEach(active.prefix(Self.maxActive)) { run in
                    runRow(run)
                }
                showAll(count: active.count, over: Self.maxActive)
            }
        }

        let recent = store.recentRuns
        if !recent.isEmpty {
            section("RECENT") {
                ForEach(recent.prefix(Self.maxRecent)) { run in
                    runRow(run)
                }
                showAll(count: recent.count, over: Self.maxRecent)
            }
        }
    }

    private func runRow(_ run: Run) -> some View {
        Button {
            path.append(run.id)
        } label: {
            RunRowView(run: run, workerName: model.store.worker(id: run.worker)?.name)
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func showAll(count: Int, over limit: Int) -> some View {
        if count > limit {
            Button("show all \(count) →") { model.openMainWindow?(nil) }
                .buttonStyle(.link)
                .font(.caption)
        }
    }

    // MARK: workers

    @ViewBuilder
    private var workerSection: some View {
        let workers = model.store.workers
        if !workers.isEmpty {
            section("WORKERS") {
                ForEach(workers.prefix(Self.maxWorkers)) { worker in
                    WorkerRowView(worker: worker)
                        .workerDropTarget(worker, model: model)  // drag a file on = send it
                }
                showAll(count: workers.count, over: Self.maxWorkers)
            }
        }
    }

    private func section(
        _ title: String, @ViewBuilder content: () -> some View
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
            content()
        }
    }

    // MARK: footer

    private var footer: some View {
        HStack {
            Button("Open Roost ⌘O") { model.openMainWindow?(nil) }
                .buttonStyle(.link)
                .keyboardShortcut("o")
            Button("Console ⌘T") { model.openConsole() }
                .buttonStyle(.link)
                .keyboardShortcut("t")
            Spacer()
            if let updated = model.store.lastUpdated {
                Text("updated \(Format.timeAgo(updated.timeIntervalSince1970))")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .font(.caption)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }
}

// MARK: - rows

struct RunRowView: View {
    let run: Run
    let workerName: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(run.isTerminal ? run.verdictGlyph : "◉")
                    .foregroundStyle(run.statusColor)
                    .accessibilityLabel(run.health.status)
                Text(run.goal.isEmpty ? run.id : run.goal)
                    .font(.callout)
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
            HStack(spacing: 6) {
                Text(run.metaLine(workerName: workerName))
                if run.isActive, let progress = run.progress {
                    ProgressView(value: Double(progress), total: 100)
                        .controlSize(.small)
                        .frame(width: 70)
                    Text("\(progress)%")
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.leading, 18)

            subtitle
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .padding(.leading, 18)
        }
        .contentShape(Rectangle())
        .padding(.vertical, 2)
    }

    // narration while active; cost / verdict / diagnosis when terminal —
    // render only what the backend provided, never invent (§2.2).
    @ViewBuilder
    private var subtitle: some View {
        if run.isActive {
            if let narration = run.narration, !narration.isEmpty {
                Text("“\(narration)”").italic()
            }
        } else if run.state == "failed" {
            if let why = run.diagnosis ?? run.result, !why.isEmpty {
                Text("“\(why)”").italic()
            }
        } else {
            let pieces = [
                run.verified == true ? "verified ✓" : nil,
                Format.costLine(run.cost).isEmpty ? nil : Format.costLine(run.cost),
            ].compactMap { $0 }
            if !pieces.isEmpty {
                Text(pieces.joined(separator: " · "))
            }
        }
    }
}

struct WorkerRowView: View {
    let worker: Worker

    var body: some View {
        HStack(spacing: 6) {
            StatusDot(
                color: worker.statusColor,
                label: "\(worker.name) \(worker.status.rawValue)",
                filled: worker.status != .offline)
            Text(worker.name)
                .font(.callout)
                .lineLimit(1)
            Text(worker.statusLine)
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            Text(worker.headline)
                .font(.caption)
                .foregroundStyle(.tertiary)
                .lineLimit(1)
        }
    }
}
#endif
