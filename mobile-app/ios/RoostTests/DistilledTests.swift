import XCTest
@testable import Roost

/// R108: the iOS distilled live-stream transform mirrors the LANGUAGE-NEUTRAL
/// contract in `mobile-app/fixtures/distilled/SPEC.md`. The SHARED golden
/// fixtures (`mobile-app/fixtures/distilled/cases.json`) are the cross-platform
/// consistency guarantee: the CLI (`roost.cli.distill_log_line`, the reference
/// impl) and iOS MUST produce identical output for every committed case. This
/// suite loads those committed fixtures (NOT hand-rolled expectations) and
/// asserts `DistilledLine.from(case.raw) == case.distilled` for every case, so
/// the iOS transform can never silently drift from the contract.
final class DistilledTests: XCTestCase {

    private struct Case: Decodable {
        let note: String
        let source: String
        let raw: String
        let distilled: String?   // null in JSON == suppress (nil)
    }
    private struct CasesFile: Decodable {
        let version: Int
        let cases: [Case]
    }

    /// Load the shared fixtures. On Linux (`ROOST_FIXTURES` set) they live at
    /// `$ROOST_FIXTURES/distilled/cases.json`; under Xcode they are bundled by
    /// reference under `fixtures/distilled/cases.json` — `Fixtures.url` resolves
    /// both layouts.
    private func loadCases() throws -> [Case] {
        let url = Fixtures.url("distilled/cases.json")
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(CasesFile.self, from: data).cases
    }

    // MARK: - The cross-platform contract

    func testGoldenFixturesDistillToExpected() throws {
        let cases = try loadCases()
        XCTAssertGreaterThanOrEqual(cases.count, 66,
            "expected the full committed fixture set (R107 + R113 SPEC-branch/adversarial expansion)")
        for c in cases {
            XCTAssertEqual(DistilledLine.from(c.raw), c.distilled,
                "distilled mismatch for case: \(c.note)")
        }
    }

    func testFixturesAreWellFormedAndGrounded() throws {
        let cases = try loadCases()
        var sources = Set<String>()
        for c in cases {
            XCTAssertFalse(c.raw.isEmpty)
            XCTAssertTrue(["captured", "synthesized"].contains(c.source), c.source)
            sources.insert(c.source)
        }
        // Must be grounded in at least one real captured stream-json line.
        XCTAssertTrue(sources.contains("captured"))
    }

    // MARK: - Transform rules directly (mirrors tests/test_distilled.py)

    func testPlainTextPassesThroughVerbatim() {
        // A `command` job's stdout is not stream-json — never mangled.
        XCTAssertEqual(DistilledLine.from("total 48\ndrwxr-xr-x 3 me"),
                       "total 48\ndrwxr-xr-x 3 me")
        XCTAssertEqual(DistilledLine.from("plain line"), "plain line")
    }

    func testEmptyHandled() {
        XCTAssertEqual(DistilledLine.from(""), "")
    }

    func testMalformedJSONPassesThrough() {
        XCTAssertEqual(DistilledLine.from("{\"broken"), "{\"broken")
    }

    func testRoostEventEnvelopePassesThrough() {
        let raw = "{\"type\": \"started\", \"attempt\": 1, \"exit_code\": null}"
        XCTAssertEqual(DistilledLine.from(raw), raw)
    }

    func testSystemInitIsPhaseDivider() {
        XCTAssertEqual(DistilledLine.from("{\"type\": \"system\", \"subtype\": \"init\"}"),
                       "🔎 starting…")
    }

    func testSystemOtherSubtypeSuppressed() {
        XCTAssertNil(DistilledLine.from("{\"type\": \"system\", \"subtype\": \"thinking_tokens\"}"))
    }

    func testRateLimitEventSuppressed() {
        XCTAssertNil(DistilledLine.from("{\"type\": \"rate_limit_event\", \"rate_limit_info\": {}}"))
    }

    func testResultSuccessAndError() {
        XCTAssertEqual(DistilledLine.from("{\"type\": \"result\", \"subtype\": \"success\"}"), "✓ done")
        XCTAssertEqual(DistilledLine.from("{\"type\": \"result\", \"is_error\": true}"), "✗ failed")
        // is_error:false is falsy → success divider.
        XCTAssertEqual(DistilledLine.from("{\"type\": \"result\", \"is_error\": false}"), "✓ done")
    }

