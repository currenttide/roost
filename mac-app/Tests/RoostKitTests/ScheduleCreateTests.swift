import XCTest
@testable import RoostKit

/// Linux-runnable tests for the schedule-create finisher (R124): the create-form
/// decision layer (`ScheduleDraft` — when Create enables, the exact spec shape,
/// the label/preview rules), the preset row, the `POST /schedules` body wire
/// shape, and the single-instance yield decision. The interval grammar itself is
/// already pinned in VerbsTests (R62); these cover the new pure logic on top.
final class ScheduleCreateTests: XCTestCase {

    // MARK: - presets

    func testPresetsAreAllValidEveryStrings() {
        // Every chip must be acceptable to the server by construction, and must
        // round-trip through the CLI's compact display unchanged (chip == label).
        for preset in ScheduleIntervalPreset.all {
            XCTAssertTrue(ScheduleInterval.isValid(preset), "preset \(preset) invalid")
            let sec = ScheduleInterval.parse(preset)!
            XCTAssertEqual(ScheduleInterval.format(sec), preset)
        }
        XCTAssertEqual(ScheduleIntervalPreset.all.first, "30s")  // the server floor
    }

    // MARK: - draft: Create gating

    func testCanCreateRequiresTaskAndValidInterval() {
        XCTAssertFalse(ScheduleDraft().canCreate)                       // no task yet
        XCTAssertFalse(ScheduleDraft(task: "   \n").canCreate)          // whitespace-only
        XCTAssertTrue(ScheduleDraft(task: "check disks").canCreate)     // default 6h valid
        XCTAssertFalse(ScheduleDraft(task: "x", every: "29s").canCreate)  // below floor
        XCTAssertFalse(ScheduleDraft(task: "x", every: "soon").canCreate) // unparseable
        XCTAssertFalse(ScheduleDraft(task: "x", every: "").canCreate)
        XCTAssertTrue(ScheduleDraft(task: "x", every: "90").canCreate)  // bare seconds
    }

    func testIntervalPreviewAndMessage() {
        // Valid → compact preview, no message.
        let valid = ScheduleDraft(task: "x", every: "90m")
        XCTAssertEqual(valid.intervalPreview, "90m")
        XCTAssertNil(valid.intervalMessage)
        // Below floor → no preview, the floor message.
        let low = ScheduleDraft(task: "x", every: "5s")
        XCTAssertNil(low.intervalPreview)
        XCTAssertEqual(low.intervalMessage, "Minimum interval is 30s.")
        // Unparseable → no preview, the grammar message.
        let bad = ScheduleDraft(task: "x", every: "tomorrow")
        XCTAssertNil(bad.intervalPreview)
        XCTAssertEqual(bad.intervalMessage,
                       "Use seconds or <N>[smhd] — e.g. 30s, 15m, 6h, 1d.")
        // Empty → neither (the field hint shows instead).
        XCTAssertNil(ScheduleDraft(task: "x", every: "").intervalMessage)
    }

    func testTrimmedNameDropsBlankLabels() {
        XCTAssertNil(ScheduleDraft().trimmedName)
        XCTAssertNil(ScheduleDraft(name: "   ").trimmedName)
        XCTAssertEqual(ScheduleDraft(name: "  nightly \n").trimmedName, "nightly")
    }

    // MARK: - draft: spec shape

    func testAgentSpecIsTheCLIScheduleShape() {
        // `roost schedule "<goal>"` sends {"kind": "auto", "task": goal} — the
        // worker self-assesses fit and the trust loop verifies (cli.py).
        let spec = ScheduleDraft(task: "  check disk space  ").spec()
        XCTAssertEqual(spec, ["kind": .string("auto"),
                              "task": .string("check disk space")])
    }

    func testCommandSpecRunsTheLineVerbatim() {
        let spec = ScheduleDraft(task: "df -h", isCommand: true).spec()
        XCTAssertEqual(spec, ["kind": .string("command"),
                              "command": .string("df -h")])
    }

