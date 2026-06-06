import Foundation

// Wire models for the Roost control plane (roost/server.py, v0.2.0).
//
// Decoding is deliberately tolerant (DESIGN.md §12.1): unknown fields are
// ignored, and everything beyond identity is optional-with-defaults, so the
// app degrades to "snapshot partially understood" instead of crashing when
// the backend evolves.

// MARK: - /healthz

public struct Healthz: Decodable, Sendable {
    public let ok: Bool
    public let version: String?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        ok = (try? c.decode(Bool.self, forKey: "ok")) ?? false
        version = try? c.decode(String.self, forKey: "version")
    }
}

// MARK: - Fleet verdict (from /derived)

public struct FleetVerdict: Decodable, Equatable, Sendable {
    public enum Level: String, Sendable {
        case ok, alert, unknown
    }

    public let level: Level
    public let summary: String

    public init(level: Level, summary: String) {
        self.level = level
        self.summary = summary
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let raw = (try? c.decode(String.self, forKey: "level")) ?? ""
        level = Level(rawValue: raw) ?? .unknown
        summary = (try? c.decode(String.self, forKey: "summary")) ?? ""
    }
}

// MARK: - Run (the derived "story" of one job, from _derive_run)

public struct Run: Decodable, Identifiable, Equatable, Sendable {
    public struct Health: Decodable, Equatable, Sendable {
        /// queued | waiting | unplaceable | running | stuck? | verifying |
        /// self-healing | verified | unverified | done | failed | cancelled
        public let status: String
        public let reason: String

        public init(status: String, reason: String) {
            self.status = status
            self.reason = reason
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyCodingKey.self)
            status = (try? c.decode(String.self, forKey: "status")) ?? ""
            reason = (try? c.decode(String.self, forKey: "reason")) ?? ""
        }
    }

    public struct Cost: Decodable, Equatable, Sendable {
        public let tokensUsed: Int
        public let costEstUSD: Double
        public let budgetPct: Double?

        public init(tokensUsed: Int, costEstUSD: Double, budgetPct: Double?) {
            self.tokensUsed = tokensUsed
            self.costEstUSD = costEstUSD
            self.budgetPct = budgetPct
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyCodingKey.self)
            tokensUsed = (try? c.decode(Int.self, forKey: "tokens_used")) ?? 0
            costEstUSD = (try? c.decode(Double.self, forKey: "cost_est_usd")) ?? 0
            budgetPct = try? c.decode(Double.self, forKey: "budget_pct")
        }
    }

    public let id: String                 // run_id
    public let goal: String
    public let state: String              // queued|assigned|running|succeeded|failed|cancelled
    public let phase: String              // state, or verifying|self-healing
    public let health: Health
    public let worker: String?            // worker id
    public let verified: Bool?
    public let evidence: String?
    public let result: String?            // output or error, truncated server-side
    public let diagnosis: String?
    public let lastActivity: String?
    public let idleSec: Double?
    public let queuedSec: Double?
    public let capableWorkers: Int?
    public let declineCount: Int?
    public let cost: Cost
    public let narration: String?
    public let progress: Int?             // 0–100 or nil; never invented client-side
    public let etaSec: Int?
    public let rootJobID: String?
    public let createdAt: Double?
    public let finishedAt: Double?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        guard let rid = try? c.decode(String.self, forKey: "run_id") else {
            throw DecodingError.keyNotFound(
                AnyCodingKey("run_id"),
                .init(codingPath: decoder.codingPath, debugDescription: "run without run_id"))
        }
        id = rid
        goal = (try? c.decode(String.self, forKey: "goal")) ?? ""
        state = (try? c.decode(String.self, forKey: "state")) ?? ""
        phase = (try? c.decode(String.self, forKey: "phase")) ?? state
        health = (try? c.decode(Health.self, forKey: "health")) ?? Health(status: "", reason: "")
        worker = try? c.decode(String.self, forKey: "worker")
        verified = try? c.decode(Bool.self, forKey: "verified")
        evidence = try? c.decode(String.self, forKey: "evidence")
        result = try? c.decode(String.self, forKey: "result")
        diagnosis = try? c.decode(String.self, forKey: "diagnosis")
        lastActivity = try? c.decode(String.self, forKey: "last_activity")
        idleSec = try? c.decode(Double.self, forKey: "idle_sec")
        queuedSec = try? c.decode(Double.self, forKey: "queued_sec")
        capableWorkers = try? c.decode(Int.self, forKey: "capable_workers")
        declineCount = try? c.decode(Int.self, forKey: "decline_count")
        cost = (try? c.decode(Cost.self, forKey: "cost"))
            ?? Cost(tokensUsed: 0, costEstUSD: 0, budgetPct: nil)
        narration = try? c.decode(String.self, forKey: "narration")
        progress = try? c.decode(Int.self, forKey: "progress")
        etaSec = try? c.decode(Int.self, forKey: "eta_sec")
        rootJobID = try? c.decode(String.self, forKey: "root_job_id")
        createdAt = try? c.decode(Double.self, forKey: "created_at")
        finishedAt = try? c.decode(Double.self, forKey: "finished_at")
    }

    public var isTerminal: Bool {
        ["succeeded", "failed", "cancelled"].contains(state)
    }

    public var isActive: Bool { !isTerminal }
}

