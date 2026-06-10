import Foundation

/// Fleet sheet store (R121, API.md §2a): polls `GET /workers` every 5 s while
/// the sheet is up (paused when it isn't — same foreground-only discipline as
/// the dashboard, DESIGN §7) and exposes the display-sorted rows. All
/// formatting/sorting/staleness judgment lives in the pure `Fleet` layer
/// (Foundation-only, Linux-tested); this store is just the iOS orchestration
/// around the client, mirroring `DashboardStore`.
@MainActor
final class FleetStore: ObservableObject {
    @Published private(set) var workers: [Worker] = []
    /// True once any fetch finished (drives the empty-state vs spinner choice).
    @Published private(set) var loaded = false
    @Published private(set) var lastError: String?

    private weak var app: AppState?
    private var pollTask: Task<Void, Never>?
    /// Workers heartbeat every ~10 s and the staleness bands are 45/120 s —
    /// 5 s keeps the sheet honest without hammering the CP from a phone.
    private let pollInterval: UInt64 = 5_000_000_000

    func bind(_ app: AppState) { self.app = app }

    /// Rows in display order (busy → idle → stale → unknown → offline, then
    /// name) — the pure layer owns the ordering contract.
    var sortedWorkers: [Worker] { Fleet.sorted(workers) }

    /// Headline counts: up = server-live AND fresh by the client clock.
    func upCount(now: Double) -> Int {
        workers.filter { Fleet.isUp(status: $0.status, lastSeen: $0.lastSeen, now: now) }.count
    }

    func start() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: self?.pollInterval ?? 5_000_000_000)
            }
        }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    /// Fetch the fleet (API.md §2a). 401 → drop to pairing; 403 → show, stay
    /// paired (§1); transport errors keep the last rows and say so.
    func refresh() async {
        guard let api = app?.api else { return }
        do {
            workers = try await api.workers()
            lastError = nil
            loaded = true
        } catch ApiError.unauthorized {
            app?.handleUnauthorized()
        } catch let ApiError.forbidden(detail) {
            lastError = "Not allowed: \(detail)"
            loaded = true
        } catch {
            // Keep showing the last list; the per-row last-seen ages keep
            // ticking (R75), so stale rows degrade honestly on their own.
            lastError = "Offline — showing last data."
            loaded = true
        }
    }
}
