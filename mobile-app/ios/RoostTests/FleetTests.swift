import XCTest
@testable import Roost

/// Pure-layer tests for the Fleet screen (R121, API.md §2a): the golden
/// `workers.json` decode (now busy + idle-GPU + offline rows), staleness
/// pills mirroring the server's 45/120 s thresholds (the R75 pattern), the
/// capability summary, load + last-seen lines, and the display sort. The
/// example strings here are byte-identical to Android's `FleetTest.kt` —
/// the cross-platform consistency guarantee for the two Fleet screens.
final class FleetTests: XCTestCase {
    private let dec = JSONDecoder()

    private func fixtureWorkers() throws -> [Worker] {
        try dec.decode([Worker].self, from: Fixtures.data("workers.json"))
    }

    // MARK: Fixture decode (API.md §2a row shape)

    func testWorkersFixtureCoversStatusVocabulary() throws {
        let workers = try fixtureWorkers()
        XCTAssertEqual(workers.count, 3)
        XCTAssertEqual(workers.map(\.status), ["busy", "idle", "offline"])
        XCTAssertEqual(workers.map(\.name),
                       ["fixture-node", "fixture-gpu-node", "fixture-offline-node"])
        // Load fields: the busy row runs 1/1; the others are 0/1.
        XCTAssertEqual(workers[0].running, 1)
        XCTAssertEqual(workers[0].capacity, 1)
        XCTAssertEqual(workers[1].running, 0)
        // Heterogeneous capabilities decode through JSONValue.
        XCTAssertEqual(workers[1].capabilities?["gpu_vram_gb"], .number(16))
        XCTAssertEqual(workers[1].capabilities?["hostname"], .string("gpu-host"))
        XCTAssertEqual(workers[2].capabilities?["arch"], .string("aarch64"))
        // The offline row was last seen ~10 min before the live rows.
        let gap = try XCTUnwrap(workers[0].lastSeen) - (workers[2].lastSeen ?? 0)
        XCTAssertGreaterThan(gap, Fleet.offlineAfter)
    }

    // MARK: Staleness pills (R75 pattern, server thresholds 45/120 s)

    func testPillThresholds() {
        let seen = 1_000.0
        // Fresh: no pill.
        XCTAssertNil(Fleet.pill(status: "idle", lastSeen: seen, now: seen + 10))
        // 44 s: still fresh; 45 s: stale (>= threshold).
        XCTAssertNil(Fleet.pill(status: "idle", lastSeen: seen, now: seen + 44))
        XCTAssertEqual(Fleet.pill(status: "idle", lastSeen: seen, now: seen + 45), .stale)
        // 119 s: stale; 120 s: offline.
        XCTAssertEqual(Fleet.pill(status: "busy", lastSeen: seen, now: seen + 119), .stale)
        XCTAssertEqual(Fleet.pill(status: "busy", lastSeen: seen, now: seen + 120), .offline)
    }

    func testServerWordWinsOfflineAndStale() {
        let seen = 1_000.0
        // A server-declared offline row is offline however fresh the payload.
        XCTAssertEqual(Fleet.pill(status: "offline", lastSeen: seen, now: seen + 1), .offline)
        // A server-declared stale row shows stale even with a fresh clock gap.
        XCTAssertEqual(Fleet.pill(status: "stale", lastSeen: seen, now: seen + 1), .stale)
        // …but the clock can still escalate a stale row to offline.
        XCTAssertEqual(Fleet.pill(status: "stale", lastSeen: seen, now: seen + 500), .offline)
        // No last_seen at all: trust the status word only.
        XCTAssertNil(Fleet.pill(status: "idle", lastSeen: nil, now: seen))
        XCTAssertEqual(Fleet.pill(status: "offline", lastSeen: nil, now: seen), .offline)
    }

    func testIsUpAndHeadline() throws {
        let workers = try fixtureWorkers()
        // "Now" = the busy row's heartbeat: live rows are up, offline is not.
        let now = try XCTUnwrap(workers[0].lastSeen)
        let up = workers.filter {
            Fleet.isUp(status: $0.status, lastSeen: $0.lastSeen, now: now)
        }
        XCTAssertEqual(up.map(\.name), ["fixture-node", "fixture-gpu-node"])
        XCTAssertEqual(Fleet.headline(up: up.count, total: workers.count), "2 of 3 up")
        // The same rows 10 minutes later: every heartbeat is ancient → 0 up.
        let later = now + 600
        XCTAssertEqual(workers.filter {
            Fleet.isUp(status: $0.status, lastSeen: $0.lastSeen, now: later)
        }.count, 0)
        // Unknown status never counts as up, however fresh.
        XCTAssertFalse(Fleet.isUp(status: "rebooting", lastSeen: now, now: now))
    }

