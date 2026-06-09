#if os(macOS)
import RoostKit
import SwiftUI

/// Ambient menu-bar popover (redesign §Ambient popover): verdict · goal box ·
/// a few active runs · footer. Glance, type a goal, go — workers, recent runs,
/// and run detail live in the windows now.
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
            Button("Connect…") { model.openOnboarding() }
                .keyboardShortcut(.defaultAction)
        }
        .padding(24)
    }
}

struct FleetPopoverView: View {
    @Environment(AppModel.self) private var model

    private static let maxActive = 4

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    GoalBoxView()
                    staleBanner
                    activeSection
                }
                .padding(12)
            }
            .frame(maxHeight: 420)
            Divider()
            footer
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
                Button("Open Roost") { model.openWorkspace() }
                    .keyboardShortcut("o")
                Button("Fleet") { model.openFleet() }
                Button("Settings…") { model.openSettings() }
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
                action: ("Reconnect…", { model.openOnboarding() }))
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

    // MARK: active runs

    @ViewBuilder
    private var activeSection: some View {
        let active = model.store.activeRuns
        let pending = model.store.optimisticRuns
            .filter { opt in !active.contains { $0.id == opt.id } }

        if active.isEmpty && pending.isEmpty {
            Text("No active runs. Type a goal above to put your fleet to work.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.vertical, 4)
        } else {
            VStack(alignment: .leading, spacing: 6) {
                SectionLabel("Active")
                ForEach(pending, id: \.id) { opt in
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.mini)
                        Text(opt.goal).font(.callout).lineLimit(1)
                        Spacer()
                        Text("submitting…").font(.caption2).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 2)
                }
                ForEach(active.prefix(Self.maxActive)) { run in
                    Button { model.openRun(run.id) } label: {
                        Card {
                            RunRowView(run: run,
                                       workerName: model.store.worker(id: run.worker)?.name)
                        }
                    }
                    .buttonStyle(.plain)
                }
                if active.count > Self.maxActive {
                    Button("show all \(active.count) in Roost →") { model.openWorkspace() }
                        .buttonStyle(.link)
                        .font(.caption)
                }
            }
        }
    }

    // MARK: footer

    private var footer: some View {
        HStack(spacing: 12) {
            Button("Open Roost ⌘O") { model.openWorkspace() }
                .buttonStyle(.link)
                .keyboardShortcut("o")
            Button("Console ⌘T") { model.openConsole() }
                .buttonStyle(.link)
                .keyboardShortcut("t")
            Button("Fleet") { model.openFleet() }
                .buttonStyle(.link)
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
#endif