// MARK: - Worker

public struct Worker: Decodable, Identifiable, Equatable, Sendable {
    public enum Status: String, Sendable {
        case idle, busy, stale, offline, unknown
    }

    public let id: String
    public let name: String
    public let statusRaw: String
    public let capabilities: JSONValue
    public let registeredAt: Double?
    public let lastSeen: Double?
    public let running: Int
    public let capacity: Int
    public let revoked: Bool
    public let policy: JSONValue?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        guard let wid = try? c.decode(String.self, forKey: "id") else {
            throw DecodingError.keyNotFound(
                AnyCodingKey("id"),
                .init(codingPath: decoder.codingPath, debugDescription: "worker without id"))
        }
        id = wid
        name = (try? c.decode(String.self, forKey: "name")) ?? wid
        statusRaw = (try? c.decode(String.self, forKey: "status")) ?? ""
        capabilities = (try? c.decode(JSONValue.self, forKey: "capabilities")) ?? .object([:])
        registeredAt = try? c.decode(Double.self, forKey: "registered_at")
        lastSeen = try? c.decode(Double.self, forKey: "last_seen")
        running = (try? c.decode(Int.self, forKey: "running")) ?? 0
        capacity = max(1, (try? c.decode(Int.self, forKey: "capacity")) ?? 1)
        // SQLite booleans arrive as 0/1
        if let b = try? c.decode(Bool.self, forKey: "revoked") {
            revoked = b
        } else {
            revoked = ((try? c.decode(Int.self, forKey: "revoked")) ?? 0) != 0
        }
        policy = try? c.decode(JSONValue.self, forKey: "policy")
    }

    public var status: Status { Status(rawValue: statusRaw) ?? .unknown }

    // MARK: capability accessors

    public var os: String? { capabilities["os"]?.stringValue }
    public var arch: String? { capabilities["arch"]?.stringValue }
    public var hostname: String? { capabilities["hostname"]?.stringValue }
    public var cpus: Int? { capabilities["cpus"]?.intValue }
    public var ramGB: Double? { capabilities["ram_gb"]?.doubleValue }

    public var tools: [String] {
        capabilities["tools"]?.arrayValue?.compactMap(\.stringValue) ?? []
    }

    public var gpuNames: [String] {
        capabilities["gpu"]?.arrayValue?.compactMap(\.stringValue) ?? []
    }

    public var gpuVRAMGB: Double? { capabilities["gpu_vram_gb"]?.doubleValue }

    public var freeVRAMGB: Double? {
        capabilities["load"]?["free_vram_gb"]?.doubleValue
    }

    public var hasClaude: Bool { tools.contains("claude") }

    /// Two-piece capability summary for list rows (DESIGN.md §2.2):
    /// GPU > claude > cpus, pick two.
    public var headline: String {
        var pieces: [String] = []
        if let gpu = gpuNames.first {
            var s = gpu
            if let free = freeVRAMGB {
                s += String(format: " · %.0f GB free", free)
            } else if let vram = gpuVRAMGB {
                s += String(format: " · %.0f GB", vram)
            }
            pieces.append(s)
        }
        if pieces.count < 2, hasClaude { pieces.append("claude ✓") }
        if pieces.count < 2, let n = cpus {
            let prefix = [arch, os].compactMap { $0 }.first.map { "\($0) · " } ?? ""
            pieces.append("\(prefix)\(n) cpu")
        }
        if pieces.isEmpty, let os { pieces.append(os) }
        return pieces.prefix(2).joined(separator: "   ")
    }
}

// MARK: - /derived snapshot

