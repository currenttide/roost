import Foundation

/// Closed enum from API.md §2, but decoded *failably*: unknown server values
/// must render as text, never crash the app (contract is additive-only, §7).
/// We model the open set with `.unknown(String)` so the raw value survives.
enum HealthStatus: Equatable, Hashable {
    case verified, done, unverified, failed, cancelled
    case running, verifying, selfHealing, queued, waiting
    case unplaceable, stuckQuestion
    case unknown(String)

    init(raw: String) {
        switch raw {
        case "verified": self = .verified
        case "done": self = .done
        case "unverified": self = .unverified
        case "failed": self = .failed
        case "cancelled": self = .cancelled
        case "running": self = .running
        case "verifying": self = .verifying
        case "self-healing": self = .selfHealing
        case "queued": self = .queued
        case "waiting": self = .waiting
        case "unplaceable": self = .unplaceable
        case "stuck?": self = .stuckQuestion
        default: self = .unknown(raw)
        }
    }

    /// Glyph per API.md §2. Unknown → the raw string itself (caller renders it
    /// as plain text), so nothing is dropped and nothing crashes.
    var glyph: String {
        switch self {
        case .verified, .done: return "✓"
        case .unverified, .unplaceable, .stuckQuestion: return "⚠"
        case .failed: return "✗"
        case .cancelled: return "−"
        case .running, .verifying, .selfHealing: return "▶"
        case .queued: return "○"
        case .waiting: return "◔"
        case .unknown(let raw): return raw
        }
    }

    /// True when this status represents an in-flight job (drives the spinner /
    /// running-first sort fallback). Terminal states return false.
    var isActive: Bool {
        switch self {
        case .running, .verifying, .selfHealing, .queued, .waiting: return true
        default: return false
        }
    }

    var isError: Bool {
        switch self {
        case .failed, .unplaceable, .stuckQuestion, .unverified: return true
        default: return false
        }
    }
}

/// Decodable wrapper so `Health.status` can be a failable enum while the parent
/// struct stays pure-Codable.
struct Health: Codable, Equatable, Hashable {
    let status: HealthStatus
    let reason: String?

    enum CodingKeys: String, CodingKey { case status, reason }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.status = HealthStatus(raw: try c.decode(String.self, forKey: .status))
        self.reason = try c.decodeIfPresent(String.self, forKey: .reason)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        // Re-encode is only used in tests; round-trip the raw form.
        let raw: String
        switch status {
        case .verified: raw = "verified"
        case .done: raw = "done"
        case .unverified: raw = "unverified"
        case .failed: raw = "failed"
        case .cancelled: raw = "cancelled"
        case .running: raw = "running"
        case .verifying: raw = "verifying"
        case .selfHealing: raw = "self-healing"
        case .queued: raw = "queued"
        case .waiting: raw = "waiting"
        case .unplaceable: raw = "unplaceable"
        case .stuckQuestion: raw = "stuck?"
        case .unknown(let v): raw = v
        }
        try c.encode(raw, forKey: .status)
        try c.encodeIfPresent(reason, forKey: .reason)
    }
}
