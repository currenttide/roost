import XCTest
@testable import Roost

/// The health.status → glyph map, including the unknown-status fallback
/// (API.md §2/§6: unknown values render as text, never crash).
final class HealthGlyphTests: XCTestCase {

    func testKnownGlyphs() {
        let cases: [(String, String)] = [
            ("verified", "✓"), ("done", "✓"),
            ("unverified", "⚠"), ("failed", "✗"), ("cancelled", "−"),
            ("running", "▶"), ("verifying", "▶"), ("self-healing", "▶"),
            ("queued", "○"), ("waiting", "◔"),
            ("unplaceable", "⚠"), ("stuck?", "⚠"),
        ]
        for (raw, glyph) in cases {
            XCTAssertEqual(HealthStatus(raw: raw).glyph, glyph, raw)
        }
    }

    func testUnknownStatusRendersAsText() {
        let s = HealthStatus(raw: "teleporting")
        // Must not crash; the glyph IS the raw string so it renders as text.
        XCTAssertEqual(s, .unknown("teleporting"))
        XCTAssertEqual(s.glyph, "teleporting")
        XCTAssertFalse(s.isActive)
        XCTAssertFalse(s.isError)
    }

    func testSelfHealingRawMapping() {
        // The hyphenated raw value maps to the camelCase case.
        XCTAssertEqual(HealthStatus(raw: "self-healing"), .selfHealing)
        XCTAssertEqual(HealthStatus(raw: "stuck?"), .stuckQuestion)
    }

    /// Decoding a Health blob with an unknown status must succeed (additive).
    func testHealthDecodeUnknown() throws {
        let json = #"{"status": "warp-core-breach", "reason": "uh oh"}"#
        let h = try JSONDecoder().decode(Health.self, from: Data(json.utf8))
        XCTAssertEqual(h.status, .unknown("warp-core-breach"))
        XCTAssertEqual(h.status.glyph, "warp-core-breach")
        XCTAssertEqual(h.reason, "uh oh")
    }

    func testAnsiStrip() {
        let colored = "\u{1B}[31mred\u{1B}[0m plain \u{1B}[1;32mgreen\u{1B}[0m"
        XCTAssertEqual(Ansi.strip(colored), "red plain green")
        XCTAssertEqual(Ansi.strip("no escapes"), "no escapes")
    }
}
