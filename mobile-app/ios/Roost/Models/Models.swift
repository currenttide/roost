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
    /// Effective executor kind (API.md §2: "command"|"claude"|"docker"|…). Optional:
    /// an older CP omits it, so the kind subtitle segment is dropped rather than
    /// guessed (R85 — guessing "claude" for every job was the bug; iOS previously
    /// showed no kind at all, this adds the truthful one).
    let kind: String?
    /// R86: server-summarized glanceable goal for the verdict bar (collapses a
    /// raw `command`'s shell text). Absent against older control planes — read
    /// via `displayGoal`, which falls back to `goal`.
    let goalDisplay: String?
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
        case goal, kind, state, phase, health, worker, verified, evidence, result
        case narration, progress, cost, diagnosis
        case goalDisplay = "goal_display"
        case etaSec = "eta_sec"
        case createdAt = "created_at"
        case finishedAt = "finished_at"
        case lastActivity = "last_activity"
        case rootJobId = "root_job_id"
        case capableWorkers = "capable_workers"
        case declineCount = "decline_count"
    }

    /// R86: the goal to show in a glanceable row. Prefers the server's
    /// summarized `goal_display`; falls back to the full `goal` against an
    /// older control plane that doesn't send it.
    var displayGoal: String? {
        if let g = goalDisplay, !g.isEmpty { return g }
        return goal
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
    /// Free-form capability map (API.md §2a) — heterogeneous values
    /// (`"x86_64"`, `16`, `["python3"]`), carried type-erased and summarized
    /// for the Fleet screen by `Fleet.capsSummary`.
    let capabilities: [String: JSONValue]?

    enum CodingKeys: String, CodingKey {
        case id, name, status, running, capacity, capabilities
        case lastSeen = "last_seen"
    }

    /// idle + busy count toward the live "N nodes" chip (API.md §2).
    var isLive: Bool { status == "idle" || status == "busy" }

    /// Best display name (the server may register a worker without one).
    var displayName: String { (name?.isEmpty == false ? name : nil) ?? id }
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
    ///
    /// `raw` selects how `stdout` rows render (R108). The DEFAULT
    /// (`raw == false`) DISTILLS each stdout line of an agent job's stream-json
    /// into a readable transcript via `DistilledLine.from` — assistant text,
    /// `→ Tool: summary`, truncated results; phase dividers kept; base64
    /// signatures, reasoning blobs, and roost-internal `event` envelopes
    /// suppressed (those distil to `nil` → the row is dropped). A plain
    /// `command` job's stdout is not stream-json, so it passes through verbatim.
    /// `raw == true` reproduces today's exact firehose (every stdout line shown
    /// as-is, ANSI-stripped). The shared golden fixtures under
    /// `mobile-app/fixtures/distilled/` pin the distilled output across clients.
    static func from(_ row: LogRow, raw: Bool = false) -> DisplayLine? {
        switch row.stream {
        case "event":
            guard let label = LogRow.eventLabel(row.data) else { return nil }
            return DisplayLine(seq: row.seq, kind: .event, text: label)
        case "stderr":
            return DisplayLine(seq: row.seq, kind: .stderr, text: Ansi.strip(row.data))
        default:   // "stdout" and anything else render as stdout text
            if raw {
                return DisplayLine(seq: row.seq, kind: .stdout, text: Ansi.strip(row.data))
            }
            // Distilled (default): suppress noise lines (nil); ANSI-strip the
            // survivors. Distillation parses the raw stream-json (which carries
            // no ANSI), so it runs before the strip.
            guard let distilled = DistilledLine.from(row.data) else { return nil }
            return DisplayLine(seq: row.seq, kind: .stdout, text: Ansi.strip(distilled))
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

// MARK: - Schedules (API.md §7)

/// An interval schedule — `POST /schedules` / `GET /schedules` rows
/// (`schedule_create_response.json`, `schedules_list.json`). The CP re-submits
/// `spec` every `intervalSec`; we render the clock and toggle `enabled`.
struct Schedule: Codable, Equatable, Identifiable {
    let id: String
    let name: String?
    let spec: JobSpec?
    let intervalSec: Double
    let enabled: Bool
    let nextRunAt: Double?
    let lastRunAt: Double?
    let lastJobId: String?
    let createdAt: Double

    enum CodingKeys: String, CodingKey {
        case id, name, spec, enabled
        case intervalSec = "interval_sec"
        case nextRunAt = "next_run_at"
        case lastRunAt = "last_run_at"
        case lastJobId = "last_job_id"
        case createdAt = "created_at"
    }

    /// One-liner describing what this schedule runs, for the list row. Prefers the
    /// agent `intent`, then a `command`, then a generic kind/label fallback so a
    /// future spec shape still renders something (never blank).
    var taskSummary: String {
        if let intent = spec?.intent, !intent.isEmpty { return intent }
        if let command = spec?.command, !command.isEmpty { return command }
        return name ?? (spec?.kind.map { "\($0) job" } ?? "scheduled job")
    }
}

/// `POST /schedules` request body (API.md §7a). `every` is seconds or
/// "<N>[smhd]"; `spec` is the §3 submit shape carried as a free-form map.
struct ScheduleCreate: Encodable, Equatable {
    let spec: [String: JSONValue]
    let every: String
    let name: String?
    let enabled: Bool

    enum CodingKeys: String, CodingKey { case spec, every, name, enabled }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(spec, forKey: .spec)
        try c.encode(every, forKey: .every)
        try c.encode(enabled, forKey: .enabled)
        try c.encodeIfPresent(name, forKey: .name)
    }
}

/// `PATCH /schedules/{id}` body — enable/disable (API.md §7c).
struct SchedulePatch: Encodable, Equatable {
    let enabled: Bool
}

/// `DELETE /schedules/{id}` response (API.md §7d).
struct ScheduleDeleteResponse: Codable, Equatable {
    let deleted: Bool
    let id: String
}

// MARK: - Follow-up input (R38, API.md §4)

/// Ack for `POST /jobs/{id}/input` (`job_input_response.json`). `state` here is
/// always `queued`; the worker's later delivery shows up via `JobInput`.
struct JobInputAck: Codable, Equatable {
    let inputId: String
    let jobId: String
    let state: String

    enum CodingKeys: String, CodingKey {
        case state
        case inputId = "input_id"
        case jobId = "job_id"
    }
}

/// One follow-up input row of `GET /jobs/{id}/inputs` (`job_inputs_list.json`).
/// `detail` carries the drop reason when `state == "dropped"` (agent/docker jobs
/// run with stdin closed, so their input is honestly dropped, not delivered).
struct JobInput: Codable, Equatable, Identifiable {
    let id: String
    let state: String          // "queued" | "delivered" | "dropped"
    let detail: String?
    let createdAt: Double
    let deliveredAt: Double?
    let createdBy: String?

    var isDelivered: Bool { state == "delivered" }
    var isDropped: Bool { state == "dropped" }
    var isQueued: Bool { state == "queued" }

    enum CodingKeys: String, CodingKey {
        case id, state, detail
        case createdAt = "created_at"
        case deliveredAt = "delivered_at"
        case createdBy = "created_by"
    }
}

/// `GET /jobs/{id}/inputs` response: the job's follow-up queue (API.md §4).
struct JobInputs: Codable, Equatable {
    let jobId: String
    let state: String          // the JOB's state
    let inputs: [JobInput]

    enum CodingKeys: String, CodingKey {
        case state, inputs
        case jobId = "job_id"
    }
}

/// `POST /jobs/{id}/input` request body (API.md §4).
struct JobInputSubmit: Encodable, Equatable {
    let text: String
}

// MARK: - Error envelope (API.md §1)

struct ErrorEnvelope: Codable, Equatable {
    let detail: String
}
