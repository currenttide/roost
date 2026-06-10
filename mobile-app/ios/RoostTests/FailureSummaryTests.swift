import XCTest
@testable import Roost

/// R122: failed-agent results render DISTILLED, not as raw stream-json walls.
///
/// A worker can report a failure whose `result.output`/`error` is one or more
/// raw Anthropic stream-json lines (the UAT "failed-agent rows render raw JSON"
/// finding). `DistilledLine.failureSummary` / `.failureLine` REUSE the SPEC.md
/// transform + truncation rules (no new transform branch — the golden fixtures
/// in `mobile-app/fixtures/distilled/cases.json` still pin the underlying
/// transform): stream-json lines distil, noise suppresses, and verbatim
/// passthrough lines are whitespace-collapsed + capped at 200 per SPEC rule 5.
///
/// The `// PARITY:` cases below are mirrored byte-for-byte in the Android
/// harness (`FailureSummaryTest.kt`) so the two phones can't drift on the
/// failure rendering either.
final class FailureSummaryTests: XCTestCase {

    // MARK: - PARITY cases (mirrored in Android FailureSummaryTest.kt)

    func testRawResultEnvelopeWallDistilsToPhaseDivider() {
        // PARITY P1: the most common wall — the final `result` envelope.
        let wall = "{\"type\":\"result\",\"subtype\":\"error_during_execution\",\"is_error\":true,"
            + "\"duration_ms\":4521,\"num_turns\":3,"
            + "\"usage\":{\"input_tokens\":9114,\"output_tokens\":201}}"
        XCTAssertEqual(DistilledLine.failureSummary(wall), "✗ failed")
        XCTAssertEqual(DistilledLine.failureLine(wall), "✗ failed")
    }

    func testAssistantEnvelopeWallDistilsToItsText() {
        // PARITY P2: an `assistant` envelope reported as the failure output.
        let wall = "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\","
            + "\"text\":\"I could not reach the host — connection refused.\"}]}}"
        XCTAssertEqual(DistilledLine.failureSummary(wall),
                       "I could not reach the host — connection refused.")
    }

    func testPlainErrorTextPassesThroughCollapsed() {
        // PARITY P3: an honest plain-text error is kept (rule 1), with each
        // line whitespace-collapsed per rule 5.
        let text = "verification failed after 2 self-heal attempt(s): evidence  says\nno artifact"
        XCTAssertEqual(DistilledLine.failureSummary(text),
                       "verification failed after 2 self-heal attempt(s): evidence says\nno artifact")
    }

    func testNonJsonWallIsCappedAt200() {
        // PARITY P4: a JSON-ish-but-unparseable wall (e.g. a Python dict repr)
        // passes through rule 1 but is capped at RESULT_MAX with a single `…`.
        let wall = "{'type': 'result', 'is_error': True, " + String(repeating: "x", count: 200)
        let out = DistilledLine.failureSummary(wall)
        XCTAssertEqual(out?.count, 201)   // 200 chars + U+2026
        XCTAssertEqual(out, String(wall.prefix(200)) + "…")
    }

    func testMixedLinesDistilSuppressAndPassThrough() {
        // PARITY P5: stream-json distils, noise suppresses, plain text stays.
        let text = "{\"type\":\"system\",\"subtype\":\"init\"}\n"
            + "{\"type\":\"rate_limit_event\"}\n"
            + "exit_code=1"
        XCTAssertEqual(DistilledLine.failureSummary(text), "🔎 starting…\nexit_code=1")
    }

    func testAllNoiseSuppressesToNil() {
        // PARITY P6: nothing survives → nil (caller falls back to its state line).
        XCTAssertNil(DistilledLine.failureSummary("{\"type\":\"rate_limit_event\"}"))
    }

    func testNilAndBlankAreNil() {
        // PARITY P7.
        XCTAssertNil(DistilledLine.failureSummary(nil))
        XCTAssertNil(DistilledLine.failureSummary(""))
        XCTAssertNil(DistilledLine.failureSummary("   \n  "))
    }