    func testAssistantTextShown() {
        let raw = "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"text\", \"text\": \"Hi there\"}]}}"
        XCTAssertEqual(DistilledLine.from(raw), "Hi there")
    }

    func testAssistantStringContent() {
        let raw = "{\"type\": \"assistant\", \"message\": {\"content\": \"direct string\"}}"
        XCTAssertEqual(DistilledLine.from(raw), "direct string")
    }

    func testAssistantThinkingSuppressed() {
        let raw = "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"thinking\", \"thinking\": \"deep\", \"signature\": \"Er0CCmMIabc\"}]}}"
        XCTAssertNil(DistilledLine.from(raw))   // reasoning AND base64 signature gone
    }

    func testToolUseWithHint() {
        let raw = "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"tool_use\", \"name\": \"Bash\", \"input\": {\"command\": \"ls -la\"}}]}}"
        XCTAssertEqual(DistilledLine.from(raw), "→ Bash: ls -la")
    }

    func testToolUseHintPriorityCommandOverDescription() {
        let raw = "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"tool_use\", \"name\": \"Bash\", \"input\": {\"command\": \"uptime\", \"description\": \"show uptime\"}}]}}"
        XCTAssertEqual(DistilledLine.from(raw), "→ Bash: uptime")
    }

    func testToolUseWithoutHintIsBareArrow() {
        let raw = "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"tool_use\", \"name\": \"TodoWrite\", \"input\": {\"todos\": []}}]}}"
        XCTAssertEqual(DistilledLine.from(raw), "→ TodoWrite")
    }

    func testToolUseHintCappedAndCollapsed() {
        let long = String(repeating: "x", count: 200)
        let raw = "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"tool_use\", \"name\": \"Bash\", \"input\": {\"command\": \"\(long)\"}}]}}"
        let out = DistilledLine.from(raw)
        XCTAssertEqual(out, "→ Bash: " + String(repeating: "x", count: 80) + "…")
    }

    func testToolResultStrTruncatedCollapsed() {
        let raw = "{\"type\": \"user\", \"message\": {\"content\": [{\"type\": \"tool_result\", \"is_error\": false, \"content\": \"file contents\\nmore\"}]}}"
        XCTAssertEqual(DistilledLine.from(raw), "  ⎿ file contents more")
    }

    func testToolResultListContent() {
        let raw = "{\"type\": \"user\", \"message\": {\"content\": [{\"type\": \"tool_result\", \"content\": [{\"type\": \"text\", \"text\": \"the output\"}]}]}}"
        XCTAssertEqual(DistilledLine.from(raw), "  ⎿ the output")
    }

    func testToolResultErrorMarked() {
        let raw = "{\"type\": \"user\", \"message\": {\"content\": [{\"type\": \"tool_result\", \"is_error\": true, \"content\": \"denied\"}]}}"
        XCTAssertEqual(DistilledLine.from(raw), "  ⎿ ✗ denied")
    }

    func testToolResultEmptyPlaceholder() {
        let raw = "{\"type\": \"user\", \"message\": {\"content\": [{\"type\": \"tool_result\", \"content\": \"\"}]}}"
        XCTAssertEqual(DistilledLine.from(raw), "  ⎿ (result)")
    }

    func testAssistantMultipleBlocksJoined() {
        let raw = "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"text\", \"text\": \"Let me check\"}, {\"type\": \"tool_use\", \"name\": \"Read\", \"input\": {\"file_path\": \"/etc/hostname\"}}]}}"
        XCTAssertEqual(DistilledLine.from(raw), "Let me check\n→ Read: /etc/hostname")
    }

    func testAssistantEmptyContentSuppressed() {
        XCTAssertNil(DistilledLine.from("{\"type\": \"assistant\", \"message\": {\"content\": []}}"))
    }

