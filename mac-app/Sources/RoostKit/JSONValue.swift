import Foundation

/// A loosely-typed JSON value. Used for the parts of the API that are
/// free-form by design (job `spec`, worker `capabilities`, `result`), so the
/// client never breaks when the backend grows a field.
public enum JSONValue: Codable, Equatable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let b = try? container.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? container.decode(Double.self) {
            self = .number(n)
        } else if let s = try? container.decode(String.self) {
            self = .string(s)
        } else if let a = try? container.decode([JSONValue].self) {
            self = .array(a)
        } else if let o = try? container.decode([String: JSONValue].self) {
            self = .object(o)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "unsupported JSON value")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let b): try container.encode(b)
        case .number(let n): try container.encode(n)
        case .string(let s): try container.encode(s)
        case .array(let a): try container.encode(a)
        case .object(let o): try container.encode(o)
        }
    }

    // MARK: typed accessors

    public var stringValue: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    public var doubleValue: Double? {
        if case .number(let n) = self { return n }
        return nil
    }

    public var intValue: Int? {
        if case .number(let n) = self { return Int(n) }
        return nil
    }

    public var boolValue: Bool? {
        switch self {
        case .bool(let b): return b
        case .number(let n): return n != 0  // SQLite booleans arrive as 0/1
        default: return nil
        }
    }

    public var arrayValue: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }

    public var objectValue: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }

    public subscript(key: String) -> JSONValue? {
        objectValue?[key]
    }

    /// Human-facing rendering: strings unquoted, everything else compact JSON.
    public var displayText: String {
        switch self {
        case .null: return ""
        case .string(let s): return s
        case .bool(let b): return b ? "true" : "false"
        case .number(let n):
            return n == n.rounded() && abs(n) < 1e15
                ? String(Int(n)) : String(n)
        case .array, .object:
            guard let data = try? JSONEncoder().encode(self),
                  let text = String(data: data, encoding: .utf8) else { return "" }
            return text
        }
    }
}
