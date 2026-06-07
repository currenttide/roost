import SwiftUI

/// Dashboard / home (DESIGN §3.1). Verdict bar, run list, big new-session
/// button. Polls /derived every 2 s while foregrounded; pauses in background.
struct DashboardView: View {
    @EnvironmentObject var app: AppState
    @StateObject private var store = DashboardStore()
    @Environment(\.scenePhase) private var scenePhase

    @State private var showNew = false
    @State private var showPublish = false
    @State private var showNotifySettings = false
    @State private var showSchedules = false
    @State private var confirmCancel: Run?
    @State private var path: [String] = []      // navigation stack of job ids

    var body: some View {
        NavigationStack(path: $path) {
            ZStack(alignment: .bottom) {
                List {
                    if let verdict = store.derived?.fleetVerdict {
                        verdictBar(verdict)
                            .listRowInsets(EdgeInsets())
                            .listRowSeparator(.hidden)
                    }
                    ForEach(store.sortedRuns) { run in
                        RunRow(run: run)
                            .contentShape(Rectangle())
                            .onTapGesture { path.append(run.runId) }
                            .accessibilityIdentifier("run-row-\(run.runId)")
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                swipeActions(for: run)
                            }
                    }
                }
                .accessibilityIdentifier("dashboard-list")
                .listStyle(.plain)
                .safeAreaPadding(.bottom, 72)   // keep last row above the button

                newSessionButton
            }
            .navigationTitle("Roost")
            .navigationDestination(for: String.self) { jobId in
                SessionView(jobId: jobId)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button {
                            showPublish = true
                        } label: { Label("Publish a site", systemImage: "globe") }
                        .accessibilityIdentifier("overflow-publish")
                        Button {
                            showNotifySettings = true
                        } label: { Label("Notifications", systemImage: "bell") }
                        .accessibilityIdentifier("overflow-notifications")
                        Button {
                            showSchedules = true
                        } label: { Label("Schedules", systemImage: "clock.arrow.circlepath") }
                        .accessibilityIdentifier("overflow-schedules")
                        Divider()
                        Button("Unpair", role: .destructive) { app.unpair() }
                            .accessibilityIdentifier("overflow-unpair")
                        if let name = app.credential?.name { Text(name) }
                    } label: { Image(systemName: "ellipsis.circle") }
                    .accessibilityIdentifier("overflow-menu")
                }
            }
            .overlay(alignment: .top) { stalePill }
            .refreshable { await store.refresh() }
            .sheet(isPresented: $showNew) {
                NewSessionView { newId in
                    showNew = false
                    path.append(newId)
                }
            }
            .sheet(isPresented: $showPublish) {
                PublishView()
            }
            .sheet(isPresented: $showNotifySettings) {
                NotificationSettingsView()
            }
            .sheet(isPresented: $showSchedules) {
                SchedulesView()
            }
            .confirmationDialog("Cancel this job?",
                                isPresented: Binding(
                                    get: { confirmCancel != nil },
                                    set: { if !$0 { confirmCancel = nil } }),
                                presenting: confirmCancel) { run in
                Button("Cancel job", role: .destructive) {
                    Task { await store.cancel(run.runId) }
                    confirmCancel = nil
                }
                Button("Keep running", role: .cancel) { confirmCancel = nil }
            }
        }
        .onAppear {
            store.bind(app)
            store.start()
            // Screenshot/demo hook: jump straight to the publish sheet.
            if app.autoOpenPublish { showPublish = true }
            // Screenshot/demo hook: push straight into a job's Session screen so
            // the composer is reachable without tapping a run row (R84/XCUITest).
            if let jobId = app.autoOpenSession, path.isEmpty { path.append(jobId) }
        }
        .onChange(of: scenePhase) { _, phase in
            // Pause polling in background (DESIGN §7: no background networking).
            if phase == .active { store.start() } else { store.stop() }
        }
    }

    // MARK: - Pieces

    private func verdictBar(_ v: FleetVerdict) -> some View {
        HStack(spacing: 10) {
            Circle()
                .fill(v.isAlert ? Color.red : Color.green)
                .frame(width: 12, height: 12)
            Text(v.summary)
                .font(.subheadline.weight(.semibold))
                .lineLimit(2)
            Spacer()
            Text("\(store.liveWorkerCount) nodes")
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("node-count")
        }
        .padding(.horizontal)
        .padding(.vertical, 12)
        .background((v.isAlert ? Color.red : Color.green).opacity(0.12))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("verdict-bar")
    }

    @ViewBuilder
    private func swipeActions(for run: Run) -> some View {
        if run.state == "running" || run.state == "assigned" {
            Button(role: .destructive) { confirmCancel = run } label: {
                Label("Cancel", systemImage: "xmark.circle")
            }
        }
        if run.state == "failed" {
            Button {
                Task {
                    if let newId = await store.retry(run.runId) {
                        path.append(newId)
                    }
                }
            } label: { Label("Retry", systemImage: "arrow.clockwise") }
            .tint(.blue)
        }
    }

    private var newSessionButton: some View {
        Button { showNew = true } label: {
            Label("New session", systemImage: "mic.fill")
                .font(.headline)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 6)
        }
        .buttonStyle(.borderedProminent)
        .accessibilityIdentifier("new-session-button")
        .padding(.horizontal)
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private var stalePill: some View {
        if store.stale, let gen = store.derived?.generatedAt {
            let age = Int(Date().timeIntervalSince1970 - gen)
            Text("data \(max(age, 0))s old")
                .font(.caption2.weight(.semibold))
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(.thinMaterial, in: Capsule())
                .padding(.top, 4)
        } else if store.lastError != nil {
            Text(store.lastError ?? "")
                .font(.caption2)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(.thinMaterial, in: Capsule())
                .padding(.top, 4)
        }
    }
}

/// A single run row (DESIGN §3.1 mock): glyph + title, then worker · kind ·
/// elapsed/result. Unknown health renders the raw status as text (never crash).
struct RunRow: View {
    let run: Run

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            glyph
            VStack(alignment: .leading, spacing: 2) {
                Text(run.displayGoal ?? run.runId)
                    .font(.body.weight(.medium))
                    .lineLimit(1)
                Text(meta)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
        }
        .padding(.vertical, 4)
    }

    private var glyph: some View {
        Text(run.healthStatus.glyph)
            .font(.headline)
            .foregroundStyle(run.healthStatus.color)
            .frame(width: 22, alignment: .center)
    }

    private var meta: String {
        var parts: [String] = []
        if let w = run.worker { parts.append(w) }
        // R85: the job's actual kind (truthful "command"/"claude"/…); omitted on an
        // older CP that doesn't report it.
        if let k = Subtitle.kindSegment(run.kind) { parts.append(k) }
        // Active → elapsed since created; terminal → state (+ result snippet).
        if run.healthStatus.isActive, let e = UIFormat.elapsed(since: run.createdAt) {
            parts.append(e)
        } else {
            parts.append(run.state)
        }
        if let sub = run.subtitle, !sub.isEmpty { parts.append(sub) }
        return parts.joined(separator: " · ")
    }
}