    func testFailureLineTakesFirstSurvivingLine() {
        // PARITY P8: a multi-block assistant envelope distils to several lines;
        // the dashboard row shows the first.
        let wall = "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\","
            + "\"text\":\"Let me check\"},{\"type\":\"tool_use\",\"name\":\"Read\","
            + "\"input\":{\"file_path\":\"/etc/hostname\"}}]}}"
        XCTAssertEqual(DistilledLine.failureSummary(wall), "Let me check\n→ Read: /etc/hostname")
        XCTAssertEqual(DistilledLine.failureLine(wall), "Let me check")
    }

    // MARK: - Wiring: dashboard run row (Run.subtitle)

    /// Decode a minimal §2 run row with the given state + result string.
    private func run(state: String, result: String) throws -> Run {
        let resultJSON = String(data: try JSONSerialization.data(
            withJSONObject: [result]), encoding: .utf8)!
        // [ "..." ] → "..." (safe single-element array trick for escaping)
        let escaped = String(resultJSON.dropFirst().dropLast())
        let json = "{\"run_id\": \"j1\", \"state\": \"\(state)\", \"result\": \(escaped)}"
        return try JSONDecoder().decode(Run.self, from: Data(json.utf8))
    }

    func testFailedRunSubtitleDistilsResultWall() throws {
        let wall = "{\"type\":\"result\",\"is_error\":true}"
        let failed = try run(state: "failed", result: wall)
        XCTAssertEqual(failed.subtitle, "✗ failed")
    }

    func testNonFailedRunSubtitleUnchanged() throws {
        // A succeeded row keeps today's verbatim behavior — R122 touches only
        // failure rendering.
        let wall = "{\"type\":\"result\",\"is_error\":false}"
        let ok = try run(state: "succeeded", result: wall)
        XCTAssertEqual(ok.subtitle, wall)
    }

    func testFailedRunSubtitlePrefersNarrationAndDistilsIt() throws {
        let json = """
        {"run_id": "j2", "state": "failed",
         "narration": "{\\"type\\":\\"assistant\\",\\"message\\":{\\"content\\":[{\\"type\\":\\"text\\",\\"text\\":\\"boom\\"}]}}",
         "result": "exit_code=1"}
        """
        let r = try JSONDecoder().decode(Run.self, from: Data(json.utf8))
        XCTAssertEqual(r.subtitle, "boom")
    }

    // MARK: - Wiring: session result card (SSEDonePayload.displaySummary)

    func testDoneDisplaySummaryDistilsFailedOutput() {
        let d = SSEDonePayload(
            state: "failed", exitCode: 1, error: "exit_code=1",
            result: JobResult(output: "{\"type\":\"result\",\"is_error\":true}",
                              verified: false, evidence: nil),
            tokensUsed: nil)
        XCTAssertEqual(d.displaySummary, "✗ failed")
    }

    func testDoneDisplaySummaryFallsThroughToErrorWhenOutputSuppresses() {
        // Output distils to nothing → the (distilled) error is shown instead.
        let d = SSEDonePayload(
            state: "failed", exitCode: 1,
            error: "wallclock budget exceeded (300s); killing",
            result: JobResult(output: "{\"type\":\"rate_limit_event\"}",
                              verified: nil, evidence: nil),
            tokensUsed: nil)
        XCTAssertEqual(d.displaySummary, "wallclock budget exceeded (300s); killing")
    }

    func testDoneDisplaySummaryVerbatimWhenNotFailed() {
        let wall = "{\"type\":\"result\",\"is_error\":false}"
        let d = SSEDonePayload(
            state: "succeeded", exitCode: 0, error: nil,
            result: JobResult(output: wall, verified: true, evidence: "ok"),
            tokensUsed: 12)
        XCTAssertEqual(d.displaySummary, wall)
    }
}
