import Foundation

// MARK: - SSE event payloads (API.md §5)

struct SSEStatePayload: Codable, Equatable {
    let state: String
}

struct SSEDonePayload: Codable, Equatable {
    let state: String?
    let exitCode: Int?
    let error: String?
    let result: JobResult?
    let tokensUsed: Int?

    enum CodingKeys: String, CodingKey {
        case state, error, result
        case exitCode = "exit_code"
        case tokensUsed = "tokens_used"
    }
}

struct SSEErrorPayload: Codable, Equatable {
    let error: String
}

/// A decoded SSE event. `log` carries a `LogRow` (same shape as `/logs`).
enum SSEEvent: Equatable {
    case state(SSEStatePayload)
    case log(LogRow)
    case done(SSEDonePayload)
    case error(SSEErrorPayload)
    case unknown(event: String)   // unparseable / unrecognized → ignored by UI
}

// MARK: - Frame parser (API.md §5)

/// Hand-rolled SSE frame parser. Rules from the contract:
///   · frames are separated by a blank line,
///   · a frame is a set of lines; we read `event:` and `data:` prefixes,
///   · anything else (comments starting `:`, `retry:` hints) is ignored,
///   · `data:` is a single line of JSON per the Roost server.
///
/// This is deliberately a pure value type: feed it bytes/strings, get events.
/// The transport (URLSession.bytes) lives in `LogStream`; keeping the parser
/// separate is what makes it unit-testable against `stream_succeeded.sse.txt`.
struct SSEParser {
    private var buffer = ""
    private let decoder = JSONDecoder()

    /// Append a chunk of decoded text and return any complete events it yields.
    mutating func consume(_ chunk: String) -> [SSEEvent] {
        buffer += chunk
        var events: [SSEEvent] = []
        // Frames end at a blank line. A blank line is "\n\n" (LF) — the server
        // emits LF; we also tolerate CRLF by normalizing first.
        buffer = buffer.replacingOccurrences(of: "\r\n", with: "\n")
        while let range = buffer.range(of: "\n\n") {
            let frame = String(buffer[buffer.startIndex..<range.lowerBound])
            buffer.removeSubrange(buffer.startIndex..<range.upperBound)
            if let ev = Self.parseFrame(frame, decoder: decoder) {
                events.append(ev)
            }
        }
        return events
    }

    /// Flush a trailing frame that wasn't blank-line terminated (e.g. stream
    /// closed right after `done`). Call once when the byte stream ends.
    mutating func flush() -> [SSEEvent] {
        let frame = buffer.trimmingCharacters(in: .whitespacesAndNewlines)
        buffer = ""
        guard !frame.isEmpty, let ev = Self.parseFrame(frame, decoder: JSONDecoder()) else {
            return []
        }
        return [ev]
    }

    /// Parse a single frame (no trailing blank line) into an event.
    static func parseFrame(_ frame: String, decoder: JSONDecoder) -> SSEEvent? {
        var eventName: String?
        var dataLines: [String] = []
        for rawLine in frame.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(rawLine)
            if line.hasPrefix(":") { continue }            // comment
            if let v = value(of: "event:", in: line) {
                eventName = v
            } else if let v = value(of: "data:", in: line) {
                dataLines.append(v)
            }
            // any other field (id:, retry:, …) is ignored per contract
        }
        guard let name = eventName else { return nil }
        // Per the SSE spec multiple data lines join with "\n"; Roost sends one.
        let data = dataLines.joined(separator: "\n")
        let bytes = Data(data.utf8)
        switch name {
        case "state":
            if let p = try? decoder.decode(SSEStatePayload.self, from: bytes) {
                return .state(p)
            }
        case "log":
            if let p = try? decoder.decode(LogRow.self, from: bytes) {
                return .log(p)
            }
        case "done":
            if let p = try? decoder.decode(SSEDonePayload.self, from: bytes) {
                return .done(p)
            }
        case "error":
            if let p = try? decoder.decode(SSEErrorPayload.self, from: bytes) {
                return .error(p)
            }
        default:
            return .unknown(event: name)
        }
        // event name recognized but data failed to decode → skip (don't crash)
        return nil
    }

    /// Strip a `field:` prefix and a single optional leading space (SSE spec:
    /// exactly one space after the colon is consumed if present).
    private static func value(of prefix: String, in line: String) -> String? {
        guard line.hasPrefix(prefix) else { return nil }
        var rest = String(line.dropFirst(prefix.count))
        if rest.hasPrefix(" ") { rest.removeFirst() }
        return rest
    }
}
