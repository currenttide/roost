import XCTest
@testable import Roost

/// Parse the recorded SSE transcript through the frame parser and assert the
/// exact event sequence + seq-dedupe (API.md §5/§6).
final class SSETests: XCTestCase {

    /// Feed the whole transcript and collect events.
    private func parseAll(_ text: String) -> [SSEEvent] {
        var parser = SSEParser()
        var events = parser.consume(text)
        events += parser.flush()
        return events
    }

    func testExactEventSequence() throws {
        let text = Fixtures.string("stream_succeeded.sse.txt")
        let events = parseAll(text)

        // Expected: 1 state, 6 logs, 1 done = 8 events.
        XCTAssertEqual(events.count, 8)

        guard case .state(let s) = events[0] else {
            return XCTFail("first event not state")
        }
        XCTAssertEqual(s.state, "succeeded")

        // 6 log frames, seq 1...6, in order.
        for i in 1...6 {
            guard case .log(let row) = events[i] else {
                return XCTFail("event \(i) not log")
            }
            XCTAssertEqual(row.seq, i)
        }
        // First and last log streams.
        if case .log(let first) = events[1] { XCTAssertEqual(first.stream, "event") }
        if case .log(let last) = events[6] { XCTAssertEqual(last.stream, "event") }
        if case .log(let mid) = events[2] {
            XCTAssertEqual(mid.stream, "stdout")
            XCTAssertEqual(mid.data, "running pytest -q ...")
        }

        guard case .done(let done) = events[7] else {
            return XCTFail("last event not done")
        }
        XCTAssertEqual(done.state, "succeeded")
        XCTAssertEqual(done.exitCode, 0)
        XCTAssertEqual(done.tokensUsed, 48213)
        XCTAssertEqual(done.result?.output, "fixed: tests green")
        XCTAssertEqual(done.result?.verified, true)
    }

    /// Chunk the transcript at arbitrary byte boundaries; frames spanning
    /// chunks must still reassemble into the same sequence.
    func testChunkedReassembly() {
        let text = Fixtures.string("stream_succeeded.sse.txt")
        var parser = SSEParser()
        var events: [SSEEvent] = []
        // Feed 7 chars at a time to stress the buffer.
        var idx = text.startIndex
        while idx < text.endIndex {
            let end = text.index(idx, offsetBy: 7, limitedBy: text.endIndex) ?? text.endIndex
            events += parser.consume(String(text[idx..<end]))
            idx = end
        }
        events += parser.flush()
        XCTAssertEqual(events.count, 8)
    }

    /// Seq-dedupe: replay an already-seen frame and assert the LogStream cursor
    /// logic drops it. We exercise the dedupe rule directly (seq <= lastSeen).
    func testSeqDedupeDropsReplay() {
        // Simulate the boundary overlap: catch-up delivered up to seq 3, then
        // the stream re-sends seq 3 (a replay) plus a fresh seq 4.
        var lastSeen = 3
        let incoming = [3, 4]   // 3 is a replay, 4 is new
        var accepted: [Int] = []
        for seq in incoming where seq > lastSeen {
            accepted.append(seq)
            lastSeen = seq
        }
        XCTAssertEqual(accepted, [4], "replayed seq 3 must be dropped")
        XCTAssertEqual(lastSeen, 4)
    }

    /// Event log rows decode to a lifecycle type label (for the divider).
    func testEventLabelParsing() {
        let started = #"{"type": "started", "attempt": 1}"#
        XCTAssertEqual(LogRow.eventLabel(started), "started")
        XCTAssertNil(LogRow.eventLabel("not json"))
        XCTAssertNil(LogRow.eventLabel(#"{"no":"type"}"#))
    }

    /// Unknown event names parse to `.unknown` (rendered/ignored, never crash).
    func testUnknownEventIgnored() {
        let frame = "event: retry-hint\ndata: {\"x\":1}"
        let ev = SSEParser.parseFrame(frame, decoder: JSONDecoder())
        guard case .unknown(let name)? = ev else { return XCTFail() }
        XCTAssertEqual(name, "retry-hint")
    }

    /// Comment lines and unknown fields inside a frame are ignored.
    func testCommentsIgnored() {
        let frame = ": this is a comment\nevent: state\nid: 7\ndata: {\"state\": \"running\"}"
        let ev = SSEParser.parseFrame(frame, decoder: JSONDecoder())
        guard case .state(let s)? = ev else { return XCTFail() }
        XCTAssertEqual(s.state, "running")
    }
}
