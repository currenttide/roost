import XCTest
@testable import RoostKit

/// R82: the Transfers pane showed "expires 0s from now" for every staged blob
/// (user-testing/mac-app/mainwindow-transfers.png) because the old `timeAgo`
/// was past-tense only — it clamped `now - epoch` to ≥ 0, so any FUTURE epoch
/// rendered "0s" and the "ago"→"from now" string swap produced "0s from now".
/// These tests pin a real signed formatter covering BOTH directions, with an
/// injected `now` so they never depend on the wall clock.
final class RelativeTimeTests: XCTestCase {
    // A fixed reference instant so every case is deterministic.
    private let now: Double = 1_000_000

    // MARK: future — the bug

    /// The crux of R82: a blob expiring two hours out must read "in 2h", NOT
    /// "0s" / "0s from now". This is the case the old clamp got wrong.
    func testFutureTwoHoursReadsInTwoHours() {
        XCTAssertEqual(RelativeTime.signed(now + 2 * 3600, now: now), "in 2h")
    }

    func testFutureSecondsReadsInSeconds() {
        XCTAssertEqual(RelativeTime.signed(now + 45, now: now), "in 45s")
    }

    func testFutureMinutesReadsInMinutes() {
        XCTAssertEqual(RelativeTime.signed(now + 5 * 60, now: now), "in 5m")
    }

    func testFutureDaysReadsInDays() {
        XCTAssertEqual(RelativeTime.signed(now + 3 * 86_400, now: now), "in 3d")
    }

    /// A realistic staged-blob TTL (hours out) must never collapse to "0s".
    func testFutureNeverCollapsesToZeroSeconds() {
        let out = RelativeTime.signed(now + 3 * 3600, now: now)
        XCTAssertEqual(out, "in 3h")
        XCTAssertFalse(out.contains("0s"), "future TTL must not render as 0s: \(out)")
    }

    // MARK: past — unchanged from the historic timeAgo buckets

    func testPastSecondsReadsAgo() {
        XCTAssertEqual(RelativeTime.signed(now - 30, now: now), "30s ago")
    }

    func testPastMinutesReadsAgo() {
        XCTAssertEqual(RelativeTime.signed(now - 5 * 60, now: now), "5m ago")
    }

    func testPastHoursReadsAgo() {
        XCTAssertEqual(RelativeTime.signed(now - 2 * 3600, now: now), "2h ago")
    }

    func testPastDaysReadsAgo() {
        XCTAssertEqual(RelativeTime.signed(now - 4 * 86_400, now: now), "4d ago")
    }

    // MARK: edges

    func testNilEpochIsEmDash() {
        XCTAssertEqual(RelativeTime.signed(nil, now: now), "—")
    }

    func testZeroEpochIsEmDash() {
        XCTAssertEqual(RelativeTime.signed(0, now: now), "—")
    }

    func testSubSecondEitherDirectionReadsNow() {
        XCTAssertEqual(RelativeTime.signed(now + 0.4, now: now), "now")
        XCTAssertEqual(RelativeTime.signed(now - 0.4, now: now), "now")
        XCTAssertEqual(RelativeTime.signed(now, now: now), "now")
    }

    func testAgoAliasMatchesSigned() {
        XCTAssertEqual(RelativeTime.ago(now - 90, now: now),
                       RelativeTime.signed(now - 90, now: now))
        XCTAssertEqual(RelativeTime.ago(now + 90, now: now), "in 1m")
    }

    // MARK: regression guard for the exact reported symptom

    /// Documents what the OLD code did and asserts the NEW code does not: a
    /// future epoch run through the historic clamp produced "0s", which the
    /// Transfers call site swapped to "0s from now". The signed formatter must
    /// produce a real interval instead.
    func testReportedSymptomIsGone() {
        let futureEpoch = now + 6 * 3600  // 6h of TTL remaining
        // Old (buggy) behavior reproduced inline: clamp to >= 0 → 0 → "0s".
        let oldClamped = max(0, now - futureEpoch)   // == 0 for any future epoch
        XCTAssertEqual(Int(oldClamped), 0, "the old clamp zeroed every future epoch")
        // New behavior: a real signed interval, never "0s".
        XCTAssertEqual(RelativeTime.signed(futureEpoch, now: now), "in 6h")
    }
}
