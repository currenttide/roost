import XCTest
@testable import Roost

/// Pairing-URI decode: round-trip incl. base64url padding restoration and the
/// v>1 rejection (API.md §1/§6).
final class PairingTests: XCTestCase {

    /// base64url-encode without padding, like `roost pair` does.
    private func encode(_ payload: PairPayload) -> String {
        let json = try! JSONEncoder().encode(payload)
        var b64 = json.base64EncodedString()
        b64 = b64.replacingOccurrences(of: "+", with: "-")
                 .replacingOccurrences(of: "/", with: "_")
                 .replacingOccurrences(of: "=", with: "")   // strip padding
        return b64
    }

    func testRoundTripWithPaddingRestoration() throws {
        let original = PairPayload(v: 1,
                                   url: "http://192.168.1.193:8787",
                                   token: "rst-mob-PpHr7xSLN8SyEB1_bA7OefybWKP",
                                   name: "yang-iphone")
        let d = encode(original)
        // The encoded string has had padding stripped — exercise restoration.
        XCTAssertFalse(d.hasSuffix("="))
        let uri = "roost://pair?d=\(d)"
        let decoded = try Pairing.decode(url: URL(string: uri)!)
        XCTAssertEqual(decoded, original)
    }

    func testPaddingRestorationAcrossLengths() throws {
        // Names of varying length flip the base64 length % 4 through 0..3,
        // so each padding case (0, 1, 2 '=') is exercised.
        for suffix in ["", "a", "ab", "abc", "abcd"] {
            let p = PairPayload(v: 1, url: "http://10.0.0.2:8787",
                                token: "rst-mob-x", name: "n\(suffix)")
            let decoded = try Pairing.decode(base64url: encode(p))
            XCTAssertEqual(decoded, p, "suffix=\(suffix)")
        }
    }

    func testNameOptional() throws {
        let p = PairPayload(v: 1, url: "http://10.0.0.5:8787", token: "rst-mob-y", name: nil)
        let decoded = try Pairing.decode(base64url: encode(p))
        XCTAssertNil(decoded.name)
        XCTAssertEqual(decoded.url, "http://10.0.0.5:8787")
    }

    func testRejectVersionGreaterThanOne() {
        let p = PairPayload(v: 2, url: "http://10.0.0.9:8787", token: "rst-mob-z", name: nil)
        XCTAssertThrowsError(try Pairing.decode(base64url: encode(p))) { err in
            XCTAssertEqual(err as? Pairing.PairError, .unsupportedVersion(2))
        }
    }

    func testRejectGarbage() {
        XCTAssertThrowsError(try Pairing.decode(base64url: "!!!not base64!!!"))
    }

    func testPastedFullURIWorks() throws {
        let p = PairPayload(v: 1, url: "http://192.168.0.10:8787",
                            token: "rst-mob-q", name: "phone")
        let uri = "roost://pair?d=\(encode(p))"
        // The paste field accepts a full URI too.
        let decoded = try Pairing.decode(base64url: uri)
        XCTAssertEqual(decoded, p)
    }
}