public struct DerivedSnapshot: Decodable, Equatable, Sendable {
    public let generatedAt: Double
    public let fleetVerdict: FleetVerdict
    public let workers: [Worker]
    public let runs: [Run]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        generatedAt = (try? c.decode(Double.self, forKey: "generated_at")) ?? 0
        fleetVerdict = (try? c.decode(FleetVerdict.self, forKey: "fleet_verdict"))
            ?? FleetVerdict(level: .unknown, summary: "")
        // Tolerate single bad rows without dropping the whole snapshot.
        workers = ((try? c.decode([Tolerant<Worker>].self, forKey: "workers")) ?? [])
            .compactMap(\.value)
        runs = ((try? c.decode([Tolerant<Run>].self, forKey: "runs")) ?? [])
            .compactMap(\.value)
    }
}

// MARK: - Raw job (from /jobs, /jobs/{id}, /jobs/{id}/tree)

public struct Job: Decodable, Identifiable, Equatable, Sendable {
    public let id: String
    public let state: String
    public let spec: JSONValue
    public let intent: String?
    public let workerID: String?
    public let parentJobID: String?
    public let rootJobID: String?
    public let depth: Int
    public let createdAt: Double?
    public let startedAt: Double?
    public let finishedAt: Double?
    public let exitCode: Int?
    public let result: JSONValue?
    public let error: String?
    public let tokensUsed: Int
    public let lastActivity: String?
    public let diagnosis: String?
    public let attempt: Int
    public let maxAttempts: Int

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        guard let jid = try? c.decode(String.self, forKey: "id") else {
            throw DecodingError.keyNotFound(
                AnyCodingKey("id"),
                .init(codingPath: decoder.codingPath, debugDescription: "job without id"))
        }
        id = jid
        state = (try? c.decode(String.self, forKey: "state")) ?? ""
        spec = (try? c.decode(JSONValue.self, forKey: "spec")) ?? .object([:])
        intent = try? c.decode(String.self, forKey: "intent")
        workerID = try? c.decode(String.self, forKey: "worker_id")
        parentJobID = try? c.decode(String.self, forKey: "parent_job_id")
        rootJobID = try? c.decode(String.self, forKey: "root_job_id")
        depth = (try? c.decode(Int.self, forKey: "depth")) ?? 0
        createdAt = try? c.decode(Double.self, forKey: "created_at")
        startedAt = try? c.decode(Double.self, forKey: "started_at")
        finishedAt = try? c.decode(Double.self, forKey: "finished_at")
        exitCode = try? c.decode(Int.self, forKey: "exit_code")
        result = try? c.decode(JSONValue.self, forKey: "result")
        error = try? c.decode(String.self, forKey: "error")
        tokensUsed = (try? c.decode(Int.self, forKey: "tokens_used")) ?? 0
        lastActivity = try? c.decode(String.self, forKey: "last_activity")
        diagnosis = try? c.decode(String.self, forKey: "diagnosis")
        attempt = (try? c.decode(Int.self, forKey: "attempt")) ?? 0
        maxAttempts = (try? c.decode(Int.self, forKey: "max_attempts")) ?? 0
    }

    /// Mirrors the server's _goal_text(): task > intent > command, truncated.
    public var goal: String {
        let g = spec["task"]?.stringValue
            ?? spec["intent"]?.stringValue
            ?? intent
            ?? specCommandText
            ?? ""
        return String(g.prefix(140))
    }

    private var specCommandText: String? {
        if let s = spec["command"]?.stringValue { return s }
        if let parts = spec["command"]?.arrayValue?.compactMap(\.stringValue) {
            return parts.joined(separator: " ")
        }
        return nil
    }

    public var isTerminal: Bool {
        ["succeeded", "failed", "cancelled"].contains(state)
    }
}

// MARK: - Logs

public struct LogLine: Decodable, Identifiable, Equatable, Sendable {
    public let seq: Int
    public let stream: String   // stdout | stderr | event
    public let data: JSONValue
    public let ts: Double

    public var id: Int { seq }
    public var text: String { data.displayText }

    public init(seq: Int, stream: String, data: JSONValue, ts: Double) {
        self.seq = seq
        self.stream = stream
        self.data = data
        self.ts = ts
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        seq = (try? c.decode(Int.self, forKey: "seq")) ?? 0
        stream = (try? c.decode(String.self, forKey: "stream")) ?? "stdout"
        data = (try? c.decode(JSONValue.self, forKey: "data")) ?? .string("")
        ts = (try? c.decode(Double.self, forKey: "ts")) ?? 0
    }
}

