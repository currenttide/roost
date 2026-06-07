#if os(macOS)
import RoostKit
import SwiftUI

/// The expandable window (DESIGN.md §2.4): same components as the popover
/// with more room. Sidebar: Runs · Workers · Console · Transfers.
struct MainWindowView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        @Bindable var transfers = model.transfers
        NavigationSplitView {
            List(MainSection.allCases, selection: $model.mainSection) { item in
                Label(item.rawValue, systemImage: item.icon)
                    .badge(item == .transfers ? model.transfers.activeCount : 0)
                    .tag(item)
            }
            .navigationSplitViewColumnWidth(min: 150, ideal: 170)
        } detail: {
            switch model.mainSection {
            case .runs: RunsPane()
            case .workers: WorkersPane()
            case .console: ConsolePane()
            case .transfers: TransfersPane()
            case .publish: PublishPane()
            case .schedules: SchedulesPane()
            }
        }
        .sheet(item: $transfers.pendingSend) { pending in
            SendFileSheet(pending: pending)
        }
        .onAppear { model.store.uiVisible = true }
    }
}

// MARK: - Runs

private struct RunsPane: View {
    @Environment(AppModel.self) private var model
    @State private var search = ""
    @State private var stateFilter = "all"

    private var filtered: [Run] {
        var runs = (model.store.snapshot?.runs ?? [])
            .sorted { ($0.createdAt ?? 0) > ($1.createdAt ?? 0) }
        if stateFilter != "all" {
            runs = runs.filter { $0.state == stateFilter }
        }
        if !search.isEmpty {
            runs = runs.filter {
                $0.goal.localizedCaseInsensitiveContains(search)
                    || $0.id.localizedCaseInsensitiveContains(search)
            }
        }
        return runs
    }

