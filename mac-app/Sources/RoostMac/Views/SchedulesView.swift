#if os(macOS)
import RoostKit
import SwiftUI

// Schedules verb: list the interval schedules and enable/disable each one. The
// clock (`every` / next-run / last-run) renders with the same grammar the CLI uses
// (RoostKit `ScheduleInterval`, Linux-tested). Creating a schedule is deferred to
// the CLI for now (`roost schedule --every …`) — it needs a full job-spec composer;
// this pane covers the menu-bar-natural read + toggle.

/// Loads the schedules list and applies enable/disable toggles. Admin/scheduler
/// scoped server-side; the pane surfaces a 403 honestly.
@MainActor
@Observable
final class SchedulesModel {
    private let store: FleetStore

    private(set) var schedules: [Schedule] = []
    private(set) var loading = false
    private(set) var hasLoaded = false
    /// Classified failure from the last load. A 404 (older CP without `/schedules`)
    /// classifies as `.endpointMissing`, NOT a generic error — see `displayState`.
    private(set) var loadError: SchedulesLoadError?
    /// Schedule ids with a toggle in flight, to disable the row's control.
    private(set) var pending: Set<String> = []

    init(store: FleetStore) { self.store = store }

    /// The single pane state. The decision (404 ⇒ unavailable, not an error
    /// stacked on the empty state) lives in RoostKit so it's Linux-tested.
    var displayState: SchedulesListState {
        SchedulesListState.decide(
            scheduleCount: schedules.count,
            loadError: loadError,
            loading: loading,
            hasLoaded: hasLoaded)
    }

    func refresh() async {
        guard let client = store.client else { return }
        loading = true
        defer { loading = false }
        do {
            schedules = try await client.schedules()
            loadError = nil
        } catch {
            loadError = SchedulesLoadError.from(error)
        }
        hasLoaded = true
    }

    /// A failed enable/disable toggle, shown inline. Kept separate from the load
    /// state so a transient toggle error doesn't masquerade as "list unavailable".
    var toggleError: String?

    /// Flip a schedule's enabled flag. The server returns the updated row (re-enabling
    /// restarts the clock), so we replace it in place rather than inventing fields.
    func setEnabled(_ schedule: Schedule, _ enabled: Bool) async {
        guard let client = store.client else { return }
        pending.insert(schedule.id)
        defer { pending.remove(schedule.id) }
        do {
            let updated = try await client.setScheduleEnabled(id: schedule.id, enabled: enabled)
            if let i = schedules.firstIndex(where: { $0.id == updated.id }) {
                schedules[i] = updated
            }
            toggleError = nil
        } catch RoostClientError.unauthorized {
            toggleError = "An admin (or scheduler) token is required to manage schedules."
        } catch {
            self.toggleError = error.localizedDescription
        }
    }
}

struct SchedulesPane: View {
    @Environment(AppModel.self) private var model
    @State private var sched: SchedulesModel?

    var body: some View {
        Group {
            if let sched {
                content(sched)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onAppear {
            if sched == nil {
                let s = SchedulesModel(store: model.store)
                sched = s
                Task { await s.refresh() }
            }
        }
    }

    @ViewBuilder
    private func content(_ sched: SchedulesModel) -> some View {
        VStack(spacing: 0) {
            HStack {
                Text("\(sched.schedules.count) schedule\(sched.schedules.count == 1 ? "" : "s")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if sched.loading { ProgressView().controlSize(.mini) }
                Spacer()
                Button("Refresh") { Task { await sched.refresh() } }
                    .controlSize(.small)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            Divider()

            // A toggle failure is an action error, distinct from the load state.
            if let toggleError = sched.toggleError {
                Label(toggleError, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(8)
            }

            // One state, one screen — the RoostKit decision guarantees the 404,
            // error, empty, and list states never stack on each other.
            switch sched.displayState {
            case .loading:
                Spacer()
                ProgressView().controlSize(.small)
                Spacer()
            case .unavailable:
                ContentUnavailableView {
                    Label("Schedules not available", systemImage: "clock.badge.xmark")
                } description: {
                    Text("This control plane doesn't support schedules (older server). Update the control plane to manage interval jobs from here.")
                }
            case .error(let message):
                ContentUnavailableView {
                    Label("Couldn't load schedules", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(message)
                } actions: {
                    Button("Retry") { Task { await sched.refresh() } }
                }
            case .empty:
                ContentUnavailableView {
                    Label("No schedules", systemImage: "clock.arrow.circlepath")
                } description: {
                    Text("Create one from the CLI: `roost schedule \"<goal>\" --every 6h`.")
                }
            case .list:
                List(sched.schedules) { schedule in
                    ScheduleRow(schedule: schedule,
                                busy: sched.pending.contains(schedule.id)) { enabled in
                        Task { await sched.setEnabled(schedule, enabled) }
                    }
                }
                .listStyle(.inset)
            }
        }
    }
}

private struct ScheduleRow: View {
    let schedule: Schedule
    let busy: Bool
    let onToggle: (Bool) -> Void

    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    if let name = schedule.name, !name.isEmpty {
                        Text(name).font(.callout.weight(.medium))
                    }
                    Text(schedule.taskSummary)
                        .font(.callout)
                        .foregroundStyle(schedule.name?.isEmpty == false ? .secondary : .primary)
                        .lineLimit(1)
                }
                Text(clockLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if busy { ProgressView().controlSize(.small) }
            Toggle("", isOn: Binding(
                get: { schedule.enabled },
                set: { onToggle($0) }))
                .toggleStyle(.switch)
                .labelsHidden()
                .disabled(busy)
                .help(schedule.enabled ? "Disable this schedule" : "Enable this schedule")
        }
        .padding(.vertical, 2)
    }

    /// every <interval> · next/last run, rendered with the CLI's grammar. A disabled
    /// schedule has no live next-run, so we say so rather than show a stale clock.
    private var clockLine: String {
        let now = Date().timeIntervalSince1970
        var parts = ["every \(ScheduleInterval.format(schedule.intervalSec))"]
        if schedule.enabled {
            if let next = ScheduleInterval.relative(to: schedule.nextRunAt, now: now) {
                parts.append("next \(next)")
            }
        } else {
            parts.append("disabled")
        }
        if let last = ScheduleInterval.relative(to: schedule.lastRunAt, now: now) {
            parts.append("last ran \(last)")
        }
        return parts.joined(separator: " · ")
    }
}
#endif
