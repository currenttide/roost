import XCTest
@testable import Roost

/// Pure-logic tests for the interval-schedule flow (API.md §7): the `every`
/// grammar (parse + 30 s floor), the compact interval formatter, and the list
/// reducers. All Foundation-only, so they run on the Linux harness AND in the iOS
/// bundle. The grammar block is a CROSS-CONTRACT pin: every literal here is copied
/// from the server's `tests/test_schedules.py` (`test_parse_every_units_and_numbers`,
/// `test_parse_every_garbage`, `test_create_validates_interval`) so client/server
/// drift on the accepted/rejected set is caught.
final class SchedulesTests: XCTestCase {

    // MARK: every grammar — mirrors server.py `parse_every` + the 30s floor

    /// Accepted values, copied verbatim from `tests/test_schedules.py::
    /// test_parse_every_units_and_numbers` (the string cases — the phone always
    /// sends `every` as a string). Each must parse to the SAME seconds the server
    /// computes, or the Create button would mislead the user.
    func testParseEveryUnitsAndNumbers() {
        XCTAssertEqual(ScheduleInterval.parse("30s"), 30)
        XCTAssertEqual(ScheduleInterval.parse("5m"), 300)
        XCTAssertEqual(ScheduleInterval.parse("2h"), 7200)
        XCTAssertEqual(ScheduleInterval.parse("1d"), 86400)
        XCTAssertEqual(ScheduleInterval.parse("90"), 90)         // bare numeric string
        XCTAssertEqual(ScheduleInterval.parse("1.5h"), 5400)     // decimal value
    }

    /// Rejected values, copied from `test_parse_every_garbage` (the string cases) —
    /// these must return nil exactly as the server's `parse_every` returns None.
    func testParseEveryGarbage() {
        XCTAssertNil(ScheduleInterval.parse("soon"))
        XCTAssertNil(ScheduleInterval.parse("5 fortnights"))
        XCTAssertNil(ScheduleInterval.parse(""))
        XCTAssertNil(ScheduleInterval.parse("h"))            // unit with no number
        XCTAssertNil(ScheduleInterval.parse("5x"))           // unknown unit
        XCTAssertNil(ScheduleInterval.parse("5m30s"))        // single unit only
    }

    /// The server lowercases `every` before matching, so an uppercase unit parses.
    func testParseEveryIsCaseInsensitive() {
        XCTAssertEqual(ScheduleInterval.parse("6H"), 21600)
        XCTAssertEqual(ScheduleInterval.parse("1D"), 86400)
    }

    /// Surrounding whitespace is tolerated by `_EVERY_RE` (`^\s* … \s*$`).
    func testParseEveryToleratesWhitespace() {
        XCTAssertEqual(ScheduleInterval.parse("  30m  "), 1800)
        XCTAssertEqual(ScheduleInterval.parse(" 90 "), 90)
    }

    // MARK: 30s floor — mirrors SCHEDULE_MIN_INTERVAL_SEC + test_create_validates_interval

    func testFloorMatchesServer() {
        // `test_create_validates_interval` posts "5s" and expects a 400 (under floor).
        XCTAssertNotNil(ScheduleInterval.parse("5s"))        // parses fine…
        XCTAssertFalse(ScheduleInterval.isValid("5s"))       // …but is below the floor
        XCTAssertFalse(ScheduleInterval.isValid("29"))       // bare seconds, under floor
        // Exactly at the floor is accepted (server uses `interval < MIN`).
        XCTAssertTrue(ScheduleInterval.isValid("30s"))
        XCTAssertTrue(ScheduleInterval.isValid("30"))
        XCTAssertEqual(ScheduleInterval.minSeconds, 30)
    }

    func testIsValidRejectsUnparseable() {
        XCTAssertFalse(ScheduleInterval.isValid("soon"))
        XCTAssertFalse(ScheduleInterval.isValid(""))
    }

    func testValidationMessageDistinguishesCauses() {
        XCTAssertNil(ScheduleInterval.validationMessage(""))          // empty → no message
        XCTAssertNil(ScheduleInterval.validationMessage("6h"))        // valid → no message
        XCTAssertEqual(ScheduleInterval.validationMessage("soon"),
                       "Use seconds or <N>[smhd] — e.g. 30s, 15m, 6h, 1d.")
        XCTAssertEqual(ScheduleInterval.validationMessage("5s"),
                       "Minimum interval is 30s.")
    }

