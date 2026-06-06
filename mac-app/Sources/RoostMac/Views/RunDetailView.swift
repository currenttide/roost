#if os(macOS)
import Observation
import RoostKit
import SwiftUI

// MARK: - per-run stream model

/// Owns the SSE log stream for one open run detail (the "StreamHub" of
/// DESIGN.md §3 — one stream per open detail, torn down on dismiss).
@MainActor
@Observable
final class RunDetailModel {
    enum StreamState: Equatable {
        case loading       // initial page-in
        case live          // SSE attached
        case polling       // SSE failed twice → fallback to GET /logs (§12.2)
        case ended         // job terminal, stream done
        case failed(String)
    }

    let runID: String
    private let store: FleetStore

    private(set) var logs: [LogLine] = []
    private(set) var streamState: StreamState = .loading
    private(set) var done: JobDone?
    private(set) var tree: [Job] = []
    /// Filled when the run isn't in the store snapshot (deep history).
    private(set) var detachedRun: Run?

    var follow = true
    var showEvents = false

    private var task: Task<Void, Never>?
    private static let maxLines = 5000   // §8: log views virtualize

    init(runID: String, store: FleetStore) {
        self.runID = runID
        self.store = store
    }

    var run: Run? { store.run(id: runID) ?? detachedRun }

    var visibleLogs: [LogLine] {
        showEvents ? logs : logs.filter { $0.stream != "event" }
    }

    var eventCount: Int { logs.lazy.filter { $0.stream == "event" }.count }

    func start() {
        guard task == nil else { return }
        task = Task { [weak self] in await self?.runLoop() }
    }

    func stop() {
        task?.cancel()
        task = nil
    }

    private func runLoop() async {
        guard let client = store.client else {
            streamState = .failed("not connected")
            return
        }

        if store.run(id: runID) == nil {
            detachedRun = try? await client.derivedRun(id: runID)
        }
        await refreshTree(client)

        // 1. page in existing logs (chunks of 1000, capped)
        var since = 0
        do {
            while !Task.isCancelled {
                let page = try await client.logs(id: runID, since: since, limit: 1000)
                append(page.logs)
                if let last = page.logs.last { since = last.seq }
                if page.logs.count < 1000 { break }
                if logs.count >= Self.maxLines { break }
            }
        } catch {
            streamState = .failed(error.localizedDescription)
            return
        }

        // 2. SSE with resume; after 2 failed attempts fall back to polling
        var streamFailures = 0
        while !Task.isCancelled, streamFailures < 2 {
            streamState = .live
            do {
                for try await event in client.streamJob(id: runID, since: since) {
                    switch event {
                    case .log(let line):
                        append([line])
                        since = max(since, line.seq)
                    case .state:
                        await refreshRun(client)
                    case .done(let d):
                        done = d
                        await refreshRun(client)
                        await refreshTree(client)
                        streamState = .ended
                        return
                    }
                }
                // server closed without done (e.g. proxy timeout) — reconnect
                streamFailures += 1
            } catch {
                if Task.isCancelled { return }
                streamFailures += 1
            }
        }

        // 3. polling fallback
        while !Task.isCancelled {
            streamState = .polling
            if let page = try? await store.client?.logs(id: runID, since: since, limit: 1000) {
                append(page.logs)
                if let last = page.logs.last { since = last.seq }
                if ["succeeded", "failed", "cancelled"].contains(page.state) {
                    await refreshRun(client)
                    streamState = .ended
                    return
                }
            }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
    }

    private func append(_ lines: [LogLine]) {
        guard !lines.isEmpty else { return }
        let known = Set(logs.suffix(200).map(\.seq))  // reconnects may replay
        logs.append(contentsOf: lines.filter { !known.contains($0.seq) })
        if logs.count > Self.maxLines {
            logs.removeFirst(logs.count - Self.maxLines)
        }
    }

    private func refreshRun(_ client: RoostClient) async {
        store.poke()
        if store.run(id: runID) == nil {
            detachedRun = (try? await client.derivedRun(id: runID)) ?? detachedRun
        }
    }

    private func refreshTree(_ client: RoostClient) async {
        if let fetched = try? await client.tree(id: runID), fetched.count > 1 {
            tree = fetched
        }
    }
}

// MARK: - view

struct RunDetailView: View {
    @Environment(AppModel.self) private var model
    @State private var detail: RunDetailModel?
    @State private var confirmCancel = false
    @State private var actionError: String?

    let runID: String
    /// true in the popover (tighter spacing), false in the main window.
    var compact = false