    // MARK: Capability summary (cross-platform string contract)

    func testCapsSummaryFixtureRows() throws {
        let workers = try fixtureWorkers()
        // GPU node: hostname · gpu · arch · cpus (mirrors `roost workers`).
        XCTAssertEqual(Fleet.capsSummary(workers[1].capabilities),
                       "gpu-host · gpu 16GB · x86_64 · 8 cpu")
        // Offline pi: no GPU key at all → no gpu segment.
        XCTAssertEqual(Fleet.capsSummary(workers[2].capabilities),
                       "attic-pi · aarch64 · 2 cpu")
        // Original busy row advertises only tools/cpus → cpus only.
        XCTAssertEqual(Fleet.capsSummary(workers[0].capabilities), "4 cpu")
    }

    func testCapsSummaryEdgeCases() {
        XCTAssertNil(Fleet.capsSummary(nil))
        XCTAssertNil(Fleet.capsSummary([:]))
        XCTAssertNil(Fleet.capsSummary(["tools": .array([.string("python3")])]))
        // R41: a broken GPU probe is flagged, not silently bare.
        XCTAssertEqual(
            Fleet.capsSummary(["gpu_detection": .string("failed"),
                               "hostname": .string("box")]),
            "box · gpu: detection failed")
        // Fractional VRAM keeps its decimal (Jetson-style 30.7).
        XCTAssertEqual(Fleet.capsSummary(["gpu_vram_gb": .number(30.7)]), "gpu 30.7GB")
    }

    // MARK: Load + last-seen lines

    func testLoadText() {
        XCTAssertEqual(Fleet.loadText(running: 1, capacity: 1), "1/1 running")
        XCTAssertEqual(Fleet.loadText(running: 2, capacity: 4), "2/4 running")
        // Older CP omitting the fields → honest zeros over a 1-slot default.
        XCTAssertEqual(Fleet.loadText(running: nil, capacity: nil), "0/1 running")
        XCTAssertEqual(Fleet.loadText(running: 0, capacity: 0), "0/1 running")
    }

    func testLastSeenText() {
        let now = 10_000.0
        XCTAssertEqual(Fleet.lastSeenText(now - 3, now: now), "just now")
        XCTAssertEqual(Fleet.lastSeenText(now - 30, now: now), "seen 30s ago")
        XCTAssertEqual(Fleet.lastSeenText(now - 90, now: now), "seen 1m ago")
        XCTAssertEqual(Fleet.lastSeenText(now - 7_200, now: now), "seen 2h ago")
        XCTAssertEqual(Fleet.lastSeenText(now - 200_000, now: now), "seen 2d ago")
        XCTAssertEqual(Fleet.lastSeenText(nil, now: now), "never seen")
        // Clock skew (heartbeat ahead of the phone) clamps to "just now".
        XCTAssertEqual(Fleet.lastSeenText(now + 60, now: now), "just now")
    }

    // MARK: Display sort

    func testSortRanksAndTieBreaks() {
        func w(_ id: String, _ name: String, _ status: String) -> Worker {
            Worker(id: id, name: name, status: status, lastSeen: nil,
                   running: nil, capacity: nil, capabilities: nil)
        }
        let sorted = Fleet.sorted([
            w("1", "zeta", "offline"),
            w("2", "beta", "idle"),
            w("3", "alpha", "idle"),
            w("4", "mike", "busy"),
            w("5", "sierra", "stale"),
            w("6", "quark", "rebooting"),   // unknown status: above offline
        ])
        XCTAssertEqual(sorted.map(\.name),
                       ["mike", "alpha", "beta", "sierra", "quark", "zeta"])
        // Equal rank + name: id breaks the tie (total order, no row jumping).
        let dup = Fleet.sorted([w("b", "same", "idle"), w("a", "same", "idle")])
        XCTAssertEqual(dup.map(\.id), ["a", "b"])
    }

    func testSortFixture() throws {
        let sorted = Fleet.sorted(try fixtureWorkers())
        XCTAssertEqual(sorted.map(\.name),
                       ["fixture-node", "fixture-gpu-node", "fixture-offline-node"])
    }
}
