package oss.roost.mobile.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import oss.roost.mobile.model.CancelAck
import oss.roost.mobile.model.Derived
import oss.roost.mobile.model.Healthz
import oss.roost.mobile.model.Job
import oss.roost.mobile.model.JobInputAck
import oss.roost.mobile.model.JobInputs
import oss.roost.mobile.model.LogPage
import oss.roost.mobile.model.Parsers
import oss.roost.mobile.model.Run
import oss.roost.mobile.model.Schedule
import oss.roost.mobile.model.Site
import oss.roost.mobile.model.StagedBlob
import oss.roost.mobile.model.Worker
import org.json.JSONObject
import java.io.BufferedReader
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * The single networking surface. Every method is a suspend function that runs the blocking
 * HttpURLConnection call on Dispatchers.IO (API.md transport: plain HTTP to the CP URL).
 *
 * WHY HttpURLConnection and not OkHttp/Retrofit: the weight budget (DESIGN §7) — zero
 * third-party runtime deps. HUC + org.json + coroutines cover the whole contract.
 *
 * Auth/error model (API.md §1): every request carries Bearer <token>. A 401 throws
 * ApiException(401) which the app turns into "unpair"; 403 throws ApiException(403) which
 * stays paired and shows the detail. The SSE stream is handled separately (SseClient).
 */
