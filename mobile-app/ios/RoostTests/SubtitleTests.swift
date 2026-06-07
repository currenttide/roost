import XCTest
@testable import Roost

/// R85: the subtitle kind segment reflects the job's ACTUAL kind. Android wrongly
/// hardcoded "claude"; iOS omitted the kind entirely — both now read the server's
/// truthful `kind` (API.md §2), dropping the segment only when it's unknown.
final class SubtitleTests: XCTestCase {

    func testKnownKindsShownVerbatim() {
        XCTAssertEqual(Subtitle.kindSegment("command"), "command")
        XCTAssertEqual(Subtitle.kindSegment("claude"), "claude")
        XCTAssertEqual(Subtitle.kindSegment("docker"), "docker")
        // A future server kind is shown verbatim, not crashed or guessed.
        XCTAssertEqual(Subtitle.kindSegment("codex"), "codex")
    }

    func testUnknownKindOmitsSegment() {
        // Older CP omits `kind` (nil) → drop the segment rather than guess "claude".
        XCTAssertNil(Subtitle.kindSegment(nil))
        XCTAssertNil(Subtitle.kindSegment(""))
        XCTAssertNil(Subtitle.kindSegment("   "))
    }
}