    var body: some View {
        Group {
            if let detail {
                content(detail)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onAppear {
            if detail == nil {
                let d = RunDetailModel(runID: runID, store: model.store)
                d.start()
                detail = d
            }
        }
        .onDisappear {
            detail?.stop()
            detail = nil
        }
    }

    @ViewBuilder
    private func content(_ detail: RunDetailModel) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let run = detail.run {
                header(run)
                PhaseRailView(run: run)
                if run.isActive, let narration = run.narration, !narration.isEmpty {
                    Text("“\(narration)”")
                        .font(.callout.italic())
                        .foregroundStyle(.secondary)
                }
                if run.isTerminal {
                    outcome(run, detail: detail)
                }
            }
            logSection(detail)
            if !detail.tree.isEmpty {
                treeSection(detail)
            }
            if let run = detail.run {
                bottomBar(run)
            }
        }
        .padding(compact ? 12 : 16)
        .frame(minWidth: compact ? 336 : 480, alignment: .topLeading)
        .navigationTitle(detail.run?.goal ?? runID)
    }

    // MARK: header

    private func header(_ run: Run) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(run.goal.isEmpty ? run.id : run.goal)
                .font(compact ? .headline : .title3.weight(.semibold))
                .lineLimit(2)
            HStack(spacing: 4) {
                Text(headerMeta(run))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if run.isActive, let progress = run.progress {
                HStack(spacing: 8) {
                    ProgressView(value: Double(progress), total: 100)
                    Text("\(progress)%").font(.caption).foregroundStyle(.secondary)
                    if let eta = Format.eta(run.etaSec) {
                        Text(eta).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private func headerMeta(_ run: Run) -> String {
        var parts: [String] = []
        if let name = model.store.worker(id: run.worker)?.name {
            parts.append(run.isTerminal ? "ran on \(name)" : "running on \(name)")
        }
        parts.append(Format.elapsed(run))
        if run.health.status == "stuck?" {
            parts.append("⚠ may be stuck — \(run.health.reason)")
        }
        if let queued = run.queuedSec, run.state == "queued" {
            parts.append("queued \(Format.duration(queued))")
            if run.capableWorkers == 0 {
                parts.append("⚠ no capable worker online")
            }
        }
        return parts.joined(separator: " · ")
    }

    // MARK: outcome (§2.3 terminal states)

    @ViewBuilder
    private func outcome(_ run: Run, detail: RunDetailModel) -> some View {
        let (title, color): (String, Color) = switch run.health.status {
        case "verified": ("✓ Verified", .green)
        case "unverified": ("✓ Succeeded (not verified)", .orange)
        case "done": ("✓ Succeeded", .green)
        case "cancelled": ("⊘ Cancelled", .secondary)
        default: ("✗ Failed", .red)
        }

        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.headline).foregroundStyle(color)

            if run.state == "failed" {
                if let diagnosis = run.diagnosis, !diagnosis.isEmpty {
                    Text(diagnosis).font(.callout.weight(.medium))
                }
                if let error = detail.done?.error ?? run.result, !error.isEmpty {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            } else {
                if let evidence = run.evidence, !evidence.isEmpty {
                    Text(evidence)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                if let output = detail.done?.output ?? run.result, !output.isEmpty {
                    Text(output)
                        .font(.callout)
                        .textSelection(.enabled)
                        .lineLimit(compact ? 6 : nil)
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(color.opacity(0.07), in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: logs

    private func logSection(_ detail: RunDetailModel) -> some View {
        @Bindable var detail = detail
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("LOGS")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.tertiary)
                streamBadge(detail.streamState)
                Spacer()
                if detail.eventCount > 0 {
                    Toggle("events (\(detail.eventCount))", isOn: $detail.showEvents)
                        .toggleStyle(.checkbox)
                        .controlSize(.mini)
                }
                Toggle("follow", isOn: $detail.follow)
                    .toggleStyle(.checkbox)
                    .controlSize(.mini)
            }
            LogView(lines: detail.visibleLogs, follow: $detail.follow)
                .frame(minHeight: compact ? 140 : 220,
                       maxHeight: compact ? 200 : .infinity)
        }
    }

    @ViewBuilder
    private func streamBadge(_ state: RunDetailModel.StreamState) -> some View {
        switch state {
        case .loading:
            ProgressView().controlSize(.mini)
        case .live:
            Text("live").font(.caption2).foregroundStyle(.green)
        case .polling:
            Text("polling").font(.caption2).foregroundStyle(.orange)
                .help("Streaming unavailable — refreshing every 2 s")
        case .ended:
            EmptyView()
        case .failed(let why):
            Text(why).font(.caption2).foregroundStyle(.red).lineLimit(1)
        }
    }

    // MARK: tree (captain runs)

    private func treeSection(_ detail: RunDetailModel) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(detail.tree) { job in
                    NavigationLink(value: job.id) {
                        HStack(spacing: 6) {
                            Text(job.isTerminal
                                 ? (job.state == "succeeded" ? "✓" : job.state == "cancelled" ? "⊘" : "✗")
                                 : "◉")
                                .foregroundStyle(jobColor(job))
                            Text(job.goal.isEmpty ? job.id : job.goal)
                                .lineLimit(1)
                            Spacer()
                            Text(job.state)
                                .foregroundStyle(.secondary)
                        }
                        .font(.caption)
                        .padding(.leading, CGFloat(job.depth) * 14)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(job.id == runID)
                }
            }
            .padding(.top, 4)
        } label: {
            Text("PLAN — \(detail.tree.count) jobs")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
    }

    private func jobColor(_ job: Job) -> Color {
        switch job.state {
        case "succeeded": .green
        case "failed": .red
        case "cancelled": .secondary
        default: .blue
        }
    }

    // MARK: actions

    @ViewBuilder
    private func bottomBar(_ run: Run) -> some View {
        if let actionError {
            Text(actionError).font(.caption).foregroundStyle(.red)
        }
        HStack {
            if !Format.costLine(run.cost).isEmpty {
                Text(Format.costLine(run.cost))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Copy job id") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(run.id, forType: .string)
            }
            .controlSize(.small)

            if run.isActive {
                Button("Cancel Run…", role: .destructive) {
                    confirmCancel = true
                }
                .controlSize(.small)
                .confirmationDialog(
                    "Cancel this run?", isPresented: $confirmCancel
                ) {
                    Button("Cancel run + sub-jobs", role: .destructive) {
                        perform { try await model.store.cancelRun(id: run.id, tree: true) }
                    }
                    if (detail?.tree.count ?? 0) > 1 {
                        Button("This run only", role: .destructive) {
                            perform { try await model.store.cancelRun(id: run.id, tree: false) }
                        }
                    }
                    Button("Keep running", role: .cancel) {}
                }
            } else if run.state == "failed" {
                Button("Investigate in Console") {
                    let why = run.diagnosis ?? run.result ?? "unknown"
                    model.openConsole(prompt: """
                    Investigate roost run \(run.id) (goal: "\(run.goal)") — it \
                    failed with: \(why). Diagnose the root cause and suggest a fix.
                    """)
                }
                .controlSize(.small)
                Button("Retry") {
                    perform { try await model.store.retry(run: run) }
                }
                .controlSize(.small)
            }
        }
    }

    private func perform(_ action: @escaping () async throws -> Void) {
        actionError = nil
        Task { @MainActor in
            do { try await action() } catch { actionError = error.localizedDescription }
        }
    }
}

// MARK: - phase rail (§2.3 — the trust loop made visible)

struct PhaseRailView: View {
    let run: Run

    private static let steps = ["queued", "running", "verifying", "done"]

    private var currentIndex: Int {
        switch run.phase {
        case "queued", "assigned": 0
        case "running": 1
        case "verifying", "self-healing": 2
        default: 3
        }
    }

    private var terminalLabel: String {
        switch run.phase {
        case "succeeded": "succeeded"
        case "failed": "failed"
        case "cancelled": "cancelled"
        default: "done"
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(Self.steps.enumerated()), id: \.offset) { index, step in
                HStack(spacing: 0) {
                    if index > 0 {
                        Rectangle()
                            .fill(index <= currentIndex ? railColor : Color.secondary.opacity(0.3))
                            .frame(height: 2)
                            .frame(maxWidth: .infinity)
                    }
                    VStack(spacing: 2) {
                        Circle()
                            .fill(index <= currentIndex ? railColor : Color.secondary.opacity(0.3))
                            .frame(width: index == currentIndex ? 9 : 6,
                                   height: index == currentIndex ? 9 : 6)
                        Text(index == 3 ? terminalLabel
                             : (index == 2 && run.phase == "self-healing" ? "self-healing" : step))
                            .font(.caption2)
                            .foregroundStyle(index == currentIndex ? .primary : .tertiary)
                    }
                    .fixedSize()
                }
            }
        }
        .animation(.easeInOut(duration: 0.3), value: currentIndex)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Phase: \(run.phase)")
    }

    private var railColor: Color {
        if run.isTerminal { return run.statusColor }
        return run.phase == "verifying" || run.phase == "self-healing" ? .purple : .blue
    }
}

// MARK: - log view

struct LogView: View {
    let lines: [LogLine]
    @Binding var follow: Bool

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(lines) { line in
                        Text(line.text)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(color(for: line))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(line.seq)
                    }
                    if lines.isEmpty {
                        Text("no output yet")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                            .padding(4)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(6)
            }
            .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 6))
            // any manual scroll pauses follow-mode (§2.3)
            .simultaneousGesture(DragGesture().onChanged { _ in follow = false })
            .onChange(of: lines.count) {
                if follow { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            .onChange(of: follow) {
                if follow { proxy.scrollTo("bottom", anchor: .bottom) }
            }
        }
    }

    private func color(for line: LogLine) -> Color {
        switch line.stream {
        case "stderr": .orange
        case "event": .secondary
        default: .primary
        }
    }
}
#endif
