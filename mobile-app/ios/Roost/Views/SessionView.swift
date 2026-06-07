import SwiftUI

/// Session view (DESIGN §3.2): header, live monospaced log via SSE, jump-to-
/// bottom auto-follow, result card on done, cancel + tree.
struct SessionView: View {
    let jobId: String
    @EnvironmentObject var app: AppState
    @StateObject private var store: SessionStore
    @Environment(\.scenePhase) private var scenePhase

    @State private var autoFollow = true
    @State private var showTree = false
    @State private var confirmCancel = false

    init(jobId: String) {
        self.jobId = jobId
        _store = StateObject(wrappedValue: SessionStore(jobId: jobId))
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            logScroll
            if let done = store.done {
                resultCard(done)
            }
            footer
            // Follow-up composer (DESIGN §3.2 / API.md §4, R38): react fast — type
            // a steering message and send it to the live job. Hidden once terminal
            // (the server 409s a terminal job), matching the Cancel button's gate.
            if !store.isTerminal {
                Divider()
                composer
            }
        }
        .navigationTitle(store.header?.goal ?? "Session")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            store.bind(app)
            Task { await store.loadHeader() }
            store.startStream()
        }
        .onDisappear { store.stopStream() }
        .onChange(of: scenePhase) { _, phase in
            // Foreground → re-page /logs + re-attach (the stream handles it).
            if phase == .active { store.resume() } else { store.stopStream() }
        }
        .sheet(isPresented: $showTree) {
            TreeView(jobId: jobId, store: store)
        }
        .confirmationDialog("Cancel this job?", isPresented: $confirmCancel) {
            Button("Cancel job", role: .destructive) { Task { await store.cancel() } }
            Button("Keep running", role: .cancel) {}
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 8) {
            Text(store.header?.healthStatus.glyph ?? "▶")
                .foregroundStyle(store.header?.healthStatus.color ?? .blue)
            VStack(alignment: .leading, spacing: 2) {
                Text(store.header?.displayGoal ?? jobId)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                Text(headerMeta)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
        }
        .padding(.horizontal).padding(.vertical, 8)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("session-header")
    }

    private var headerMeta: String {
        var parts: [String] = []
        if let w = store.header?.worker { parts.append(w) }
        // R85: the job's actual kind (was absent on iOS; Android wrongly hardcoded
        // "claude"). Omitted when the CP doesn't report it (older server).
        if let k = Subtitle.kindSegment(store.header?.kind) { parts.append(k) }
        parts.append(store.state.isEmpty ? (store.header?.state ?? "—") : store.state)
        if let e = UIFormat.elapsed(since: store.header?.createdAt),
           store.header?.healthStatus.isActive == true {
            parts.append(e)
        }
        return parts.joined(separator: " · ")
    }

    // MARK: - Log

    private var logScroll: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(store.lines) { line in
                        LogLineView(line: line).id(line.seq)
                    }
                    // Anchor for jump-to-bottom / auto-follow.
                    Color.clear.frame(height: 1).id(bottomAnchor)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .overlay(alignment: .bottomTrailing) {
                if !autoFollow {
                    Button {
                        autoFollow = true
                        withAnimation { proxy.scrollTo(bottomAnchor, anchor: .bottom) }
                    } label: {
                        Image(systemName: "arrow.down.circle.fill")
                            .font(.title)
                            .symbolRenderingMode(.hierarchical)
                    }
                    .padding(12)
                }
            }
            // Auto-follow the tail as lines arrive, unless the user scrolled up.
            .onChange(of: store.lines.count) { _, _ in
                if autoFollow {
                    withAnimation { proxy.scrollTo(bottomAnchor, anchor: .bottom) }
                }
            }
            // A drag up turns off auto-follow; the jump button turns it back on.
            .simultaneousGesture(
                DragGesture().onChanged { v in
                    if v.translation.height > 12 { autoFollow = false }
                }
            )
        }
    }

    private let bottomAnchor = "log-bottom"

    // MARK: - Result card (done)

    private func resultCard(_ d: SSEDonePayload) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(d.state ?? "done").font(.subheadline.bold())
                Spacer()
                if let code = d.exitCode { Text("exit \(code)").font(.caption.monospaced()) }
            }
            if let summary = d.result?.output ?? d.error, !summary.isEmpty {
                Text(summary).font(.caption).foregroundStyle(.secondary)
            }
            if let ev = d.result?.evidence, !ev.isEmpty {
                Text(ev).font(.caption2).foregroundStyle(.secondary)
            }
            if let tokens = d.tokensUsed {
                Text("\(tokens) tokens").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground))
    }

    // MARK: - Footer

    private var footer: some View {
        HStack {
            if !store.isTerminal {
                Button(role: .destructive) { confirmCancel = true } label: {
                    Label("Cancel", systemImage: "xmark.circle")
                }
                .accessibilityIdentifier("session-cancel")
            }
            Spacer()
            Button {
                Task { await store.loadTree() }
                showTree = true
            } label: { Label("Tree", systemImage: "list.bullet.indent") }
            .accessibilityIdentifier("session-tree")
        }
        .padding(.horizontal).padding(.vertical, 8)
        .background(.bar)
    }

    // MARK: - Composer (follow-up input, R38)

    private var composer: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let outcome = store.sendOutcome {
                Text(outcome).font(.caption2).foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                TextField("Follow up…", text: $store.draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .disabled(store.sending)
                    .onSubmit { Task { await store.sendFollowUp() } }
                    .accessibilityIdentifier("session-composer-field")
                Button {
                    Task { await store.sendFollowUp() }
                } label: {
                    Image(systemName: "arrow.up.circle.fill").font(.title2)
                }
                .accessibilityIdentifier("session-composer-send")
                .disabled(store.sending || !Composer.canSend(store.draft))
            }
        }
        .padding(.horizontal).padding(.vertical, 8)
        .background(.bar)
    }
}

/// One log line. stdout/stderr monospaced; event rows a subtle centered divider.
struct LogLineView: View {
    let line: DisplayLine

    var body: some View {
        switch line.kind {
        case .event:
            HStack {
                VStack { Divider() }
                Text(line.text).font(.caption2).foregroundStyle(.secondary)
                VStack { Divider() }
            }
            .padding(.vertical, 2)
        case .stderr:
            Text(line.text)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.red)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        case .stdout:
            Text(line.text)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// Child-job list for captain dispatches (API.md §4 tree). Rows push into their
/// own session view.
struct TreeView: View {
    let jobId: String
    @ObservedObject var store: SessionStore
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List(store.tree) { job in
                NavigationLink(value: job.id) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(job.intent ?? job.id).font(.body).lineLimit(1)
                        Text(job.state).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Job tree")
            .navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: String.self) { childId in
                SessionView(jobId: childId)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .overlay {
                if store.tree.isEmpty {
                    ContentUnavailableView("No child jobs", systemImage: "list.bullet.indent")
                }
            }
        }
    }
}
