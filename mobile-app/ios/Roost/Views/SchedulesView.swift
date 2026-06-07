import SwiftUI

/// Schedules sheet (API.md §7): list the interval schedules the control plane
/// re-runs on a cadence, create one from a task + interval, toggle each on/off,
/// and delete with a confirm. Mirrors `PublishView`/`NotificationSettingsView` —
/// a `Form` in its own `NavigationStack` with a Cancel/primary toolbar — and the
/// dashboard overflow-menu → sheet entry the publish + notifications flows use.
///
/// All interval grammar/format/validation + list reducers are in the pure
/// `ScheduleInterval`/`ScheduleListReducer` layer (Linux-tested); this view is the
/// SwiftUI shell over `SchedulesStore`.
struct SchedulesView: View {
    @EnvironmentObject var app: AppState
    @StateObject private var store = SchedulesStore()
    @Environment(\.dismiss) private var dismiss

    @State private var confirmDelete: Schedule?

    var body: some View {
        NavigationStack {
            Form {
                createSection

                if !store.schedules.isEmpty {
                    Section("Active schedules") {
                        ForEach(store.schedules) { schedule in
                            ScheduleRow(schedule: schedule,
                                        onToggle: { Task { await store.toggle(schedule) } },
                                        onDelete: { confirmDelete = schedule })
                        }
                    }
                } else if !store.loading {
                    Section {
                        Text("No schedules yet. Create one above to run a task on a "
                             + "fixed interval.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }

                if let error = store.error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
            }
            .navigationTitle("Schedules")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Done") { dismiss() }
                }
            }
            .confirmationDialog("Delete this schedule?",
                                isPresented: Binding(
                                    get: { confirmDelete != nil },
                                    set: { if !$0 { confirmDelete = nil } }),
                                presenting: confirmDelete) { schedule in
                Button("Delete", role: .destructive) {
                    Task { await store.delete(schedule) }
                    confirmDelete = nil
                }
                Button("Keep", role: .cancel) { confirmDelete = nil }
            } message: { schedule in
                Text(schedule.taskSummary)
            }
        }
        .onAppear { store.bind(app) }
        .task { await store.load() }
    }

    // MARK: - Create a schedule

    @ViewBuilder
    private var createSection: some View {
        Section {
            TextField("Task to run each interval", text: $store.taskText, axis: .vertical)
                .lineLimit(1...3)

            Picker("Kind", selection: $store.isCommand) {
                Text("Agent").tag(false)
                Text("Command").tag(true)
            }
            .pickerStyle(.segmented)

            // Interval presets as a quick row of chips + a free-text field that
            // accepts the same `every` grammar the server does.
            intervalPicker

            TextField("Label (optional)", text: $store.name)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()

            Button {
                Task { await store.create() }
            } label: {
                if store.creating {
                    ProgressView()
                } else {
                    Text("Create schedule")
                }
            }
            .disabled(!store.canCreate)
        } header: {
            Text("New schedule")
        } footer: {
            // The first run fires one interval from now, never immediately (§7a).
            if let preview = store.intervalPreview {
                Text("Runs every \(preview); first run one interval from now.")
            } else if let msg = store.intervalMessage {
                Text(msg).foregroundStyle(.red)
            } else {
                Text("Interval: seconds or <N>[smhd] — e.g. 30s, 15m, 6h, 1d. Minimum 30s.")
            }
        }
    }

    @ViewBuilder
    private var intervalPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            TextField("Interval (e.g. 6h)", text: $store.every)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(ScheduleIntervalPreset.all, id: \.self) { preset in
                        Button(preset) { store.every = preset }
                            .font(.caption.weight(.medium))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(
                                store.every == preset
                                    ? Color.accentColor.opacity(0.2)
                                    : Color.secondary.opacity(0.12),
                                in: Capsule())
                            .foregroundStyle(store.every == preset ? Color.accentColor : .primary)
                    }
                }
                .padding(.vertical, 2)
            }
            .buttonStyle(.plain)
        }
    }
}

/// One schedule row: enabled toggle, interval + task summary, the next-run clock,
/// and a delete button. The toggle/delete call back into the store; the row only
/// renders the authoritative `Schedule` (clock formatting via `ScheduleInterval`).
private struct ScheduleRow: View {
    let schedule: Schedule
    let onToggle: () -> Void
    let onDelete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(ScheduleInterval.format(schedule.intervalSec))
                    .font(.body.weight(.semibold).monospacedDigit())
                if let label = schedule.name, !label.isEmpty {
                    Text(label)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("", isOn: Binding(get: { schedule.enabled },
                                         set: { _ in onToggle() }))
                    .labelsHidden()
            }
            Text(schedule.taskSummary)
                .font(.callout)
                .foregroundStyle(.primary)
                .lineLimit(2)
            Text(clockLine)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button(role: .destructive, action: onDelete) {
                Label("Delete", systemImage: "trash")
            }
        }
    }

    /// "Next run in 5h" while enabled; "Paused" when off; last-run hint when known.
    private var clockLine: String {
        guard schedule.enabled else { return "Paused" }
        let now = Date().timeIntervalSince1970
        if let next = ScheduleInterval.relative(to: schedule.nextRunAt, now: now) {
            return "Next run \(next)"
        }
        return "Scheduled"
    }
}
