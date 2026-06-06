import XCTest
@testable import Roost

/// Offline cache (DESIGN §5): round-trip, cap rule, sanitization, and the
/// cold-start seed contract (lines + cursor from one artifact).
final class CacheTests: XCTestCase {

    private var cache: OfflineCache!
    private var dir: URL!

    override func setUp() {
        dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("roost-cache-tests-\(UUID().uuidString)")
        cache = OfflineCache(directory: dir)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: dir)
    }

    private func line(_ seq: Int, _ kind: DisplayLine.Kind = .stdout) -> DisplayLine {
        DisplayLine(seq: seq, kind: kind, text: "line \(seq)")
    }

    func testLinesRoundTrip() {
        let lines = [line(1, .event), line(2), line(3, .stderr)]
        cache.saveLines("job1", lines)
        XCTAssertEqual(cache.loadLines("job1"), lines)
        XCTAssertEqual(cache.loadLines("other"), [])
    }

    func testCapKeepsTail() {
        let lines = (1...600).map { line($0) }
        let capped = OfflineCache.cap(lines)
        XCTAssertEqual(capped.count, OfflineCache.lineCap)
        XCTAssertEqual(capped.first?.seq, 101)
        XCTAssertEqual(capped.last?.seq, 600)
        // And the cap applies on save.
        cache.saveLines("big", lines)
        XCTAssertEqual(cache.loadLines("big").count, OfflineCache.lineCap)
    }

    func testDerivedRawRoundTripUsesContractDecode() throws {
        // The cached body must decode through the SAME model as a live fetch.
        let raw = Fixtures.data("derived.json")
        cache.saveDerivedRaw(raw)
        let loaded = try XCTUnwrap(cache.loadDerivedRaw())
        let d = try JSONDecoder().decode(Derived.self, from: loaded)
        XCTAssertFalse(d.runs.isEmpty)
    }

    func testJobIdSanitizedForPath() {
        cache.saveLines("../../etc/passwd", [line(1)])
        // Saved under a sanitized name inside the cache dir — and loadable back
        // through the same sanitization. Nothing escapes the directory.
        XCTAssertEqual(cache.loadLines("../../etc/passwd").count, 1)
        let files = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        XCTAssertTrue(files.allSatisfy { $0.hasPrefix("logs_") || $0 == "derived.json" })
        XCTAssertFalse(FileManager.default.fileExists(atPath: "/etc/roost-cache"))
    }

    func testClearWipes() {
        cache.saveLines("job1", [line(1)])
        cache.saveDerivedRaw(Data("{}".utf8))
        cache.clear()
        XCTAssertEqual(cache.loadLines("job1"), [])
        XCTAssertNil(cache.loadDerivedRaw())
    }

    func testDisplayLineFromRow() {
        // The pure render rule the cache persists: event rows → divider labels,
        // unparseable event rows are skipped, ANSI is stripped.
        let stdout = LogRow(seq: 1, stream: "stdout", data: "\u{1B}[31mred\u{1B}[0m", ts: 0)
        XCTAssertEqual(DisplayLine.from(stdout), DisplayLine(seq: 1, kind: .stdout, text: "red"))
        let event = LogRow(seq: 2, stream: "event", data: #"{"type": "started"}"#, ts: 0)
        XCTAssertEqual(DisplayLine.from(event)?.text, "started")
        XCTAssertNil(DisplayLine.from(LogRow(seq: 3, stream: "event", data: "junk", ts: 0)))
    }
}
