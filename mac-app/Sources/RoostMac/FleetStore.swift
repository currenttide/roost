#if os(macOS)
import AppKit
import Foundation
import Observation
import RoostKit

/// What changed between two snapshots — drives notifications (DESIGN.md §5).
struct FleetDiff {
    var finishedRuns: [Run] = []          // active → terminal this tick
    var becameStuck: [Run] = []           // health flipped to "stuck?"
    var verdictBecameAlert: FleetVerdict?
    var workersWentOffline: [Worker] = []
}

/// Single source of truth: fleet verdict, workers, runs. Fed by one poll loop
/// on GET /derived; views render this and never touch the network (§3).
@MainActor
@Observable
final class FleetStore {
    enum Reachability: Equatable {
        case ok
        case unreachable(String)
        case unauthorized
        case never                         // not configured yet
    }

    private(set) var client: RoostClient?
    private(set) var snapshot: DerivedSnapshot?
    private(set) var lastUpdated: Date?
    private(set) var reachability: Reachability = .never

    /// Runs submitted from the goal box this session, shown immediately and
    /// reconciled away once they appear in a snapshot (§4).
    private(set) var optimisticRuns: [(id: String, goal: String, at: Date)] = []

    /// Raised history window while the main window is open ("Load more").
    var historyLimit = 40

    /// Poll cadence while visible; user-overridable in Settings (default 2 s).
    @ObservationIgnored var visibleCadence: Double = 2

    /// True while popover or main window is visible — drives cadence.
    var uiVisible = false {
        didSet { if uiVisible != oldValue { poke() } }
    }

    /// Hook for NotificationManager; called off the poll loop with each diff.
    @ObservationIgnored var onDiff: ((FleetDiff) -> Void)?

    private var pollTask: Task<Void, Never>?
    private var consecutiveFailures = 0
    private var sleepObservers: [NSObjectProtocol] = []

    var isConfigured: Bool { client != nil }

    // MARK: derived accessors

    var verdict: FleetVerdict? { snapshot?.fleetVerdict }
    var workers: [Worker] { snapshot?.workers ?? [] }

    var activeRuns: [Run] {
        (snapshot?.runs ?? []).filter(\.isActive)
            .sorted { ($0.createdAt ?? 0) > ($1.createdAt ?? 0) }
    }

    var recentRuns: [Run] {
        (snapshot?.runs ?? []).filter(\.isTerminal)
            .sorted { ($0.finishedAt ?? $0.createdAt ?? 0) > ($1.finishedAt ?? $1.createdAt ?? 0) }
    }

    var hasActivity: Bool {
        !activeRuns.isEmpty || !optimisticRuns.isEmpty
    }

    func run(id: String) -> Run? {
        snapshot?.runs.first { $0.id == id }
    }

    func worker(id: String?) -> Worker? {
        guard let id else { return nil }
        return workers.first { $0.id == id }
    }

    // MARK: lifecycle

    func configure(_ connection: RoostConnection?) {
        if let connection {
            client = RoostClient(connection: connection)
            reachability = .ok
        } else {
            client = nil
            reachability = .never
        }
        snapshot = nil
        consecutiveFailures = 0
        restartLoop()
    }