class ApiClient(
    @Volatile var baseUrl: String,
    @Volatile var token: String?,
) {
    /** Thrown for any non-2xx with the parsed `{detail}` envelope. */
    class ApiException(val status: Int, val detail: String) : IOException("HTTP $status: $detail")

    // ---- public endpoints --------------------------------------------------------

    /** Unauthenticated reachability probe — used before accepting a pairing. */
    suspend fun healthz(): Healthz =
        Parsers.parseHealthz(getText("/healthz", auth = false))

    /** Raw body so the caller can also feed the offline cache (one fetch, one parse). */
    suspend fun derivedRaw(limit: Int = 40): String = getText("/derived?limit=$limit")

    suspend fun derived(limit: Int = 40): Derived =
        Parsers.parseDerived(derivedRaw(limit))

    suspend fun workers(): List<Worker> =
        Parsers.parseWorkers(getText("/workers"))

    suspend fun job(id: String): Job =
        Parsers.parseJob(getText("/jobs/${enc(id)}"))

    suspend fun jobStory(id: String): Run =
        Parsers.parseRunStory(getText("/jobs/${enc(id)}/derived"))

    suspend fun logs(id: String, since: Int, limit: Int = 1000): LogPage =
        Parsers.parseLogPage(getText("/jobs/${enc(id)}/logs?since=$since&limit=$limit"))

    suspend fun tree(id: String): List<Job> =
        Parsers.parseTree(getText("/jobs/${enc(id)}/tree"))

    suspend fun cancel(id: String, cascade: Boolean = false): CancelAck =
        Parsers.parseCancel(
            requestText("DELETE", "/jobs/${enc(id)}${if (cascade) "?tree=true" else ""}", null)
        )

    /**
     * Queue a follow-up message for a RUNNING job (R38, API.md §4). The CP returns
     * `{input_id, job_id, state:"queued"}`; the owning worker delivers it on its
     * heartbeat. A terminal job is rejected 409, empty text 400, >64 KiB 413 — all
     * surfaced as [ApiException] with the server detail.
     */
    suspend fun sendInput(id: String, text: String): JobInputAck =
        Parsers.parseJobInputAck(
            requestText("POST", "/jobs/${enc(id)}/input",
                JSONObject().put("text", text).toString()))

    /** Poll a job's follow-up queue for delivery outcome (API.md §4). */
    suspend fun inputs(id: String): JobInputs =
        Parsers.parseJobInputs(getText("/jobs/${enc(id)}/inputs"))

    /**
     * Submit a job (API.md §3). Sends ONLY the contract fields. `requires` is {} for
     * auto-place or {"worker": id} to pin. Returns the full job object; caller navigates
     * to job.id immediately.
     */
    suspend fun submit(
        intent: String,
        kind: String,            // "claude" | "command"
        pinWorker: String?,
        command: String?,
    ): Job {
        val body = JSONObject()
        body.put("kind", kind)
        val requires = JSONObject()
        if (pinWorker != null) requires.put("worker", pinWorker)
        body.put("requires", requires)
        if (kind == "command") {
            body.put("command", command ?: intent)
        } else {
            body.put("intent", intent)
            // can_dispatch makes the worker inject the roost MCP, so the agent
            // can SEE the fleet ("how many machines?") and spawn sub-jobs under
            // the existing depth/tree-budget guardrails. Without it the agent
            // is fleet-blind (API.md §3).
            body.put("hierarchy", JSONObject().put("can_dispatch", true))
        }
        return Parsers.parseJob(requestText("POST", "/jobs", body.toString()))
    }

    // ---- publish (API.md §6) -------------------------------------------------------

    /** Stage a site bundle (raw tar.gz bytes) — publish step 1. */
    suspend fun uploadBlob(name: String, bytes: ByteArray): StagedBlob =
        withContext(Dispatchers.IO) {
            val conn = open("/blobs?name=${enc(name)}")
            try {
                conn.requestMethod = "POST"
                conn.connectTimeout = 10_000
                conn.readTimeout = 60_000   // bundles are bigger than JSON bodies
                token?.let { conn.setRequestProperty("Authorization", "Bearer $it") }
                conn.setRequestProperty("Content-Type", "application/octet-stream")
                conn.doOutput = true
                conn.outputStream.use { it.write(bytes) }
                val code = conn.responseCode
                if (code in 200..299) {
                    Parsers.parseBlob(readBody(conn) ?: "")
                } else {
                    val err = readError(conn) ?: ""
                    throw Parsers.parseError(code, err)
                        .let { ApiException(it.status, it.detail) }
                }
            } finally {
                conn.disconnect()
            }
        }

    /**
     * Publish a staged bundle — step 2. `name` optional (server defaults it to the
     * blob name minus its tar suffix, then slugifies).
     */
    suspend fun publish(blobId: String, name: String? = null): Site {
        val body = JSONObject().put("blob_id", blobId)
        if (name != null) body.put("name", name)
        return Parsers.parseSite(requestText("POST", "/publish", body.toString()))
    }

    /**
     * One-shot publish (API.md §6a): the tar.gz IS the body, no staged blob.
     * `name` is required (no blob name to default from) and slugified server-side.
     * Returns the same [Site] as the two-step [publish]. A non-JSON Content-Type
     * selects the one-shot path server-side.
     */
    suspend fun publishBundle(name: String, bytes: ByteArray): Site =
        withContext(Dispatchers.IO) {
            val conn = open("/publish?name=${enc(name)}")
            try {
                conn.requestMethod = "POST"
                conn.connectTimeout = 10_000
                conn.readTimeout = 60_000   // bundles are bigger than JSON bodies
                token?.let { conn.setRequestProperty("Authorization", "Bearer $it") }
                conn.setRequestProperty("Content-Type", "application/gzip")
                conn.doOutput = true
                conn.outputStream.use { it.write(bytes) }
                val code = conn.responseCode
                if (code in 200..299) {
                    Parsers.parseSite(readBody(conn) ?: "")
                } else {
                    val err = readError(conn) ?: ""
                    throw Parsers.parseError(code, err)
                        .let { ApiException(it.status, it.detail) }
                }
            } finally {
                conn.disconnect()
            }
        }

    suspend fun sites(): List<Site> = Parsers.parseSites(getText("/publish"))

    // ---- schedules (API.md §7) -----------------------------------------------------

    /** List interval schedules, newest first. */
    suspend fun schedules(): List<Schedule> =
        Parsers.parseSchedules(getText("/schedules"))

    /**
     * Create an interval schedule (API.md §7a). `every` is seconds or "<N>[smhd]"
     * (e.g. "30m"; 30 s floor server-side). `spec` follows the §3 submit shape
     * (root jobs only). Returns the new [Schedule]; first run is one interval out.
     */
    suspend fun createSchedule(
        spec: Map<String, Any?>,
        every: String,
        name: String? = null,
        enabled: Boolean = true,
    ): Schedule {
        val body = JSONObject()
        body.put("spec", JSONObject(spec))
        body.put("every", every)
        body.put("enabled", enabled)
        if (name != null) body.put("name", name)
        return Parsers.parseSchedule(requestText("POST", "/schedules", body.toString()))
    }

    /**
     * Enable/disable a schedule (API.md §7c). Re-enabling restarts the clock
     * server-side. Returns the updated [Schedule].
     */
    suspend fun setScheduleEnabled(id: String, enabled: Boolean): Schedule {
        val body = JSONObject().put("enabled", enabled)
        return Parsers.parseSchedule(
            requestText("PATCH", "/schedules/${enc(id)}", body.toString()))
    }

    /** Delete a schedule (API.md §7d). 404 if unknown → ApiException(404). */
    suspend fun deleteSchedule(id: String): Boolean {
        val json = requestText("DELETE", "/schedules/${enc(id)}", null)
        return JSONObject(json).optBoolean("deleted", false)
    }

    // ---- HTTP plumbing -----------------------------------------------------------

    private suspend fun getText(path: String, auth: Boolean = true): String =
        requestText("GET", path, null, auth)

    /**
     * Run one request and return the body. Throws ApiException on non-2xx with the parsed
     * detail. All blocking I/O is confined to Dispatchers.IO.
     */
    private suspend fun requestText(
        method: String,
        path: String,
        body: String?,
        auth: Boolean = true,
    ): String = withContext(Dispatchers.IO) {
        val conn = open(path)
        try {
            conn.requestMethod = method
            conn.connectTimeout = 10_000
            conn.readTimeout = 20_000
            if (auth) token?.let { conn.setRequestProperty("Authorization", "Bearer $it") }
            conn.setRequestProperty("Accept", "application/json")
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            }
            val code = conn.responseCode
            if (code in 200..299) {
                readBody(conn) ?: ""
            } else {
                val err = readError(conn) ?: ""
                throw Parsers.parseError(code, err).let { ApiException(it.status, it.detail) }
            }
        } finally {
            conn.disconnect()
        }
    }

    /** Expose a configured connection for the SSE client without duplicating header logic. */
    fun openAuthed(path: String): HttpURLConnection {
        val conn = open(path)
        token?.let { conn.setRequestProperty("Authorization", "Bearer $it") }
        return conn
    }

    private fun open(path: String): HttpURLConnection {
        val full = if (path.startsWith("http")) path else baseUrl.trimEnd('/') + path
        return (URL(full).openConnection() as HttpURLConnection)
    }

    private fun readBody(conn: HttpURLConnection): String? =
        conn.inputStream?.bufferedReader()?.use(BufferedReader::readText)

    private fun readError(conn: HttpURLConnection): String? =
        conn.errorStream?.bufferedReader()?.use(BufferedReader::readText)

    private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")
}
