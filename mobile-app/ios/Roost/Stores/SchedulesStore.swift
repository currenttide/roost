import Foundation

/// Schedules sheet store (API.md §7): list the interval schedules, create one
/// from a task + interval, toggle enable/disable, and delete. The CP re-submits
/// the stored `spec` every interval — a phone front door scheduling recurring
/// work is the point (§7).
///
/// All grammar/format/validation + list-mutation logic lives in the pure
/// `ScheduleInterval` / `ScheduleListReducer` layer (Foundation-only, Linux-tested),
/// mirroring how `PublishStore` leans on `PublishSlug`. This store is the iOS
/// orchestration around the client + the create form's draft fields.
@MainActor
final class SchedulesStore: ObservableObject {
    /// The current list, newest-first (API.md §7b). Updated by reducers so a
    /// toggle/delete reflects instantly without a full refetch.
    @Published private(set) var schedules: [Schedule] = []
    @Published private(set) var loading = false
    @Published private(set) var creating = false
    /// Inline error for the list / create form; cleared as the user edits.
    @Published var error: String?

    // MARK: Create-form draft (mirrors `roost schedule "<goal>" --every <i>`)

    /// The task one-liner the scheduled job runs each interval.
    @Published var taskText: String = ""
    /// The `every` string sent to the server (seconds or `<N>[smhd]`); seeded to a
    /// sensible default cadence, editable, validated against the server's grammar.
    @Published var every: String = "6h"
    /// Optional human label for the schedule.
    @Published var name: String = ""
    /// Agent (`claude`) vs raw `command`, mirroring the New-session toggle.
    @Published var isCommand: Bool = false

    private weak var app: AppState?

    func bind(_ app: AppState) { self.app = app }

    // MARK: Create-form validation (delegates to the pure layer)

    /// Live preview of the interval the server will store, or nil when `every`
    /// can't be parsed / is below the floor (so we never show a bogus cadence).
    var intervalPreview: String? {
        guard let sec = ScheduleInterval.parse(every),
              sec >= ScheduleInterval.minSeconds
        else { return nil }
        return ScheduleInterval.format(sec)
    }

    /// The reason `every` is rejected, or nil when valid (drives the field hint).
    var intervalMessage: String? { ScheduleInterval.validationMessage(every) }

    /// Create is allowed once there's a task, a valid interval, and we're idle —
    /// exactly the conditions under which `POST /schedules` will return 200.
    var canCreate: Bool {
        !taskText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && ScheduleInterval.isValid(every)
            && !creating
    }

    // MARK: Loads / mutations

    /// Fetch the schedules list (API.md §7b). 401 → drop to pairing; 403 → show.
    func load() async {
        guard let api = app?.api else { return }
        loading = true
        defer { loading = false }
        do {
            schedules = try await api.schedules()
            error = nil
        } catch ApiError.unauthorized {
            app?.handleUnauthorized()
        } catch let ApiError.forbidden(detail) {
            error = "Not allowed: \(detail)"
        } catch {
            self.error = "Couldn't load schedules."
        }
    }

    /// Build the §3-shaped spec from the draft. Agent jobs carry
    /// `hierarchy.can_dispatch` (the worker injects the roost MCP) exactly like a
    /// `POST /jobs` submit; command jobs carry only `command` (API.md §3/§7a).
    private func draftSpec() -> [String: JSONValue] {
        let task = taskText.trimmingCharacters(in: .whitespacesAndNewlines)
        var spec: [String: JSONValue] = ["requires": .object([:])]
        if isCommand {
            spec["kind"] = .string("command")
            spec["command"] = .string(task)
        } else {
            spec["kind"] = .string("claude")
            spec["intent"] = .string(task)
            spec["hierarchy"] = .object(["can_dispatch": .bool(true)])
        }
        return spec
    }

    /// Create a schedule from the draft, prepend it (newest-first), and reset the
    /// form. Errors map like the other stores (401 → pairing; 403/400 → show).
    func create() async {
        guard let api = app?.api else { return }
        guard ScheduleInterval.isValid(every) else {
            error = "Interval must be seconds or <N>[smhd], at least 30s."
            return
        }
        let label = name.trimmingCharacters(in: .whitespacesAndNewlines)
        creating = true
        defer { creating = false }
        error = nil
        do {
            let created = try await api.createSchedule(
                spec: draftSpec(), every: every,
                name: label.isEmpty ? nil : label)
            schedules = ScheduleListReducer.prepend(schedules, created: created)
            // Reset the draft so the form is ready for the next one.
            taskText = ""
            name = ""
        } catch ApiError.unauthorized {
            app?.handleUnauthorized()
        } catch let ApiError.forbidden(detail) {
            error = "Not allowed: \(detail)"
        } catch let ApiError.http(_, detail) {
            // §7a 400s: unparseable every, below the floor, or a bad spec.
            error = "Couldn't create: \(detail)"
        } catch {
            self.error = "Couldn't create the schedule."
        }
    }

    /// Toggle a schedule's `enabled` (API.md §7c). Re-enabling restarts the clock
    /// server-side; we just swap in the returned object. 404 → it's gone, drop it.
    func toggle(_ schedule: Schedule) async {
        guard let api = app?.api else { return }
        do {
            let updated = try await api.setScheduleEnabled(
                schedule.id, enabled: !schedule.enabled)
            schedules = ScheduleListReducer.upsertExisting(schedules, with: updated)
            error = nil
        } catch ApiError.unauthorized {
            app?.handleUnauthorized()
        } catch ApiError.notFound {
            schedules = ScheduleListReducer.remove(schedules, id: schedule.id)
        } catch let ApiError.forbidden(detail) {
            error = "Not allowed: \(detail)"
        } catch {
            self.error = "Couldn't update the schedule."
        }
    }

    /// Delete a schedule (API.md §7d). On success (or a 404 — already gone) drop
    /// it from the list.
    func delete(_ schedule: Schedule) async {
        guard let api = app?.api else { return }
        do {
            _ = try await api.deleteSchedule(schedule.id)
            schedules = ScheduleListReducer.remove(schedules, id: schedule.id)
            error = nil
        } catch ApiError.unauthorized {
            app?.handleUnauthorized()
        } catch ApiError.notFound {
            schedules = ScheduleListReducer.remove(schedules, id: schedule.id)
        } catch let ApiError.forbidden(detail) {
            error = "Not allowed: \(detail)"
        } catch {
            self.error = "Couldn't delete the schedule."
        }
    }
}
