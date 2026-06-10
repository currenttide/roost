package oss.roost.mobile.model

/**
 * Plain data models for the Roost Mobile API (see mobile-app/API.md).
 *
 * Design notes (WHY):
 *  - These are tolerant projections, NOT 1:1 mirrors of the server. The contract is
 *    additive-only (§7): we keep only the fields the UI renders and ignore everything
 *    else, so a server that adds fields never breaks decode.
 *  - Enums are modeled as Strings + a sealed mapping (HealthGlyph) so an UNKNOWN enum
 *    value renders as plain text instead of crashing (§2).
 *  - No android.* imports anywhere in this package or in Parsers.kt — that is what lets
 *    the JVM unit tests exercise the real parsing code without Robolectric.
 */

/** Pairing payload decoded from roost://pair?d=<base64url> (API.md §1). */
data class PairPayload(
    val v: Int,
    val url: String,
    val token: String,
    val name: String?,
)

/** GET /healthz probe result. */
data class Healthz(
    val ok: Boolean,
    val version: String?,
)

/** The error envelope: {"detail": "..."} (API.md §1). */
data class ApiError(
    val status: Int,
    val detail: String,
)

/** GET /derived dashboard payload (API.md §2). */
data class Derived(
    val generatedAt: Double,
    val fleetVerdict: FleetVerdict,
    val workers: List<Worker>,
    val runs: List<Run>,
)

data class FleetVerdict(
    val level: String,   // "ok" | "alert" | (unknown → treat as alert-ish, render summary)
    val summary: String,
) {
    val isOk: Boolean get() = level.equals("ok", ignoreCase = true)
}

/** A run row (API.md §2). Many fields are informational; we keep what we render. */
data class Run(
    val runId: String,
    val goal: String,
    /** Effective executor kind (API.md §2: "command"|"claude"|"docker"|…). Null on
     *  an older CP that doesn't send it — clients then drop the kind subtitle segment
     *  rather than guess (the R85 bug was guessing "claude" for every job). */
    val kind: String?,
    /** R86: server-summarized glanceable goal for the verdict bar (collapses a
     *  raw `command`'s shell text). Null against older control planes — read via
     *  [displayGoal], which falls back to [goal]. */
    val goalDisplay: String?,
    val state: String,
    val phase: String?,
    val health: Health,
    val worker: String?,
    val verified: Boolean?,
    val evidence: String?,
    val result: String,        // already flattened to a display string by the parser
    val narration: String?,
    val lastActivity: String?,
    val progress: Int?,
    val etaSec: Int?,
    val tokensUsed: Int,
    val costEstUsd: Double,
    val createdAt: Double,
    val finishedAt: Double?,
    val exitCode: Int?,         // pulled from result/detail when present, else null
) {
    /** R86: glanceable goal for a row — prefers the server's summarized
     *  [goalDisplay], falling back to the full [goal] against an older CP. */
    val displayGoal: String
        get() = goalDisplay?.takeIf { it.isNotBlank() } ?: goal

    /** Best one-liner for a row's subtitle.
     *
     *  R122: a FAILED row's best line can be a raw stream-json wall (a worker
     *  may report the final `result`/`assistant` envelope verbatim as the
     *  failure result — the UAT "failed-agent rows render raw JSON" finding),
     *  so failed rows distil it through the shared SPEC.md transform +
     *  truncation rules ([DistilledLine.failureLine]). Non-failed rows are
     *  unchanged; null when distillation suppresses everything (the subtitle
     *  still shows the state). */
    val bestLine: String?
        get() {
            val best = narration?.takeIf { it.isNotBlank() }
                ?: lastActivity?.takeIf { it.isNotBlank() }
                ?: result.takeIf { it.isNotBlank() }
            if (state != "failed" || best == null) return best
            return DistilledLine.failureLine(best)
        }

    val isActive: Boolean
        get() = state == "running" || state == "assigned"
}

data class Health(
    val status: String,
    val reason: String?,
)

/** Worker row (workers.json / derived.workers / GET /workers, API.md §2/§2a). */
data class Worker(
    val id: String,
    val name: String,
    val status: String,      // idle | busy | stale | offline | (unknown)
    val lastSeen: Double?,
    /** In-flight jobs — render "running/capacity" (API.md §2a). */
    val running: Int? = null,
    /** Concurrency slots (>= 1); older CPs may omit it. */
    val capacity: Int? = null,
    /** Free-form capability map (API.md §2a) — heterogeneous values, carried
     *  raw and summarized for the Fleet screen by [Fleet.capsSummary]. */
    val capabilities: Map<String, Any?> = emptyMap(),
) {
    /** idle+busy count as live for the "N nodes" chip (API.md §2). */
    val isLive: Boolean get() = status == "idle" || status == "busy"

    /** Best display name (the server may register a worker without one). */
    val displayName: String get() = name.ifBlank { id }
}

/**
 * A job object (GET /jobs/{id}, POST /jobs response, tree rows). We keep the subset the
 * session header + result card need. `result`/`error`/`exitCode`/`tokensUsed` cover the
 * terminal result card; `spec*` fields let Retry resubmit (API.md §3/§4).
 */