    func testSpecIsAlwaysARootJob() {
        // The server 400s schedule specs carrying parent linkage; the draft must
        // never produce one.
        for draft in [ScheduleDraft(task: "x"), ScheduleDraft(task: "x", isCommand: true)] {
            let spec = draft.spec()
            XCTAssertNil(spec["parent_job_id"])
            XCTAssertNil(spec["captain_root"])
        }
    }

    // MARK: - POST /schedules wire shape (server ScheduleCreate)

    func testCreateBodyEncodesServerContract() throws {
        let body = ScheduleCreateBody(
            spec: ScheduleDraft(task: "check disks").spec(),
            every: "6h", name: "nightly", enabled: true)
        let json = try JSONSerialization.jsonObject(
            with: JSONEncoder().encode(body)) as? [String: Any]
        XCTAssertEqual(json?["every"] as? String, "6h")
        XCTAssertEqual(json?["name"] as? String, "nightly")
        XCTAssertEqual(json?["enabled"] as? Bool, true)
        let spec = json?["spec"] as? [String: Any]
        XCTAssertEqual(spec?["kind"] as? String, "auto")
        XCTAssertEqual(spec?["task"] as? String, "check disks")
    }

    func testCreateBodyOmitsNilName() throws {
        // No --name → the key is absent (not null), like the CLI's payload.
        let body = ScheduleCreateBody(spec: ["kind": .string("auto")], every: "30s")
        let json = try JSONSerialization.jsonObject(
            with: JSONEncoder().encode(body)) as? [String: Any]
        XCTAssertNil(json?["name"])
        XCTAssertEqual(json?["enabled"] as? Bool, true)  // default on, like the server
    }

    // MARK: - single-instance yield decision

    private func inst(_ pid: Int32, _ secondsAgo: Double? = nil) -> SingleInstance.Instance {
        SingleInstance.Instance(
            pid: pid, launchedAt: secondsAgo.map { Date(timeIntervalSinceNow: -$0) })
    }

    func testSoleInstanceProceeds() {
        // The query result includes the current process; alone → launch.
        XCTAssertNil(SingleInstance.instanceToYieldTo(
            selfPID: 42, instances: [inst(42, 0)]))
        XCTAssertNil(SingleInstance.instanceToYieldTo(selfPID: 42, instances: []))
    }

    func testSecondLaunchYieldsToTheRunningInstance() {
        // The UAT nit: Roost is already up (launched an hour ago); a second
        // launch must hand off to it, not start a second bird.
        XCTAssertEqual(SingleInstance.instanceToYieldTo(
            selfPID: 9000, instances: [inst(500, 3600), inst(9000, 0)]), 500)
        // pid wraparound: the running instance can have the HIGHER pid — launch
        // date, not pid, carries seniority.
        XCTAssertEqual(SingleInstance.instanceToYieldTo(
            selfPID: 42, instances: [inst(9000, 3600), inst(42, 0)]), 9000)
    }

    func testUnknownLaunchDateStillCountsAsRunning() {
        // NSRunningApplication.launchDate is optional; a sibling without one is
        // still an existing instance — defer to it rather than duplicate.
        XCTAssertEqual(SingleInstance.instanceToYieldTo(
            selfPID: 9000, instances: [inst(500), inst(9000, 0)]), 500)
    }

    func testRacedLaunchesConvergeOnExactlyOneSurvivor() {
        // Two simultaneous launches each see the other. A naive "any sibling →
        // quit" would kill BOTH; the seniority rank must pick one winner that
        // every racer agrees on.
        let racers = [inst(42, 1.0), inst(9000, 0.5)]  // 42 launched first
        XCTAssertNil(SingleInstance.instanceToYieldTo(selfPID: 42, instances: racers))
        XCTAssertEqual(
            SingleInstance.instanceToYieldTo(selfPID: 9000, instances: racers), 42)
    }

    func testRacedLaunchesWithoutDatesTieBreakByPID() {
        let racers = [inst(42), inst(9000)]
        XCTAssertNil(SingleInstance.instanceToYieldTo(selfPID: 42, instances: racers))
        XCTAssertEqual(
            SingleInstance.instanceToYieldTo(selfPID: 9000, instances: racers), 42)
    }
}