    func testAllPresetsAreValid() {
        for preset in ScheduleIntervalPreset.all {
            XCTAssertTrue(ScheduleInterval.isValid(preset), "preset not valid: \(preset)")
        }
    }

    // MARK: format — byte-for-byte cli.py `_fmt_interval` (30s / 5m / 6h / 1d)

    func testFormatPrefersLargestWholeUnit() {
        XCTAssertEqual(ScheduleInterval.format(30), "30s")
        XCTAssertEqual(ScheduleInterval.format(300), "5m")
        XCTAssertEqual(ScheduleInterval.format(1800), "30m")
        XCTAssertEqual(ScheduleInterval.format(21600), "6h")
        XCTAssertEqual(ScheduleInterval.format(86400), "1d")
        // 21600 is a whole number of hours but not days → hours, like the CLI.
        XCTAssertEqual(ScheduleInterval.format(3600), "1h")
        // 90s is not a whole minute → seconds.
        XCTAssertEqual(ScheduleInterval.format(90), "90s")
        // 5400 (1.5h) is whole minutes but not hours → minutes.
        XCTAssertEqual(ScheduleInterval.format(5400), "90m")
    }

    /// Round-trip: a formatted interval re-parses to the same seconds (for the
    /// presets, which are exact whole units).
    func testFormatParseRoundTrip() {
        for sec in [30.0, 300, 900, 1800, 3600, 21600, 43200, 86400] {
            let formatted = ScheduleInterval.format(sec)
            XCTAssertEqual(ScheduleInterval.parse(formatted), sec,
                           "round-trip failed for \(sec) → \(formatted)")
        }
    }

    // MARK: relative clock

    func testRelativeFutureAndPast() {
        XCTAssertEqual(ScheduleInterval.relative(to: 1_000 + 1800, now: 1_000), "in 30m")
        XCTAssertEqual(ScheduleInterval.relative(to: 1_000 - 3600, now: 1_000), "1h ago")
        XCTAssertNil(ScheduleInterval.relative(to: nil, now: 1_000))
    }

    func testRelativeDueReadsAsNow() {
        // A next-run at/just-before now reads "now", never a negative interval.
        XCTAssertEqual(ScheduleInterval.relative(to: 1_000, now: 1_000), "now")
        XCTAssertEqual(ScheduleInterval.relative(to: 999.5, now: 1_000), "now")
    }

    // MARK: list reducers (API.md §7b–§7d)

    private func sched(_ id: String, enabled: Bool = true, name: String? = nil) -> Schedule {
        Schedule(id: id, name: name, spec: nil, intervalSec: 1800, enabled: enabled,
                 nextRunAt: 1_000, lastRunAt: nil, lastJobId: nil, createdAt: 1)
    }

    func testPrependPutsNewestFirstAndDedupes() {
        let list = [sched("a"), sched("b")]
        let out = ScheduleListReducer.prepend(list, created: sched("c"))
        XCTAssertEqual(out.map(\.id), ["c", "a", "b"])
        // A create that races a refresh (same id already present) doesn't double it.
        let out2 = ScheduleListReducer.prepend(out, created: sched("a"))
        XCTAssertEqual(out2.map(\.id), ["a", "c", "b"])
    }

    func testUpsertReplacesInPlace() {
        let list = [sched("a", enabled: true), sched("b", enabled: true)]
        let out = ScheduleListReducer.upsertExisting(list, with: sched("a", enabled: false))
        XCTAssertEqual(out.map(\.id), ["a", "b"])           // order preserved
        XCTAssertEqual(out.first?.enabled, false)           // object swapped
    }

    func testUpsertUnknownIdLeavesListUnchanged() {
        let list = [sched("a"), sched("b")]
        let out = ScheduleListReducer.upsertExisting(list, with: sched("z"))
        XCTAssertEqual(out.map(\.id), ["a", "b"])           // never fabricates a row
    }

    func testRemoveDropsById() {
        let list = [sched("a"), sched("b"), sched("c")]
        XCTAssertEqual(ScheduleListReducer.remove(list, id: "b").map(\.id), ["a", "c"])
        // Removing an id that isn't present is a no-op.
        XCTAssertEqual(ScheduleListReducer.remove(list, id: "z").map(\.id), ["a", "b", "c"])
    }
}
