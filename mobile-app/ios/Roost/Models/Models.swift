import Foundation

// MARK: - Pairing (API.md §1)

/// The payload encoded in `roost://pair?d=<base64url>`.
struct PairPayload: Codable, Equatable {
    let v: Int
    let url: String
    let token: String
    let name: String?
}

/// `GET /healthz` (unauthenticated reachability probe).
struct Healthz: Codable, Equatable {
    let ok: Bool
    let version: String
}

/// `roost pair` mint response (`pair_token_response.json`). The app does not
/// mint these itself, but decoding it keeps the contract test honest.
struct PairTokenResponse: Codable, Equatable {
    let id: String
    let token: String
    let label: String?
    let scope: String
    let createdAt: Double

    enum CodingKeys: String, CodingKey {
        case id, token, label, scope
        case createdAt = "created_at"
    }
}

// MARK: - Dashboard (API.md §2)

struct Derived: Codable, Equatable {
    let generatedAt: Double
    let fleetVerdict: FleetVerdict
    let workers: [Worker]
    let runs: [Run]

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case fleetVerdict = "fleet_verdict"
        case workers, runs
    }
}

struct FleetVerdict: Codable, Equatable {
    /// "ok" | "alert" — but kept as raw string so an unknown level renders
    /// rather than crashes (additive contract, §7). `isAlert` drives the color.
    let level: String
    let summary: String

    var isAlert: Bool { level != "ok" }
}

struct Cost: Codable, Equatable, Hashable {
    let tokensUsed: Int?
    let costEstUsd: Double?
    let budgetPct: Double?

    enum CodingKeys: String, CodingKey {
        case tokensUsed = "tokens_used"
        case costEstUsd = "cost_est_usd"
        case budgetPct = "budget_pct"
    }
}

/// A dashboard run row (API.md §2). Its `result` is a *string* here, unlike the
/// job-detail object — they are distinct shapes despite the shared name.
struct Run: Codable, Equatable, Identifiable {
    let runId: String
    let goal: String?
    let state: String
    let phase: String?
    let health: Health?
    let worker: String?
    let verified: Bool?
    let evidence: String?
    let result: String?
    let narration: String?
    let progress: Double?
    let etaSec: Int?
    let cost: Cost?
    let createdAt: Double?
    let finishedAt: Double?
    let lastActivity: String?
    let rootJobId: String?
    let capableWorkers: Int?
    let declineCount: Int?
    let diagnosis: String?

    var id: String { runId }

    enum CodingKeys: String, CodingKey {
        case runId = "run_id"
        case goal, state, phase, health, worker, verified, evidence, result
        case narration, progress, cost, diagnosis
        case etaSec = "eta_sec"
        case createdAt = "created_at"
        case finishedAt = "finished_at"
        case lastActivity = "last_activity"
        case rootJobId = "root_job_id"
        case capableWorkers = "capable_workers"
        case declineCount = "decline_count"
    }

    /// Health may be absent on some rows; synthesize from `state` so the UI
    /// always has a glyph. (Contract guarantees `health` in §2, but defensive.)
    var healthStatus: HealthStatus {
        if let h = health { return h.status }
        return HealthStatus(raw: state)
    }

    /// Best one-liner to show under the title.
    var subtitle: String? {
        narration ?? lastActivity ?? health?.reason ?? (result?.isEmpty == false ? result : nil)
    }
}

struct Worker: Codable, Equatable, Identifiable {
    let id: String
    let name: String?
    let status: String
    let lastSeen: Double?
    let running: Int?
    let capacity: Int?

    enum CodingKeys: String, CodingKey {
        case id, name, status, running, capacity
        case lastSeen = "last_seen"
    }

    /// idle + busy count toward the live "N nodes" chip (API.md §2).
    var isLive: Bool { status == "idle" || status == "busy" }
}

// MARK: - Job detail (API.md §3/§4)

/// The structured result object on a job detail / done event
/// (`{output, verified, evidence}`). Distinct from the run-row string `result`.
struct JobResult: Codable, Equatable {
    let output: String?
    let verified: Bool?
    let evidence: String?
}

/// Full job object (`job_detail_*.json`, `job_submit_response.json`, tree rows).
/// We decode the subset the app renders; unknown fields are ignored.
struct Job: Codable, Equatable, Identifiable {
    let id: String
    let intent: String?
    let state: String
    let workerId: String?
    let result: JobResult?
    let error: String?
    let exitCode: Int?
    let tokensUsed: Int?
    let createdAt: Double?
    let startedAt: Double?
    let finishedAt: Double?
    let parentJobId: String?
    let rootJobId: String?
    let requires: [String: JSONValue]?
    let spec: JobSpec?

    enum CodingKeys: String, CodingKey {
        case id, intent, state, result, error, requires, spec
        case workerId = "worker_id"
        case exitCode = "exit_code"
        case tokensUsed = "tokens_used"
        case createdAt = "created_at"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case parentJobId = "parent_job_id"
        case rootJobId = "root_job_id"
    }
}

