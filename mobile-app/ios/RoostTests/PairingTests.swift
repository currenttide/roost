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

/// R83: the pairing-flow state machine (idle → contacting(host) → failed /
/// cancelled / paired) and the short fail-fast probe timeout. Pure logic, so it
/// runs in the Linux harness without URLSession.
final class PairingStateTests: XCTestCase {

    func testIdleHasNoCaptionOrErrorAndIsNotBusy() {
        let s = PairingState.idle
        XCTAssertFalse(s.isBusy)
        XCTAssertNil(s.caption)
        XCTAssertNil(s.errorMessage)
    }

    func testBeginningEntersContactingWithHostCaption() {
        let s = PairingState.beginning(url: "http://192.168.1.250:8787")
        XCTAssertEqual(s, .contacting(host: "192.168.1.250:8787"))
        XCTAssertTrue(s.isBusy)
        // The caption finally tells the user WHAT the spinner is doing + WHICH host.
        XCTAssertEqual(s.caption, "Contacting 192.168.1.250:8787…")
        XCTAssertNil(s.errorMessage)
    }

    func testDisplayHostStripsSchemeAndPathButKeepsPort() {
        XCTAssertEqual(PairingState.displayHost("http://192.168.1.193:8787"),
                       "192.168.1.193:8787")
        XCTAssertEqual(PairingState.displayHost("https://cp.example.com/"),
                       "cp.example.com")          // no port → host only
        XCTAssertEqual(PairingState.displayHost("https://roost.local:443/healthz"),
                       "roost.local:443")
    }

    func testDisplayHostNeverLeaksTheToken() {
        // The token lives in the payload, never the URL — but be defensive: even a
        // URL carrying query junk must not surface anything but host[:port].
        let host = PairingState.displayHost("http://10.0.0.2:8787/x?token=secret")
        XCTAssertEqual(host, "10.0.0.2:8787")
        XCTAssertFalse(host.contains("secret"))
    }

    func testDisplayHostFallsBackToRawForUnparseableURL() {
        // No host component → echo the raw string rather than crash or show empty.
        XCTAssertEqual(PairingState.displayHost("not a url"), "not a url")
    }

    func testFailedExposesMessageAndClearsBusy() {
        let s = PairingState.failed(message: "Can't reach the host.")
        XCTAssertFalse(s.isBusy)
        XCTAssertNil(s.caption)
        XCTAssertEqual(s.errorMessage, "Can't reach the host.")
    }

    func testCancellingFromContactingGoesToCancelledWithNoError() {
        let contacting = PairingState.beginning(url: "http://10.0.0.9:8787")
        let cancelled = contacting.cancelling()
        XCTAssertEqual(cancelled, .cancelled)
        XCTAssertFalse(cancelled.isBusy)        // spinner gone
        XCTAssertNil(cancelled.errorMessage)    // a deliberate cancel is not a failure
        XCTAssertNil(cancelled.caption)
    }

    func testCancellingIsANoOpWhenNotContacting() {
        // Cancel only acts while a probe is in flight; otherwise leave state alone.
        XCTAssertEqual(PairingState.idle.cancelling(), .idle)
        XCTAssertEqual(PairingState.paired.cancelling(), .paired)
        XCTAssertEqual(PairingState.failed(message: "x").cancelling(),
                       .failed(message: "x"))
    }

    func testPairedIsTerminalAndQuiet() {
        let s = PairingState.paired
        XCTAssertFalse(s.isBusy)
        XCTAssertNil(s.caption)
        XCTAssertNil(s.errorMessage)
    }

    /// The probe deadline is short (justified ~5 s) so a dead host fails fast,
    /// and it is *strictly* shorter than the steady-state app/SSE request timeout
    /// (30 s in AppState.makeClient) — proving the short timeout is scoped to the
    /// throwaway probe and never shortens long-lived traffic.
    func testProbeTimeoutIsShortAndScopedToPairing() {
        XCTAssertEqual(ApiClient.pairingProbeTimeout, 5)
        XCTAssertLessThanOrEqual(ApiClient.pairingProbeTimeout, 5)
        XCTAssertLessThan(ApiClient.pairingProbeTimeout, 30)   // < makeClient's request timeout
    }
}