public struct LogsResponse: Decodable, Sendable {
    public let jobID: String
    public let state: String
    public let logs: [LogLine]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        jobID = (try? c.decode(String.self, forKey: "job_id")) ?? ""
        state = (try? c.decode(String.self, forKey: "state")) ?? ""
        logs = ((try? c.decode([Tolerant<LogLine>].self, forKey: "logs")) ?? [])
            .compactMap(\.value)
    }
}

// MARK: - Mutations

/// Body for POST /jobs. Encodes only the fields that are set, matching the
/// server's JobSubmit pydantic model.
public struct JobSubmission: Encodable, Sendable {
    public var task: String?
    public var intent: String?
    public var command: String?         // command jobs (e.g. transfer delivery legs)
    public var kind: String?            // "auto" | "captain" | "command" | "docker"
    public var verify: Bool?
    public var requires: [String: String]?  // hard placement pin (exact match)
    public var prefer: [String: String]?
    public var model: String?
    public var budget: [String: Int]?
    public var hierarchy: [String: Bool]?
    public var maxAttempts: Int?

    public init(
        task: String? = nil,
        intent: String? = nil,
        command: String? = nil,
        kind: String? = nil,
        verify: Bool? = nil,
        requires: [String: String]? = nil,
        prefer: [String: String]? = nil,
        model: String? = nil,
        budget: [String: Int]? = nil,
        hierarchy: [String: Bool]? = nil,
        maxAttempts: Int? = nil
    ) {
        self.task = task
        self.intent = intent
        self.command = command
        self.kind = kind
        self.verify = verify
        self.requires = requires
        self.prefer = prefer
        self.model = model
        self.budget = budget
        self.hierarchy = hierarchy
        self.maxAttempts = maxAttempts
    }

    /// The default goal-box submission (DESIGN.md §4): the fleet decides who
    /// runs it, and the trust loop verifies the result.
    public static func goal(
        _ text: String,
        captain: Bool = false,
        verify: Bool = true,
        preferWorker: String? = nil,
        model: String? = nil,
        maxTokens: Int? = nil
    ) -> JobSubmission {
        JobSubmission(
            task: text,
            intent: text,
            kind: captain ? "captain" : "auto",
            verify: verify,
            prefer: preferWorker.map { ["worker": $0] },
            model: model,
            budget: maxTokens.map { ["max_tokens": $0] },
            hierarchy: captain ? ["can_dispatch": true] : nil
        )
    }

    enum CodingKeys: String, CodingKey {
        case task, intent, command, kind, verify, requires, prefer, model
        case budget, hierarchy
        case maxAttempts = "max_attempts"
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(task, forKey: .task)
        try c.encodeIfPresent(intent, forKey: .intent)
        try c.encodeIfPresent(command, forKey: .command)
        try c.encodeIfPresent(kind, forKey: .kind)
        try c.encodeIfPresent(verify, forKey: .verify)
        try c.encodeIfPresent(requires, forKey: .requires)
        try c.encodeIfPresent(prefer, forKey: .prefer)
        try c.encodeIfPresent(model, forKey: .model)
        try c.encodeIfPresent(budget, forKey: .budget)
        try c.encodeIfPresent(hierarchy, forKey: .hierarchy)
        try c.encodeIfPresent(maxAttempts, forKey: .maxAttempts)
    }
}

public struct CancelResponse: Decodable, Sendable {
    public let cancelled: Int

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        cancelled = (try? c.decode(Int.self, forKey: "cancelled")) ?? 0
    }
}

public struct PruneResponse: Decodable, Sendable {
    public let pruned: Int
    public let names: [String]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        pruned = (try? c.decode(Int.self, forKey: "pruned")) ?? 0
        names = (try? c.decode([String].self, forKey: "names")) ?? []
    }
}

// MARK: - decoding helpers

/// String-keyed CodingKey for tolerant hand-written decoders.
struct AnyCodingKey: CodingKey, ExpressibleByStringLiteral {
    var stringValue: String
    var intValue: Int? { nil }

    init(_ string: String) { stringValue = string }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
    init(stringLiteral value: String) { stringValue = value }
}

/// Wraps an element decode so one malformed row doesn't sink the array.
struct Tolerant<T: Decodable>: Decodable {
    let value: T?

    init(from decoder: Decoder) throws {
        value = try? T(from: decoder)
    }
}
