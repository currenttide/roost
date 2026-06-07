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
    var error: String?
    /// Schedule ids with a toggle in flight, to disable the row's control.
    private(set) var pending: Set<String> = []

    init(store: FleetStore) { self.store = store }

    func refresh() async {
        guard let client = store.client else { return }
        loading = true
        defer { loading = false }
        do {
            schedules = try await client.schedules()
            error = nil
        } catch RoostClientError.unauthorized {
            error = "An admin (or scheduler) token is required to manage schedules."
        } catch {
            self.error = error.localizedDescription
        }
    }

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
            error = nil
        } catch RoostClientError.unauthorized {
            error = "An admin (or scheduler) token is required to manage schedules."
        } catch {
            self.error = error.localizedDescription
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

            if let error = sched.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(8)
            }

            if sched.schedules.isEmpty && !sched.loading {
                ContentUnavailableView {
                    Label("No schedules", systemImage: "clock.arrow.circlepath")
                } description: {
                    Text("Create one from the CLI: `roost schedule \"<goal>\" --every 6h`.")
                }
            } else {
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
