package oss.roost.mobile.model

import org.json.JSONException
import org.json.JSONObject

/**
 * Push-notification client logic for the R37 / DESIGN.md §6 (v1.1) terminal-state
 * notifier. Two PURE pieces, android-free so the JVM harness covers them:
 *
 *  1. [NtfyTopic] — derive/validate the ntfy subscribe URL the app watches. The
 *     control plane is configured with `--notify-url` (an ntfy topic) and POSTs
 *     there on terminal jobs; it does NOT expose that topic over the API, so the
 *     app takes it as a SETTING (manual entry per DESIGN.md §6 — "ntfy.sh
 *     self-hosted or UnifiedPush-style webhooks"). The user pastes a full
 *     `https://ntfy.sh/<topic>` URL or just the bare topic name.
 *  2. [NotifyRouter] — map an incoming R37 payload to an in-app destination (the
 *     job's Session screen, or a safe Dashboard fallback for a malformed payload).
 *     This is the deep-link routing a tapped notification triggers.
 *
 * The DEVICE-ONLY half — binding a UnifiedPush distributor, holding the
 * subscription, and rendering the system notification — lives in the (untested
 * here) `push/` Android code, which calls THIS router to decide where a tap goes.
 */

/** ntfy topic settings: parse + canonicalize the subscribe URL (API parity with iOS NtfyTopic). */
object NtfyTopic {
    /** Default server when the user types only a bare topic name. */
    const val DEFAULT_HOST = "https://ntfy.sh"

    /** ntfy's topic grammar (matches the server's `[-_A-Za-z0-9]{1,64}`). */
    private val TOPIC = Regex("^[-_A-Za-z0-9]{1,64}$")

    /** True iff [s], taken as a bare topic name, is a legal ntfy topic. */
    fun isValidTopic(s: String): Boolean = TOPIC.matches(s.trim())

    /**
     * Canonical subscribe URL for whatever the user typed, or null if it can't be
     * made into one. Accepts a bare topic (→ ntfy.sh), `host/topic` (scheme
     * defaulted to https), or a full http(s) URL; trailing slash + `?query` are
     * dropped and the first path segment is validated as the topic.
     */
    fun normalize(input: String): String? {
        val raw = input.trim()
        if (raw.isEmpty()) return null

        // Bare topic (no slash): default to ntfy.sh.
        if (!raw.contains('/')) return if (isValidTopic(raw)) "$DEFAULT_HOST/$raw" else null

        // URL or host/topic. Default the scheme so we can split host vs path.
        val withScheme = if (raw.contains("://")) raw else "https://$raw"
        val scheme = withScheme.substringBefore("://").lowercase()
        if (scheme != "http" && scheme != "https") return null
        val afterScheme = withScheme.substringAfter("://")
        if (afterScheme.isEmpty()) return null

        val authority = afterScheme.substringBefore('/')
        if (authority.isEmpty()) return null
        // Strip a query/fragment if it rode on the path.
        val path = afterScheme.substringAfter('/', "").substringBefore('?').substringBefore('#')
        val topic = path.split('/').firstOrNull { it.isNotEmpty() } ?: return null
        if (!isValidTopic(topic)) return null
        return "$scheme://$authority/$topic"
    }

    /** The bare topic name from a normalized URL (last path segment), for display. */
    fun displayTopic(normalizedUrl: String): String? =
        normalizedUrl.substringAfter("://", "")
            .substringAfter('/', "")
            .split('/').lastOrNull { it.isNotEmpty() }
}

/**
 * Where a tapped notification should land. [Dashboard] is the safe fallback when
 * the payload is missing/garbled — we never crash or guess a job id.
 */
sealed interface NotifyRoute {
    data class Session(val jobId: String) : NotifyRoute
    data object Dashboard : NotifyRoute
}

/**
 * The R37 terminal-state payload the CP emits (see `roost/server.py`
 * `_build_notification` and `tests/test_notify.py`). We keep ONLY the fields the
 * app routes/renders on; per API.md's additive-only rule unknown fields are
 * ignored. Every field except the discriminator is nullable so a partial/future
 * payload still parses to a safe route.
 */
data class NotifyPayload(
    val event: String?,
    val jobId: String?,
    val state: String?,
    val intent: String?,
    val durationSec: Double?,
    val exitCode: Int?,
    val workerId: String?,
    val message: String?,
)

object NotifyRouter {

    /**
     * Parse the JSON body of an R37 notification. Returns null on non-JSON or a
     * shape that isn't a JSON object — the caller routes to [NotifyRoute.Dashboard].
     */
    fun decode(json: String): NotifyPayload? = try {
        val o = JSONObject(json)
        NotifyPayload(
            event = o.strOrNull("event"),
            jobId = o.strOrNull("job_id"),
            state = o.strOrNull("state"),
            intent = o.strOrNull("intent"),
            durationSec = o.dblOrNull("duration_sec"),
            exitCode = o.intOrNull("exit_code"),
            workerId = o.strOrNull("worker_id"),
            message = o.strOrNull("message"),
        )
    } catch (_: JSONException) {
        null
    }

    /**
     * Route a parsed payload: a non-blank `job_id` opens that Session screen;
     * anything else (null payload, blank/missing job id) falls back to the
     * Dashboard. Deep-linking to a specific job is the whole point of the push.
     */
    fun route(payload: NotifyPayload?): NotifyRoute {
        val id = payload?.jobId?.trim()
        return if (!id.isNullOrEmpty()) NotifyRoute.Session(id) else NotifyRoute.Dashboard
    }

    /** Convenience: raw JSON body → route, in one step. */
    fun route(json: String): NotifyRoute = route(decode(json))

    // --- tolerant accessors (same posture as Parsers.kt) ---

    private fun JSONObject.strOrNull(key: String): String? =
        if (isNull(key)) null else optString(key, null)

    private fun JSONObject.dblOrNull(key: String): Double? =
        if (has(key) && !isNull(key)) optDouble(key, Double.NaN).takeIf { !it.isNaN() } else null

    private fun JSONObject.intOrNull(key: String): Int? =
        if (has(key) && !isNull(key)) when (val v = opt(key)) {
            is Number -> v.toInt()
            is String -> v.toIntOrNull()
            else -> null
        } else null
}
