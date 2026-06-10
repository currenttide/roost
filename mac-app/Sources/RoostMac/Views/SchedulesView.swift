#if os(macOS)
import RoostKit
import SwiftUI

// Schedules verb: list the interval schedules, enable/disable each one, and
// create one from a task + interval (R124 — the create half R62 deferred). The
// clock (`every` / next-run / last-run) renders with the same grammar the CLI uses
// (RoostKit `ScheduleInterval`, Linux-tested); the create sheet's validation and
// spec shape live in RoostKit `ScheduleDraft` (also Linux-tested), mirroring the
// CLI's `roost schedule "<goal>" --every <i>` rather than a full job-spec composer.

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

    /// Create-in-flight flag, so the sheet's button shows progress and can't
    /// double-submit.
    private(set) var creating = false

    /// Create a schedule from the sheet's draft and prepend it (the list is
    /// newest-first, matching `GET /schedules`). Throws so the sheet can show the
    /// failure inline next to the form instead of in the pane behind it.
    func create(_ draft: ScheduleDraft) async throws {
        guard let client = store.client else {
            throw RoostClientError.transport("Not connected to a control plane.")
        }
        creating = true
        defer { creating = false }
        let created = try await client.createSchedule(
            spec: draft.spec(), every: draft.every, name: draft.trimmedName)
        schedules.removeAll { $0.id == created.id }
        schedules.insert(created, at: 0)
        hasLoaded = true  // a created row is data — never regress to the empty state
        loadError = nil
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
    @State private var showCreate = false

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
                Button("New Schedule…") { showCreate = true }
                    .controlSize(.small)
                    // An older CP without /schedules can't create one either.
                    .disabled(sched.displayState == .unavailable)
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
                    Text("Run a task on a fixed interval — e.g. check disk space every 6h.")
                } actions: {
                    Button("New Schedule…") { showCreate = true }
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
        .sheet(isPresented: $showCreate) {
            CreateScheduleSheet(sched: sched)
        }
    }
}

// MARK: - create sheet

/// Create a schedule from a task + interval (+ optional label) — the sheet form
/// of `roost schedule "<goal>" --every <i>`, per the SendFileSheet pattern. All
/// judgment (Create gating, validation copy, the spec the CP stores) lives in
/// RoostKit's `ScheduleDraft`, Linux-tested; this is the SwiftUI shell.
struct CreateScheduleSheet: View {
    @Environment(\.dismiss) private var dismiss

    let sched: SchedulesModel
    @State private var draft = ScheduleDraft()
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("New schedule").font(.headline)

            VStack(alignment: .leading, spacing: 4) {
                Text("Task to run each interval")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("check disk space on every box", text: $draft.task)
                    .textFieldStyle(.roundedBorder)
                    .frame(minWidth: 360)
                Picker("", selection: $draft.isCommand) {
                    Text("Agent").tag(false)
                    Text("Command").tag(true)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                Text(draft.isCommand
                        ? "Runs the line verbatim on a worker's shell."
                        : "An agent takes it on; the trust loop verifies the result.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("Every")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                HStack(spacing: 6) {
                    TextField("6h", text: $draft.every)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 64)
                    // Quick cadences (the R61 preset row); free text accepts the
                    // same `every` grammar the server does.
                    ForEach(ScheduleIntervalPreset.all, id: \.self) { preset in
                        Button(preset) { draft.every = preset }
                            .buttonStyle(.plain)
                            .font(.caption.weight(.medium))
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(
                                draft.every == preset
                                    ? Color.accentColor.opacity(0.2)
                                    : Color.secondary.opacity(0.12),
                                in: Capsule())
                            .foregroundStyle(
                                draft.every == preset ? Color.accentColor : .primary)
                    }
                }
                // One quiet footer line: the live cadence preview, the reason the
                // interval is rejected, or the grammar hint — never stacked.
                if let preview = draft.intervalPreview {
                    Text("Runs every \(preview); first run one interval from now.")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                } else if let message = draft.intervalMessage {
                    Text(message)
                        .font(.caption2)
                        .foregroundStyle(.red)
                } else {
                    Text("Seconds or <N>[smhd] — e.g. 30s, 15m, 6h, 1d. Minimum 30s.")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("Label (optional)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("nightly", text: $draft.name)
                    .textFieldStyle(.roundedBorder)
            }

            if let error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Create") { create() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!draft.canCreate || sched.creating)
            }
        }
        .padding(20)
        .frame(width: 440)
    }

    private func create() {
        Task { @MainActor in
            do {
                try await sched.create(draft)
                dismiss()
            } catch {
                // Reuse the pane's Linux-tested error classification so a 401 says
                // "admin token" and a 404 says "older control plane", not raw HTTP.
                switch SchedulesLoadError.from(error) {
                case .endpointMissing:
                    self.error = "This control plane doesn't support schedules (older server)."
                case .message(let text):
                    self.error = text
                }
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
