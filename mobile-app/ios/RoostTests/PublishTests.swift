import XCTest
@testable import Roost

/// Pure-logic tests for the publish flow's slug derivation/validation and the
/// gzip sniff (API.md §6). All Foundation-only, so they run on the Linux harness
/// AND in the iOS bundle. Mirrors the server's `normalize_slug`
/// (`roost/publish.py`): lowercase, spaces→`-`, validate `^[a-z0-9][a-z0-9-]{0,39}$`.
final class PublishTests: XCTestCase {

    // MARK: normalize — same transform the server applies before validating

    func testNormalizeLowercasesSpacesAndTrims() {
        XCTAssertEqual(PublishSlug.normalize("  My Site  "), "my-site")
        XCTAssertEqual(PublishSlug.normalize("Hello World"), "hello-world")
        XCTAssertEqual(PublishSlug.normalize("already-ok"), "already-ok")
    }

    // MARK: isValid — gates the Publish button

    func testValidSlugs() {
        for s in ["a", "site", "my-site", "0", "phone-oneshot",
                  "a1-b2-c3", String(repeating: "a", count: 40)] {
            XCTAssertTrue(PublishSlug.isValid(s), "expected valid: \(s)")
        }
        // A name with spaces is valid because normalize() fixes it first.
        XCTAssertTrue(PublishSlug.isValid("My Site"))
    }

    func testInvalidSlugs() {
        let bad = [
            "",                                   // empty
            "   ",                                // whitespace only
            "-leading",                           // can't start with hyphen
            "Has_Underscore",                     // underscore not allowed
            "dot.name",                           // dot not allowed
            "slash/name",                         // slash not allowed
            "über",                               // non-ascii
            String(repeating: "a", count: 41),    // one over the 40 window
        ]
        for s in bad {
            XCTAssertFalse(PublishSlug.isValid(s), "expected invalid: \(s)")
        }
    }

    func testValidityMatchesServerRegexAcrossLengths() {
        // The grammar is start char + up to 39 more = 1...40 total.
        XCTAssertTrue(PublishSlug.isValid(String(repeating: "x", count: 1)))
        XCTAssertTrue(PublishSlug.isValid(String(repeating: "x", count: 40)))
        XCTAssertFalse(PublishSlug.isValid(String(repeating: "x", count: 41)))
    }

    // MARK: suggestion — default slug proposed from the picked filename

    func testSuggestionStripsTarSuffixes() {
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "my-site.tar.gz"), "my-site")
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "my-site.tgz"), "my-site")
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "my-site.tar"), "my-site")
        // Case-insensitive suffix match.
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "Site.TAR.GZ"), "site")
    }

    func testSuggestionCoercesIllegalCharacters() {
        // Spaces → hyphens; underscores/dots → hyphens; runs collapse; ends trim.
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "My Site.tar.gz"), "my-site")
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "my_cool_site.tar.gz"),
                       "my-cool-site")
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "__weird__.tar.gz"), "weird")
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "a...b.tar.gz"), "a-b")
    }

    func testSuggestionTruncatesToWindow() {
        let long = String(repeating: "a", count: 60) + ".tar.gz"
        let s = PublishSlug.suggestion(fromFilename: long)
        XCTAssertEqual(s.count, 40)
        XCTAssertTrue(PublishSlug.isValid(s))
    }

    func testSuggestionEmptyWhenNothingSurvives() {
        // No alnum to keep → no proposal; the UI then asks for a name.
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "___.tar.gz"), "")
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: ".tar.gz"), "")
    }

    func testSuggestionsAreAlwaysValidOrEmpty() {
        for name in ["index.tar.gz", "My Portfolio Site.tgz", "v2.0-release.tar",
                     "weird ___ name.tar.gz", "résumé.tar.gz"] {
            let s = PublishSlug.suggestion(fromFilename: name)
            XCTAssertTrue(s.isEmpty || PublishSlug.isValid(s),
                          "suggestion for \(name) was \(s)")
        }
    }

    // MARK: gzip sniff — pre-reject an obviously-wrong pick before upload

    func testLooksLikeGzipMagic() {
        // Real gzip streams start with 1f 8b.
        XCTAssertTrue(BundleCheck.looksLikeGzip(Data([0x1f, 0x8b, 0x08, 0x00])))
    }

    func testRejectsNonGzip() {
        XCTAssertFalse(BundleCheck.looksLikeGzip(Data()))               // empty
        XCTAssertFalse(BundleCheck.looksLikeGzip(Data([0x1f])))         // 1 byte
        XCTAssertFalse(BundleCheck.looksLikeGzip(Data([0x50, 0x4b])))   // zip ("PK")
        XCTAssertFalse(BundleCheck.looksLikeGzip(
            Data("<html>".utf8)))                                       // html
    }

    func testGzipSniffHonorsSliceStartIndex() {
        // Data slices don't start at index 0; the check must use startIndex.
        let backing = Data([0x00, 0x00, 0x1f, 0x8b, 0x08])
        let slice = backing[backing.index(backing.startIndex, offsetBy: 2)...]
        XCTAssertTrue(BundleCheck.looksLikeGzip(slice))
    }
}