    func start() {
        watchSleepWake()
        restartLoop()
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    /// Wake the loop now (visibility change, submit, manual refresh).
    func poke() {
        restartLoop()
    }

    private func restartLoop() {
        pollTask?.cancel()
        guard client != nil else { return }
        pollTask = Task { [weak self] in
            while let self, !Task.isCancelled {
                await self.tick()
                let delay = self.nextDelay()
                // ±10% jitter so several Macs watching one CP don't sync-storm
                let jittered = delay * Double.random(in: 0.9...1.1)
                do {
                    try await Task.sleep(nanoseconds: UInt64(jittered * 1_000_000_000))
                } catch { return }
            }
        }
    }

    /// DESIGN.md §5: 2 s visible · 20 s ambient-with-activity/alert · 60 s idle.
    /// While failing: backoff 5 s → 60 s against /healthz-class errors.
    private func nextDelay() -> Double {
        if case .unreachable = reachability {
            return min(60, 5 * pow(2, Double(max(0, consecutiveFailures - 1))))
        }
        if uiVisible { return visibleCadence }
        if hasActivity || verdict?.level == .alert { return 20 }
        return 60
    }

    private func tick() async {
        guard let client else { return }
        do {
            let limit = uiVisible ? max(40, historyLimit) : 40
            let fresh = try await client.derived(limit: limit)
            let diff = diffAgainstCurrent(fresh)
            snapshot = fresh
            lastUpdated = Date()
            reachability = .ok
            consecutiveFailures = 0
            reconcileOptimistic(with: fresh)
            if let diff { onDiff?(diff) }
        } catch RoostClientError.unauthorized {
            reachability = .unauthorized
        } catch {
            consecutiveFailures += 1
            reachability = .unreachable(error.localizedDescription)
        }
    }

    // MARK: diffing (mechanical — only backend-derived states, §5)

    private func diffAgainstCurrent(_ fresh: DerivedSnapshot) -> FleetDiff? {
        guard let old = snapshot else { return nil }  // first snapshot: no noise
        var diff = FleetDiff()

        let oldRuns = Dictionary(old.runs.map { ($0.id, $0) }) { first, _ in first }
        for run in fresh.runs {
            guard let prev = oldRuns[run.id] else { continue }
            if prev.isActive && run.isTerminal {
                diff.finishedRuns.append(run)
            }
            if run.health.status == "stuck?" && prev.health.status != "stuck?" {
                diff.becameStuck.append(run)
            }
        }

        if old.fleetVerdict.level == .ok && fresh.fleetVerdict.level == .alert {
            diff.verdictBecameAlert = fresh.fleetVerdict
        }

        let oldWorkers = Dictionary(old.workers.map { ($0.id, $0) }) { first, _ in first }
        for worker in fresh.workers where worker.status == .offline {
            if let prev = oldWorkers[worker.id], prev.status != .offline {
                diff.workersWentOffline.append(worker)
            }
        }

        let empty = diff.finishedRuns.isEmpty && diff.becameStuck.isEmpty
            && diff.verdictBecameAlert == nil && diff.workersWentOffline.isEmpty
        return empty ? nil : diff
    }

    // MARK: actions

    @discardableResult
    func submitGoal(
        _ text: String, captain: Bool = false, verify: Bool = true,
        preferWorker: String? = nil, model: String? = nil, maxTokens: Int? = nil
    ) async throws -> Job {
        guard let client else { throw RoostClientError.transport("not connected") }
        let job = try await client.submit(.goal(
            text, captain: captain, verify: verify,
            preferWorker: preferWorker, model: model, maxTokens: maxTokens))
        optimisticRuns.append((id: job.id, goal: job.goal, at: Date()))
        poke()  // pull the run into the snapshot promptly
        return job
    }

    func cancelRun(id: String, tree: Bool = true) async throws {
        guard let client else { return }
        try await client.cancel(id: id, tree: tree)
        poke()
    }

    /// Resubmit a failed run's goal as a NEW run (the app never mutates a
    /// finished job).
    @discardableResult
    func retry(run: Run) async throws -> Job {
        try await submitGoal(run.goal)
    }

    private func reconcileOptimistic(with fresh: DerivedSnapshot) {
        let known = Set(fresh.runs.map(\.id))
        optimisticRuns.removeAll {
            known.contains($0.id) || $0.at.timeIntervalSinceNow < -15
        }
    }

    // MARK: sleep/wake (§5 — suspend immediately, no wake-from-sleep error spam)

    private func watchSleepWake() {
        guard sleepObservers.isEmpty else { return }
        let center = NSWorkspace.shared.notificationCenter
        sleepObservers.append(center.addObserver(
            forName: NSWorkspace.willSleepNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.stop() }
        })
        sleepObservers.append(center.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.restartLoop() }
        })
    }
}
#endif
