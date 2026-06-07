import XCTest
@testable import Roost

/// Pure-logic tests for the session follow-up composer (DESIGN §3.2 / API.md §4,
/// R38): the Send-enable gate, the byte-cap, and the outcome line. All
/// Foundation-only, so they run on the Linux harness AND in the iOS bundle.
/// Mirrors the Android `ComposeTest`. The 64 KiB cap is a CROSS-CONTRACT pin
/// against the server's `JOB_INPUT_MAX_BYTES` (`roost/server.py`) so client/server
/// drift is caught.
final class ComposerTests: XCTestCase {

    func testMaxBytesMatchesServer() {
        // Pinned to server.py: JOB_INPUT_MAX_BYTES = 64 * 1024.
        XCTAssertEqual(Composer.maxBytes, 64 * 1024)
    }

    func testCanSendGate() {
        XCTAssertFalse(Composer.canSend(""), "empty is rejected (server 400)")
        XCTAssertFalse(Composer.canSend("   \n\t "), "whitespace-only is rejected")
        XCTAssertTrue(Composer.canSend("re-run the test suite"))
        XCTAssertTrue(Composer.canSend("  fix the bug  "),
                      "leading/trailing space is fine if there's content")
    }

    func testByteCapUsesUTF8Length() {
        // A multi-byte char counts its UTF-8 bytes, like the server's
        // `len(text.encode("utf-8"))`. "é" is 2 bytes.
        XCTAssertEqual(Composer.byteLength("é"), 2)
        // At the cap: 64 KiB of ASCII is sendable; one byte over is not.
        let atCap = String(repeating: "a", count: Composer.maxBytes)
        XCTAssertTrue(Composer.canSend(atCap))
        XCTAssertNil(Composer.validationMessage(atCap))
        let overCap = String(repeating: "a", count: Composer.maxBytes + 1)
        XCTAssertFalse(Composer.canSend(overCap))
        XCTAssertEqual(Composer.validationMessage(overCap), "Message too long (max 64 KB).")
    }

    func testValidationMessageEmptyIsSilent() {
        // Empty draft = no error text, just a disabled button (mirrors Android).
        XCTAssertNil(Composer.validationMessage(""))
        XCTAssertNil(Composer.validationMessage("   "))
        XCTAssertNil(Composer.validationMessage("a valid message"))
    }

    func testOutcomeLines() {
        // command jobs deliver to stdin; agent/docker jobs run with stdin closed
        // and are honestly DROPPED with a reason (API.md §4 delivery semantics).
        XCTAssertEqual(Composer.outcome(state: "delivered", detail: nil),
                       "Delivered ✓ (to process)")
        XCTAssertEqual(Composer.outcome(state: "delivered", detail: "stdin"),
                       "Delivered ✓ (stdin)")
        XCTAssertEqual(Composer.outcome(state: "dropped", detail: "agent runs with stdin closed"),
                       "Dropped — agent runs with stdin closed")
        XCTAssertEqual(Composer.outcome(state: "dropped", detail: nil),
                       "Dropped — undeliverable")
        XCTAssertTrue(Composer.outcome(state: "queued", detail: nil).hasPrefix("Queued"))
    }
}
