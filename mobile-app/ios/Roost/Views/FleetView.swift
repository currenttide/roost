import SwiftUI

/// Fleet sheet (R121, API.md §2a): every worker — name, status, capability
/// summary, load, last-seen — so an operator can answer "is my fleet up"
/// from the couch. Reached from the dashboard overflow menu and presented as
/// a sheet, mirroring `PublishView`/`SchedulesView`.
///
/// All judgment (stale/offline pills, caps summary, sort) is in the pure
/// `Fleet` layer (Linux-tested); this view is the SwiftUI shell over
/// `FleetStore`. A 1 s ticker drives `now` so the pills and ages keep
/// advancing even when no new payload arrives (the R75 lesson).
struct FleetView: View {
    @EnvironmentObject var app: AppState
    @StateObject private var store = FleetStore()
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase

    /// Wall clock for pills/ages, advanced by the ticker below (R75).
    @State private var now = Date().timeIntervalSince1970
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    var body: some View {
        NavigationStack {
            List {
                if !store.workers.isEmpty {
                    Section {
                        headline
                    }
                    Section {
                        ForEach(store.sortedWorkers) { worker in
                            WorkerRow(worker: worker, now: now)
                                .accessibilityIdentifier("worker-row-\(worker.id)")
                        }
                    }
                } else if store.loaded {
                    Section {
                        Text("No workers enrolled. Add one with `roost worker` "
                             + "or the roost-onboard skill.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Section { ProgressView() }
                }

                if let error = store.lastError {
                    Text(error).font(.footnote).foregroundStyle(.orange)
                }
            }
            .accessibilityIdentifier("fleet-list")
            .navigationTitle("Fleet")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Done") { dismiss() }
                        .accessibilityIdentifier("fleet-done")
                }
            }
            .refreshable { await store.refresh() }
        }
        .onAppear {
            store.bind(app)
            store.start()
        }
        .onDisappear { store.stop() }
        .onChange(of: scenePhase) { _, phase in
            // No background networking (DESIGN §7).
            if phase == .active { store.start() } else { store.stop() }
        }
        .onReceive(ticker) { _ in now = Date().timeIntervalSince1970 }
    }

    /// "3 of 4 up" — up means server-live AND fresh by the client clock.
    private var headline: some View {
        let up = store.upCount(now: now)
        let total = store.workers.count
        return HStack(spacing: 10) {
            Circle()
                .fill(up == total ? Color.green : (up == 0 ? Color.red : Color.orange))
                .frame(width: 12, height: 12)
            Text(Fleet.headline(up: up, total: total))
                .font(.subheadline.weight(.semibold))
                .accessibilityIdentifier("fleet-headline")
            Spacer()
        }
    }
}

/// One worker row: status dot + name + stale/offline pill, then the
/// capability summary, then status · load · last-seen (ticking).
private struct WorkerRow: View {
    let worker: Worker
    let now: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                Circle()
                    .fill(dotColor)
                    .frame(width: 10, height: 10)
                Text(worker.displayName)
                    .font(.body.weight(.medium))
                    .lineLimit(1)
                Spacer()
                if let pill = Fleet.pill(status: worker.status,
                                         lastSeen: worker.lastSeen, now: now) {
                    Text(pill.rawValue)
                        .font(.caption2.weight(.semibold))
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(pillColor(pill).opacity(0.18), in: Capsule())
                        .foregroundStyle(pillColor(pill))
                }
            }
            if let caps = Fleet.capsSummary(worker.capabilities) {
                Text(caps)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .padding(.leading, 18)
            }
            Text(metaLine)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .padding(.leading, 18)
        }
        .padding(.vertical, 2)
    }

    private var metaLine: String {
        [worker.status,
         Fleet.loadText(running: worker.running, capacity: worker.capacity),
         Fleet.lastSeenText(worker.lastSeen, now: now)]
            .joined(separator: " · ")
    }

    /// Dot: green when up, orange when stale, secondary when offline/unknown.
    private var dotColor: Color {
        if Fleet.isUp(status: worker.status, lastSeen: worker.lastSeen, now: now) {
            return .green
        }
        switch Fleet.pill(status: worker.status, lastSeen: worker.lastSeen, now: now) {
        case .stale: return .orange
        case .offline: return .secondary
        case nil: return .secondary   // unknown status: render, don't alarm
        }
    }

    private func pillColor(_ pill: Fleet.Pill) -> Color {
        pill == .offline ? Color.red : Color.orange
    }
}