    var body: some View {
        @Bindable var model = model
        HSplitView {
            VStack(spacing: 0) {
                GoalBoxView()
                    .padding(10)
                Divider()
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(.secondary)
                    TextField("Filter runs", text: $search)
                        .textFieldStyle(.plain)
                    Picker("", selection: $stateFilter) {
                        Text("All").tag("all")
                        ForEach(["queued", "running", "succeeded", "failed", "cancelled"],
                                id: \.self) {
                            Text($0.capitalized).tag($0)
                        }
                    }
                    .pickerStyle(.menu)
                    .fixedSize()
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                Divider()
                List(filtered, selection: $model.selectedRunID) { run in
                    RunRowView(run: run,
                               workerName: model.store.worker(id: run.worker)?.name)
                        .tag(run.id)
                }
                .listStyle(.inset)
                loadMore
            }
            .frame(minWidth: 300, idealWidth: 360)

            detail
                .frame(minWidth: 380, maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder
    private var loadMore: some View {
        let shown = model.store.snapshot?.runs.count ?? 0
        if shown >= model.store.historyLimit {
            Button("Load more history") {
                model.store.historyLimit += 100
                model.store.poke()
            }
            .buttonStyle(.link)
            .font(.caption)
            .padding(6)
        }
    }

    @ViewBuilder
    private var detail: some View {
        if let runID = model.selectedRunID {
            NavigationStack {
                ScrollView {
                    RunDetailView(runID: runID, compact: false)
                }
                .navigationDestination(for: String.self) { childID in
                    ScrollView { RunDetailView(runID: childID, compact: false) }
                }
            }
            .id(runID)  // fresh stream per selection
        } else {
            ContentUnavailableView(
                "Select a run", systemImage: "play.circle",
                description: Text("Pick a run to see its phases, logs, and result."))
        }
    }
}

// MARK: - Workers

private struct WorkersPane: View {
    @Environment(AppModel.self) private var model
    @State private var selection: Worker.ID?
    @State private var confirmPrune = false
    @State private var pruneResult: String?
    @State private var pruneError: String?
    @State private var fetchFrom: Worker?

    var body: some View {
        HSplitView {
            VStack(spacing: 0) {
                HStack {
                    Text("\(model.store.workers.count) workers")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("Prune ghost workers…") { confirmPrune = true }
                        .controlSize(.small)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                Divider()
                workersTable
            }
            .frame(minWidth: 420)
            .confirmationDialog(
                "Delete worker records not seen in 7 days?",
                isPresented: $confirmPrune
            ) {
                Button("Prune", role: .destructive) { prune() }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Live nodes and workers with in-flight jobs are never touched.")
            }

            workerDetail
                .frame(minWidth: 280, maxWidth: .infinity, maxHeight: .infinity)
        }
        .overlay(alignment: .bottom) { pruneBanner }
        .sheet(item: $fetchFrom) { worker in
            FetchFileSheet(worker: worker)
        }
    }

    private var workersTable: some View {
        Table(model.store.workers, selection: $selection) {
                TableColumn("") { worker in
                    StatusDot(color: worker.statusColor,
                              label: worker.status.rawValue,
                              filled: worker.status != .offline)
                }
                .width(16)
                TableColumn("Name") { worker in
                    Text(worker.name)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .workerDropTarget(worker, model: model)
                }
                TableColumn("Status") { worker in
                    Text(worker.statusLine).foregroundStyle(.secondary)
                }
                TableColumn("Capabilities") { worker in
                    Text(worker.headline).foregroundStyle(.secondary)
                }
                TableColumn("Load") { worker in
                    Text("\(worker.running)/\(worker.capacity)")
                }
                .width(50)
                TableColumn("Last seen") { worker in
                    Text(Format.timeAgo(worker.lastSeen)).foregroundStyle(.secondary)
                }
                .width(80)
            }
    }

    @ViewBuilder
    private var pruneBanner: some View {
        if let text = pruneResult ?? pruneError {
            Text(text)
                .font(.caption)
                .padding(8)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 6))
                .padding(8)
                .task {
                    try? await Task.sleep(nanoseconds: 4_000_000_000)
                    pruneResult = nil
                    pruneError = nil
                }
        }
    }

    private func prune() {
        guard let client = model.store.client else { return }
        Task { @MainActor in
            do {
                let res = try await client.pruneWorkers()
                pruneResult = res.pruned == 0
                    ? "No ghost workers to prune"
                    : "Pruned \(res.pruned): \(res.names.joined(separator: ", "))"
                model.store.poke()
            } catch RoostClientError.unauthorized {
                pruneError = "Admin token required to prune workers"
            } catch {
                pruneError = error.localizedDescription
            }
        }
    }

    @ViewBuilder
    private var workerDetail: some View {
        if let worker = model.store.workers.first(where: { $0.id == selection }) {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) {
                        StatusDot(color: worker.statusColor,
                                  label: worker.status.rawValue,
                                  filled: worker.status != .offline)
                        Text(worker.name).font(.title3.weight(.semibold))
                        Spacer()
                        Text(worker.statusLine)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    HStack(spacing: 8) {
                        Button("Fetch file…") { fetchFrom = worker }
                            .controlSize(.small)
                        Text("or drop a file on the row to send one")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    if worker.revoked {
                        Text("revoked").font(.caption).foregroundStyle(.red)
                    }

                    labeled("Worker id", worker.id)
                    labeled("Registered", Format.timeAgo(worker.registeredAt))
                    labeled("Last seen", Format.timeAgo(worker.lastSeen))

                    recentRunsSection(worker)

                    Text("CAPABILITIES")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.tertiary)
                    Text(prettyJSON(worker.capabilities))
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .background(.quaternary.opacity(0.3),
                                    in: RoundedRectangle(cornerRadius: 6))

                    if let policy = worker.policy, policy != .object([:]) {
                        Text("POLICY")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.tertiary)
                        Text(prettyJSON(policy))
                            .font(.system(size: 11, design: .monospaced))
                            .textSelection(.enabled)
                    }
                }
                .padding(14)
            }
        } else {
            ContentUnavailableView(
                "Select a worker", systemImage: "server.rack",
                description: Text("Pick a worker to see its capabilities and recent runs."))
        }
    }

    @ViewBuilder
    private func recentRunsSection(_ worker: Worker) -> some View {
        let runs = (model.store.snapshot?.runs ?? [])
            .filter { $0.worker == worker.id }
            .prefix(5)
        if !runs.isEmpty {
            Text("RECENT RUNS HERE")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
            ForEach(Array(runs)) { run in
                RunRowView(run: run, workerName: nil)
            }
        }
    }

    private func labeled(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).font(.caption).foregroundStyle(.secondary)
                .frame(width: 80, alignment: .leading)
            Text(value).font(.caption).textSelection(.enabled)
        }
    }

    private func prettyJSON(_ value: JSONValue) -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(value),
              let text = String(data: data, encoding: .utf8) else { return "" }
        return text
    }
}
#endif
