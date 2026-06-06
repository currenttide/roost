import Foundation

// Server-Sent Events, hand-rolled — the format is trivial and a dependency
// would violate the zero-deps rule (DESIGN.md §3). The parser is incremental:
// feed it raw chunks as they arrive; it emits complete events.

public struct SSEEvent: Equatable, Sendable {
    public let event: String?   // value of the `event:` field, if any
    public let data: String     // joined `data:` lines

    public init(event: String?, data: String) {
        self.event = event
        self.data = data
    }
}

public struct SSEParser: Sendable {
    // Byte buffer, not String: a CRLF that straddles a chunk boundary (or at
    // all — Swift treats "\r\n" as ONE grapheme) must still split correctly,
    // and a multi-byte UTF-8 sequence may arrive split across chunks.
    private var buffer: [UInt8] = []
    private var currentEvent: String?
    private var dataLines: [String] = []

    public init() {}

    public mutating func feed(_ chunk: String) -> [SSEEvent] {
        feed(Data(chunk.utf8))
    }

    /// Feed a raw chunk; returns any events completed by it.
    public mutating func feed(_ chunk: Data) -> [SSEEvent] {
        buffer.append(contentsOf: chunk)
        var events: [SSEEvent] = []
        // Process only complete lines; keep a trailing partial in the buffer.
        while let nl = buffer.firstIndex(of: 0x0A) {
            var lineBytes = buffer[..<nl]
            buffer.removeSubrange(...nl)
            if lineBytes.last == 0x0D { lineBytes = lineBytes.dropLast() }
            let line = String(decoding: lineBytes, as: UTF8.self)
            if let e = consume(line: line) { events.append(e) }
        }
        return events
    }

    private mutating func consume(line: String) -> SSEEvent? {
        if line.isEmpty {
            // blank line dispatches the pending event
            guard !dataLines.isEmpty || currentEvent != nil else { return nil }
            let e = SSEEvent(event: currentEvent, data: dataLines.joined(separator: "\n"))
            currentEvent = nil
            dataLines = []
            return e
        }
        if line.hasPrefix(":") { return nil }  // comment / keep-alive
        let (field, value): (String, String)
        if let colon = line.firstIndex(of: ":") {
            field = String(line[..<colon])
            var v = String(line[line.index(after: colon)...])
            if v.hasPrefix(" ") { v.removeFirst() }
            value = v
        } else {
            field = line
            value = ""
        }
        switch field {
        case "event": currentEvent = value
        case "data": dataLines.append(value)
        default: break  // id / retry / unknown — not used by the roost stream
        }
        return nil
    }
}

// MARK: - Job stream events (GET /jobs/{id}/stream)

public enum JobStreamEvent: Equatable, Sendable {
    case state(String)
    case log(LogLine)
    case done(JobDone)

    /// Maps one wire SSE event to a typed job event. Unknown event types
    /// return nil (forward-compatible); the server's `error` event throws.
    public static func parse(_ sse: SSEEvent) throws -> JobStreamEvent? {
        let data = Data(sse.data.utf8)
        let decoder = JSONDecoder()
        switch sse.event {
        case "state":
            struct S: Decodable { let state: String? }
            let s = try? decoder.decode(S.self, from: data)
            return .state(s?.state ?? "")
        case "log":
            guard let line = try? decoder.decode(LogLine.self, from: data) else { return nil }
            return .log(line)
        case "done":
            let d = (try? decoder.decode(JobDone.self, from: data))
                ?? JobDone(state: "", exitCode: nil, error: nil, result: nil, tokensUsed: nil)
            return .done(d)
        case "error":
            struct E: Decodable { let error: String? }
            let e = try? decoder.decode(E.self, from: data)
            throw RoostClientError.server(status: 0, message: e?.error ?? "stream error")
        default:
            return nil
        }
    }
}

public struct JobDone: Decodable, Equatable, Sendable {
    public let state: String
    public let exitCode: Int?
    public let error: String?
    public let result: JSONValue?
    public let tokensUsed: Int?

    public init(state: String, exitCode: Int?, error: String?,
                result: JSONValue?, tokensUsed: Int?) {
        self.state = state
        self.exitCode = exitCode
        self.error = error
        self.result = result
        self.tokensUsed = tokensUsed
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        state = (try? c.decode(String.self, forKey: "state")) ?? ""
        exitCode = try? c.decode(Int.self, forKey: "exit_code")
        error = try? c.decode(String.self, forKey: "error")
        result = try? c.decode(JSONValue.self, forKey: "result")
        tokensUsed = try? c.decode(Int.self, forKey: "tokens_used")
    }

    public var verified: Bool? { result?["verified"]?.boolValue }
    public var evidence: String? { result?["evidence"]?.stringValue }
    public var output: String? { result?["output"]?.stringValue ?? result?.stringValue }
}