/// Echo of the submit spec. We need `intent`, `command`, `kind`, `requires`
/// to power client-side retry (API.md §4).
struct JobSpec: Codable, Equatable {
    let intent: String?
    let command: String?
    let kind: String?
    let requires: [String: JSONValue]?
}

// MARK: - Logs (API.md §4)

struct LogPage: Codable, Equatable {
    let jobId: String
    let state: String?
    let logs: [LogRow]

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id"
        case state, logs
    }
}

struct LogRow: Codable, Equatable, Identifiable, Hashable {
    let seq: Int
    let stream: String       // "stdout" | "stderr" | "event"
    let data: String
    let ts: Double?

    var id: Int { seq }

    /// Turn an `event` row's JSON (`{"type": "started"|"succeeded"|…}`) into a
    /// short divider label, or nil if unparseable (API.md §4: skip those).
    static func eventLabel(_ json: String) -> String? {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = obj["type"] as? String
        else { return nil }
        return type
    }
}

/// A renderable log line. `event` rows become subtle dividers; stdout/stderr
/// are monospaced, ANSI-stripped text. Codable because the offline cache
/// persists the rendered tail per job (DESIGN §5).
struct DisplayLine: Identifiable, Equatable, Codable {
    let seq: Int
    let kind: Kind
    let text: String
    var id: Int { seq }

    enum Kind: String, Equatable, Codable { case stdout, stderr, event }

    /// Render a wire `LogRow` (ANSI-stripped; event rows become divider labels,
    /// unparseable ones are skipped → nil).
    static func from(_ row: LogRow) -> DisplayLine? {
        switch row.stream {
        case "event":
            guard let label = LogRow.eventLabel(row.data) else { return nil }
            return DisplayLine(seq: row.seq, kind: .event, text: label)
        case "stderr":
            return DisplayLine(seq: row.seq, kind: .stderr, text: Ansi.strip(row.data))
        default:   // "stdout" and anything else render as stdout text
            return DisplayLine(seq: row.seq, kind: .stdout, text: Ansi.strip(row.data))
        }
    }
}

// MARK: - Submit (API.md §3)

/// Exactly the fields the app sends (API.md §3). `intent`/`command` are mutually
/// exclusive, so we encode only the present one (no stray `null`s on the wire);
/// `requires` is always sent (`{}` = auto-place).
struct JobSubmit: Encodable, Equatable {   // encode-only: `hierarchy` is derived
    let intent: String?
    let kind: String
    let requires: [String: JSONValue]
    let command: String?

    struct Hierarchy: Encodable, Equatable {
        let canDispatch: Bool
        enum CodingKeys: String, CodingKey { case canDispatch = "can_dispatch" }
    }

    /// Agent jobs carry can_dispatch so the worker injects the roost MCP —
    /// without it the agent is fleet-blind ("how many machines do I have?" →
    /// "I don't know"). Depth/tree-budget guardrails bound what it can spawn.
    var hierarchy: Hierarchy? {
        kind == "claude" ? Hierarchy(canDispatch: true) : nil
    }

    enum CodingKeys: String, CodingKey { case intent, kind, requires, command, hierarchy }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(kind, forKey: .kind)
        try c.encode(requires, forKey: .requires)
        try c.encodeIfPresent(intent, forKey: .intent)
        try c.encodeIfPresent(command, forKey: .command)
        try c.encodeIfPresent(hierarchy, forKey: .hierarchy)
    }
}

// MARK: - Cancel (API.md §4)

struct CancelResponse: Codable, Equatable {
    let cancelled: Int
}

// MARK: - Publish (API.md §6)

/// Staged bundle — `POST /blobs` response (`blob_upload_response.json`).
/// Step 1 of publish; only `id` and `state` matter to the flow.
struct BlobUploadResponse: Codable, Equatable {
    let id: String
    let name: String
    let size: Int
    let sha256: String?
    let state: String          // "ready" once the body landed
    let createdAt: Double
    let expiresAt: Double      // blob TTL, NOT site TTL (API.md §6)
    let getUrl: String?

    enum CodingKeys: String, CodingKey {
        case id, name, size, sha256, state
        case createdAt = "created_at"
        case expiresAt = "expires_at"
        case getUrl = "get_url"
    }
}

/// A published site — `POST /publish` / `GET /publish` rows
/// (`publish_response.json`, `publish_list.json`).
struct Site: Codable, Equatable, Identifiable {
    let slug: String
    let url: String            // LAN URL, always present
    let publicUrl: String?     // only when the CP has a publish domain
    let files: Int
    let size: Int
    let createdAt: Double
    let updatedAt: Double

    var id: String { slug }

    /// Best link to offer the user: internet-facing when available.
    var shareUrl: String { publicUrl ?? url }

    enum CodingKeys: String, CodingKey {
        case slug, url, files, size
        case publicUrl = "public_url"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

// MARK: - Error envelope (API.md §1)

struct ErrorEnvelope: Codable, Equatable {
    let detail: String
}
