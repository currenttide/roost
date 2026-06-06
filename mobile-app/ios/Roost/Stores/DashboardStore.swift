import Foundation
import SwiftUI

/// Dashboard store: polls `GET /derived?limit=40` every 2 s while foregrounded,
/// pauses on background (DESIGN §3.1, API.md §2). Sorts runs running/assigned
/// first then created_at desc, computes the staleness pill, and runs
/// cancel/retry swipe actions.
@MainActor
final class DashboardStore: ObservableObject {
    @Published private(set) var derived: Derived?
    @Published private(set) var lastError: String?
    @Published private(set) var stale = false

    private weak var app: AppState?
    private var pollTask: Task<Void, Never>?
    private let pollInterval: UInt64 = 2_000_000_000   // 2 s

    func bind(_ app: AppState) { self.app = app }

    /// Sorted for display: active (running/assigned/queued…) first, then by
    /// created_at desc. Stable within groups.
    var sortedRuns: [Run] {
        guard let runs = derived?.runs else { return [] }
        return runs.enumerated().sorted { a, b in
            let aActive = isActive(a.element), bActive = isActive(b.element)
            if aActive != bActive { return aActive }    // active first
            let aC = a.element.createdAt ?? 0, bC = b.element.createdAt ?? 0
            if aC != bC { return aC > bC }               // newest first
            return a.offset < b.offset                    // stable
        }.map(\.element)
    }

    private func isActive(_ r: Run) -> Bool {
        r.state == "running" || r.state == "assigned" || r.healthStatus.isActive
    }

    /// Live node count for the chip: idle + busy workers (API.md §2).
    var liveWorkerCount: Int {
        derived?.workers.filter(\.isLive).count ?? 0
    }

    func start() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: self?.pollInterval ?? 2_000_000_000)
            }
        }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    func refresh() async {
        guard let api = app?.api else { return }
        do {
            let raw = try await api.derivedRaw(limit: 40)
            let d = try JSONDecoder().decode(Derived.self, from: raw)
            derived = d
            lastError = nil
            // Staleness: generated_at drifting > 10 s behind wall clock (§2).
            stale = (Date().timeIntervalSince1970 - d.generatedAt) > 10
            // Write-behind to the offline cache (DESIGN §5): last good payload
            // repaints the dashboard on a cold start while the CP is unreachable.
            OfflineCache.shared.saveDerivedRaw(raw)
        } catch ApiError.unauthorized {
            app?.handleUnauthorized()
        } catch let ApiError.forbidden(detail) {
            lastError = "Permission denied: \(detail)"
        } catch {
            // Offline cold start: nothing fetched yet → render the cached
            // payload; its old generated_at keeps the staleness pill honest.
            if derived == nil,
               let raw = OfflineCache.shared.loadDerivedRaw(),
               let d = try? JSONDecoder().decode(Derived.self, from: raw) {
                derived = d
            }
            lastError = "Offline — showing last data."
            stale = true
        }
    }

    /// Swipe → cancel a running job (UI confirms first).
    func cancel(_ runId: String) async {
        guard let api = app?.api else { return }
        do { try await api.cancel(runId); await refresh() }
        catch ApiError.unauthorized { app?.handleUnauthorized() }
        catch { lastError = "Cancel failed." }
    }

    /// Swipe → retry a failed job: resubmit its spec (API.md §4). We fetch the
    /// job to read its spec, then POST a fresh submit. Returns the new job id.
    @discardableResult
    func retry(_ runId: String) async -> String? {
        guard let api = app?.api else { return nil }
        do {
            let job = try await api.job(runId)
            let spec = job.spec
            let kind = spec?.kind ?? "claude"
            let submit = JobSubmit(
                intent: kind == "command" ? nil : (spec?.intent ?? job.intent),
                kind: kind,
                requires: spec?.requires ?? [:],
                command: kind == "command" ? spec?.command : nil)
            let fresh = try await api.submit(submit)
            await refresh()
            return fresh.id
        } catch ApiError.unauthorized {
            app?.handleUnauthorized(); return nil
        } catch {
            lastError = "Retry failed."; return nil
        }
    }
}
