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
    /** Best one-liner for a row's subtitle. */
    val bestLine: String?
        get() = narration?.takeIf { it.isNotBlank() }
            ?: lastActivity?.takeIf { it.isNotBlank() }
            ?: result.takeIf { it.isNotBlank() }

    val isActive: Boolean
        get() = state == "running" || state == "assigned"
}

data class Health(
    val status: String,
    val reason: String?,
)

/** Worker row (workers.json / derived.workers). */
data class Worker(
    val id: String,
    val name: String,
    val status: String,      // idle | busy | offline | (unknown)
    val lastSeen: Double?,
) {
    /** idle+busy count as live for the "N nodes" chip (API.md §2). */
    val isLive: Boolean get() = status == "idle" || status == "busy"
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
