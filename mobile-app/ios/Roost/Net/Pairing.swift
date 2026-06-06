import Foundation

/// Decodes `roost://pair?d=<base64url>` payloads (API.md §1). Pure functions so
/// the round-trip + padding + version-rejection cases are unit-testable.
enum Pairing {
    enum PairError: Error, Equatable {
        case malformedURL
        case notBase64
        case notJSON
        case unsupportedVersion(Int)   // v > 1 → "update the app"
    }

    /// Parse a full `roost://pair?d=…` URL.
    static func decode(url: URL) throws -> PairPayload {
        guard url.scheme == "roost", url.host == "pair",
              let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let d = comps.queryItems?.first(where: { $0.name == "d" })?.value
        else { throw PairError.malformedURL }
        return try decode(base64url: d)
    }

    /// Parse a pasted base64url payload (manual paste path).
    static func decode(base64url raw: String) throws -> PairPayload {
        // Accept a pasted full URI too, for paste-field convenience.
        if raw.hasPrefix("roost://"), let u = URL(string: raw) {
            return try decode(url: u)
        }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = base64urlDecode(trimmed) else { throw PairError.notBase64 }
        guard let payload = try? JSONDecoder().decode(PairPayload.self, from: data) else {
            throw PairError.notJSON
        }
        guard payload.v <= 1 else { throw PairError.unsupportedVersion(payload.v) }
        return payload
    }

    /// base64url → Data, restoring stripped `=` padding (`len % 4`) per §1 and
    /// mapping the URL-safe alphabet (`-_`) back to standard (`+/`).
    static func base64urlDecode(_ s: String) -> Data? {
        var b64 = s.replacingOccurrences(of: "-", with: "+")
                   .replacingOccurrences(of: "_", with: "/")
        let remainder = b64.count % 4
        if remainder > 0 {
            b64 += String(repeating: "=", count: 4 - remainder)
        }
        return Data(base64Encoded: b64)
    }
}
