import Foundation

/// A type-erased JSON value. Roost's `requires` and `spec` carry free-form maps
/// whose value types are heterogeneous (`">=24"` vs `["python3"]`), so we can't
/// model them with a concrete Codable struct. We only need to *carry* and
/// occasionally *display* these blobs, never to interpret them — so a recursive
/// enum that decodes anything (and re-encodes faithfully) is the right weight.
enum JSONValue: Codable, Equatable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let v = try? c.decode(Bool.self) {
            // Bool must be probed before Double: JSON `true` would otherwise
            // slip through as a number on some platforms.
            self = .bool(v)
        } else if let v = try? c.decode(Double.self) {
            self = .number(v)
        } else if let v = try? c.decode(String.self) {
            self = .string(v)
        } else if let v = try? c.decode([JSONValue].self) {
            self = .array(v)
        } else if let v = try? c.decode([String: JSONValue].self) {
            self = .object(v)
        } else {
            throw DecodingError.dataCorruptedError(
                in: c, debugDescription: "Unsupported JSON value")
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }

    /// Best-effort human string for display in the UI (e.g. a requires chip).
    var displayString: String {
        switch self {
        case .string(let v): return v
        case .number(let v):
            return v == v.rounded() ? String(Int(v)) : String(v)
        case .bool(let v): return v ? "true" : "false"
        case .array(let a): return a.map { $0.displayString }.joined(separator: ", ")
        case .object(let o):
            return o.map { "\($0): \($1.displayString)" }.joined(separator: ", ")
        case .null: return "—"
        }
    }
}