    func testNeverThrowsOnOddShapes() {
        // Pure + total: odd shapes must not crash; they distil to nil or verbatim.
        for bad in ["[]", "123", "null", "true", "{\"type\": 5}",
                    "{\"type\": \"assistant\", \"message\": null}",
                    "{\"type\": \"assistant\", \"message\": {\"content\": [null, 7]}}"] {
            _ = DistilledLine.from(bad)
        }
    }

    // R113: is_error uses JSON truthiness (a truthy number/string means error),
    // matching the CLI. Pinned directly here as well as via the shared fixtures.
    func testIsErrorUsesJsonTruthiness() {
        XCTAssertEqual(DistilledLine.from("{\"type\":\"result\",\"is_error\":1}"), "✗ failed")
        XCTAssertEqual(DistilledLine.from("{\"type\":\"result\",\"is_error\":\"yes\"}"), "✗ failed")
        XCTAssertEqual(DistilledLine.from("{\"type\":\"result\",\"is_error\":0}"), "✓ done")
        XCTAssertEqual(DistilledLine.from("{\"type\":\"result\",\"is_error\":\"\"}"), "✓ done")
        func tr(_ e: String) -> String {
            "{\"type\":\"user\",\"message\":{\"content\":[{\"type\":\"tool_result\",\"is_error\":\(e),\"content\":\"boom\"}]}}"
        }
        XCTAssertEqual(DistilledLine.from(tr("1")), "  ⎿ ✗ boom")
        XCTAssertEqual(DistilledLine.from(tr("\"yes\"")), "  ⎿ ✗ boom")
        XCTAssertEqual(DistilledLine.from(tr("0")), "  ⎿ boom")
    }

    // R113: non-string hint values are skipped (bare arrow) and non-string text is
    // suppressed — so iOS no longer leaks coercions like "<null>"/"true"/"[\"a\", \"b\"]".
    func testNonStringHintAndTextDoNotLeakCoercion() {
        func tu(_ v: String) -> String {
            "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"tool_use\",\"name\":\"X\",\"input\":{\"command\":\(v)}}]}}"
        }
        XCTAssertEqual(DistilledLine.from(tu("42")), "→ X")
        XCTAssertEqual(DistilledLine.from(tu("true")), "→ X")
        XCTAssertEqual(DistilledLine.from(tu("[\"a\",\"b\"]")), "→ X")
        XCTAssertEqual(DistilledLine.from(
            "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"tool_use\",\"name\":\"X\",\"input\":{\"command\":0,\"file_path\":\"/p\"}}]}}"),
            "→ X: /p")
        XCTAssertNil(DistilledLine.from("{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":123}]}}"))
        XCTAssertNil(DistilledLine.from("{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":null}]}}"))
    }

    // MARK: - The render seam (DisplayLine default = distilled, R108)

    func testDisplayLineDefaultsToDistilled() {
        // A noisy thinking row (default mode) is suppressed → nil.
        let thinking = LogRow(seq: 1, stream: "stdout",
            data: "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"thinking\", \"thinking\": \"x\", \"signature\": \"abc\"}]}}",
            ts: 0)
        XCTAssertNil(DisplayLine.from(thinking))
        // An assistant text row distils to its text.
        let text = LogRow(seq: 2, stream: "stdout",
            data: "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"text\", \"text\": \"Working on it\"}]}}",
            ts: 0)
        XCTAssertEqual(DisplayLine.from(text)?.text, "Working on it")
    }

    func testDisplayLineRawShowsFirehose() {
        // raw:true reproduces the exact wire line (ANSI-stripped only) — the
        // noisy thinking row is NOT suppressed.
        let thinking = LogRow(seq: 1, stream: "stdout",
            data: "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"thinking\", \"thinking\": \"x\"}]}}",
            ts: 0)
        let line = DisplayLine.from(thinking, raw: true)
        XCTAssertNotNil(line)
        XCTAssertEqual(line?.text, thinking.data)
    }

    func testDisplayLinePlainStdoutSameBothModes() {
        // A `command` job's plain stdout is passthrough in BOTH modes.
        let row = LogRow(seq: 1, stream: "stdout", data: "hello world", ts: 0)
        XCTAssertEqual(DisplayLine.from(row)?.text, "hello world")
        XCTAssertEqual(DisplayLine.from(row, raw: true)?.text, "hello world")
    }
}