data class Job(
    val id: String,
    val intent: String,
    val state: String,
    val workerId: String?,
    val exitCode: Int?,
    val error: String?,
    val resultOutput: String?,   // result.output (or result-as-string) for the card
    val resultVerified: Boolean?,
    val resultEvidence: String?,
    val tokensUsed: Int,
    val createdAt: Double?,
    val finishedAt: Double?,
    val parentJobId: String?,
    val depth: Int,
    // --- resubmit spec (Retry / follow-up) ---
    val specKind: String,        // "claude" | "command"
    val specIntent: String?,
    val specCommand: String?,
    val specRequires: Map<String, Any?>,
) {
    val isTerminal: Boolean
        get() = state == "succeeded" || state == "failed" || state == "cancelled"
}

/** One streamed/paged log line (API.md §4/§5). */
data class LogLine(
    val seq: Int,
    val stream: String,   // stdout | stderr | event
    val data: String,
    val ts: Double,
)

/** GET /jobs/{id}/logs page. */
data class LogPage(
    val jobId: String,
    val state: String?,
    val logs: List<LogLine>,
)

/** Parsed SSE frame from GET /jobs/{id}/stream (API.md §5). */
sealed interface StreamEvent {
    data class State(val state: String) : StreamEvent
    data class Log(val line: LogLine) : StreamEvent
    data class Done(
        val state: String,
        val exitCode: Int?,
        val error: String?,
        val resultOutput: String?,
        val tokensUsed: Int?,
    ) : StreamEvent
    data class Err(val error: String) : StreamEvent
}

/** Response to POST /jobs minting a pair token preview etc. (not all used by UI). */
data class CancelAck(val cancelled: Int)

/** Staged bundle — POST /blobs response (publish step 1, API.md §6). */
data class StagedBlob(
    val id: String,
    val name: String,
    val size: Long,
    val sha256: String?,
    val state: String,        // "ready" once the body landed
    val createdAt: Double,
    val expiresAt: Double,    // blob TTL, NOT site TTL (API.md §6)
) {
    val isReady: Boolean get() = state == "ready"
}

/** A published site — POST /publish / GET /publish rows (API.md §6). */
data class Site(
    val slug: String,
    val url: String,          // LAN URL, always present
    val publicUrl: String?,   // only when the CP has a publish domain
    val files: Int,
    val size: Long,
    val createdAt: Double,
    val updatedAt: Double,
) {
    /** Best link to offer the user: internet-facing when available. */
    val shareUrl: String get() = publicUrl ?: url
}

/**
 * An interval schedule — POST /schedules / GET /schedules rows (API.md §7). The
 * CP re-submits `spec` every `intervalSec`; the UI renders the clock and toggles
 * `enabled`. `spec` is carried as a free-form map (the §3 submit shape).
 */
data class Schedule(
    val id: String,
    val name: String?,
    val spec: Map<String, Any?>,
    val intervalSec: Double,
    val enabled: Boolean,
    val nextRunAt: Double?,
    val lastRunAt: Double?,
    val lastJobId: String?,
    val createdAt: Double,
) {
    /** Kind of the stored job spec, for a "next: <kind> job" subtitle. */
    val specKind: String? get() = spec["kind"] as? String

    /**
     * One-liner describing what this schedule runs, for the list row. Prefers the
     * agent `intent`, then a `command`, then a kind/label fallback so a future
     * spec shape still renders something (never blank). Mirrors iOS
     * `Schedule.taskSummary`.
     */
    val taskSummary: String
        get() {
            (spec["intent"] as? String)?.takeIf { it.isNotEmpty() }?.let { return it }
            (spec["command"] as? String)?.takeIf { it.isNotEmpty() }?.let { return it }
            return name ?: specKind?.let { "$it job" } ?: "scheduled job"
        }
}

/**
 * Ack for POST /jobs/{id}/input (follow-up steering, R38 / API.md §4). The state
 * here is always `queued`; the worker's later delivery shows up via [JobInput].
 */
data class JobInputAck(
    val inputId: String,
    val jobId: String,
    val state: String,
)

/**
 * One queued/delivered/dropped follow-up input — a row of GET /jobs/{id}/inputs
 * (API.md §4). `detail` carries the drop reason when `state == "dropped"` (e.g.
 * agent/docker jobs run with stdin closed, so their input is honestly dropped).
 */
data class JobInput(
    val id: String,
    val state: String,        // "queued" | "delivered" | "dropped"
    val detail: String?,
    val createdAt: Double,
    val deliveredAt: Double?,
    val createdBy: String?,
) {
    val isDelivered: Boolean get() = state == "delivered"
    val isDropped: Boolean get() = state == "dropped"
    val isQueued: Boolean get() = state == "queued"
}

/** GET /jobs/{id}/inputs response (API.md §4): the job's follow-up queue. */
data class JobInputs(
    val jobId: String,
    val state: String,        // the JOB's state
    val inputs: List<JobInput>,
)
