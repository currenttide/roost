import XCTest
@testable import RoostKit

final class SSEParserTests: XCTestCase {

    func testSingleEvent() {
        var parser = SSEParser()
        let events = parser.feed("event: state\ndata: {\"state\": \"running\"}\n\n")
        XCTAssertEqual(events, [SSEEvent(event: "state", data: "{\"state\": \"running\"}")])
    }

    func testChunkedAcrossArbitraryBoundaries() {
        // The wire splits mid-line and mid-event; output must be identical.
        var parser = SSEParser()
        var events: [SSEEvent] = []
        for chunk in ["eve", "nt: log\nda", "ta: {\"seq\": 1}\n", "\nevent: done\n",
                      "data: {}\n\n"] {
            events += parser.feed(chunk)
        }
        XCTAssertEqual(events, [
            SSEEvent(event: "log", data: "{\"seq\": 1}"),
            SSEEvent(event: "done", data: "{}"),
        ])
    }

    func testCRLFAndComments() {
        var parser = SSEParser()
        let events = parser.feed(": keep-alive\r\nevent: state\r\ndata: x\r\n\r\n")
        XCTAssertEqual(events, [SSEEvent(event: "state", data: "x")])
    }

    func testMultiLineData() {
        var parser = SSEParser()
        let events = parser.feed("data: line1\ndata: line2\n\n")
        XCTAssertEqual(events, [SSEEvent(event: nil, data: "line1\nline2")])
    }

    func testNoSpaceAfterColon() {
        var parser = SSEParser()
        let events = parser.feed("event:state\ndata:x\n\n")
        XCTAssertEqual(events, [SSEEvent(event: "state", data: "x")])
    }

    // MARK: typed job events (shapes from server.py stream_job)

    func testParseStateLogDone() throws {
        let state = try JobStreamEvent.parse(
            SSEEvent(event: "state", data: #"{"state": "running"}"#))
        XCTAssertEqual(state, .state("running"))

        let log = try JobStreamEvent.parse(SSEEvent(
            event: "log",
            data: #"{"seq": 7, "stream": "stdout", "data": "hi", "ts": 1.5}"#))
        guard case .log(let line)? = log else { return XCTFail("expected log") }
        XCTAssertEqual(line.seq, 7)
        XCTAssertEqual(line.text, "hi")

        let done = try JobStreamEvent.parse(SSEEvent(
            event: "done",
            data: #"{"state": "succeeded", "exit_code": 0, "error": null, "result": {"verified": true, "evidence": "ok", "output": "out"}, "tokens_used": 42}"#))
        guard case .done(let d)? = done else { return XCTFail("expected done") }
        XCTAssertEqual(d.state, "succeeded")
        XCTAssertEqual(d.verified, true)
        XCTAssertEqual(d.evidence, "ok")
        XCTAssertEqual(d.output, "out")
        XCTAssertEqual(d.tokensUsed, 42)
    }

    func testParseErrorEventThrows() {
        XCTAssertThrowsError(try JobStreamEvent.parse(
            SSEEvent(event: "error", data: #"{"error": "job not found"}"#)))
    }

    func testUnknownEventIsSkipped() throws {
        XCTAssertNil(try JobStreamEvent.parse(SSEEvent(event: "telemetry", data: "{}")))
    }

    func testDoneWithStringResult() throws {
        // command jobs put plain text in result
        let done = try JobStreamEvent.parse(SSEEvent(
            event: "done",
            data: #"{"state": "succeeded", "exit_code": 0, "result": "plain output", "tokens_used": 0}"#))
        guard case .done(let d)? = done else { return XCTFail("expected done") }
        XCTAssertNil(d.verified)
        XCTAssertEqual(d.output, "plain output")
    }
}
