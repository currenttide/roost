package oss.roost.mobile.model

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject

/**
 * Pure-Kotlin, android-free decoders for every API.md shape. org.json only.
 *
 * WHY tolerant helpers: the contract is additive-only (§7). We never call the throwing
 * getters on optional fields — every read goes through opt* helpers that coerce or return
 * null, so an absent field, a null, or an unexpected type degrades gracefully instead of
 * throwing. The one place we DO surface an exception is a structurally invalid top-level
 * document (e.g. base64 that isn't JSON), which the caller turns into a user-facing error.
 */
object Parsers {

    // ---- small tolerant accessors -------------------------------------------------

    private fun JSONObject.str(key: String): String? =
        if (isNull(key)) null else optString(key, null)

    private fun JSONObject.dbl(key: String): Double? =
        if (has(key) && !isNull(key)) optDouble(key, Double.NaN).takeIf { !it.isNaN() } else null

    private fun JSONObject.intOrNull(key: String): Int? =
        if (has(key) && !isNull(key)) {
            // optInt won't parse a numeric string; be lenient.
            when (val v = opt(key)) {
                is Number -> v.toInt()
                is String -> v.toIntOrNull()
                else -> null
            }
        } else null

    private fun JSONObject.lngOrNull(key: String): Long? =
        if (has(key) && !isNull(key)) {
            when (val v = opt(key)) {
                is Number -> v.toLong()
                is String -> v.toLongOrNull()
                else -> null
            }
        } else null

    private fun JSONObject.boolOrNull(key: String): Boolean? =
        if (has(key) && !isNull(key)) {
            when (val v = opt(key)) {
                is Boolean -> v
                is String -> v.toBooleanStrictOrNull()
                else -> null
            }
        } else null

    /** Coerce a JSON map into a plain Map<String, Any?> for resubmit specs. */
    private fun JSONObject.toMap(): Map<String, Any?> {
        val out = LinkedHashMap<String, Any?>()
        for (k in keys()) {
            out[k] = when (val v = opt(k)) {
                JSONObject.NULL -> null
                is JSONObject -> v.toMap()
                is JSONArray -> v.toList()
                else -> v
            }
        }
        return out
    }

    private fun JSONArray.toList(): List<Any?> =
        (0 until length()).map { i ->
            when (val v = opt(i)) {
                JSONObject.NULL -> null
                is JSONObject -> v.toMap()
                is JSONArray -> v.toList()
                else -> v
            }
        }

    /**
     * `result` is polymorphic: a plain String in run rows (often "") or an object
     * {output, verified, evidence} in job detail / done events. Flatten to a display
     * string; null/empty stays "".
     */
    private fun flattenResult(value: Any?): String = when (value) {
        null, JSONObject.NULL -> ""
        is String -> value
        is JSONObject -> value.str("output") ?: value.toString()
        else -> value.toString()
    }

    // ---- pairing -----------------------------------------------------------------

    /**
     * Decode the JSON inside a roost://pair?d=… payload. Caller has already base64url-
     * decoded the `d` param to this JSON string. Rejects v>1 with a clear message (§1).
     */
    fun parsePairPayload(json: String): PairPayload {
        val o = JSONObject(json) // throws on non-JSON → caller maps to "bad pairing code"
        val v = o.optInt("v", 1)
        if (v > 1) throw PairVersionException(v)
        val url = o.str("url") ?: throw JSONException("pairing payload missing url")
        val token = o.str("token") ?: throw JSONException("pairing payload missing token")
        return PairPayload(v = v, url = url.trimEnd('/'), token = token, name = o.str("name"))
    }

    class PairVersionException(val version: Int) :
        RuntimeException("This pairing code needs a newer app (v$version). Update the app.")

    fun parseHealthz(json: String): Healthz {
        val o = JSONObject(json)
        return Healthz(ok = o.boolOrNull("ok") ?: false, version = o.str("version"))
    }

    fun parseError(status: Int, body: String): ApiError {
        val detail = try {
            JSONObject(body).str("detail")
        } catch (_: JSONException) {
            null
        } ?: body.ifBlank { "HTTP $status" }
        return ApiError(status = status, detail = detail)
    }

    // ---- dashboard ---------------------------------------------------------------

    fun parseDerived(json: String): Derived {
        val o = JSONObject(json)
        val verdict = o.optJSONObject("fleet_verdict")
        return Derived(
            generatedAt = o.dbl("generated_at") ?: 0.0,
            fleetVerdict = FleetVerdict(
                level = verdict?.str("level") ?: "ok",
                summary = verdict?.str("summary") ?: "",
            ),
            workers = o.optJSONArray("workers").objs().map(::parseWorker),
            runs = o.optJSONArray("runs").objs().map(::parseRun),
        )
    }

    fun parseWorker(o: JSONObject): Worker = Worker(
        id = o.str("id") ?: "",
        name = o.str("name") ?: (o.str("id") ?: "worker"),
        status = o.str("status") ?: "offline",
        lastSeen = o.dbl("last_seen"),
    )

    fun parseWorkers(json: String): List<Worker> =
        JSONArray(json).objs().map(::parseWorker)

    fun parseRun(o: JSONObject): Run {
        val health = o.optJSONObject("health")
        val cost = o.optJSONObject("cost")
        return Run(
            runId = o.str("run_id") ?: "",
            goal = o.str("goal") ?: "(untitled)",
            kind = o.str("kind"),
            goalDisplay = o.str("goal_display"),
            state = o.str("state") ?: "queued",
            phase = o.str("phase"),
            health = Health(
                status = health?.str("status") ?: (o.str("state") ?: "queued"),
                reason = health?.str("reason"),
            ),
            worker = o.str("worker"),
            verified = o.boolOrNull("verified"),
            evidence = o.str("evidence"),
            result = flattenResult(o.opt("result")),
            narration = o.str("narration"),
            lastActivity = o.str("last_activity"),
            progress = o.intOrNull("progress"),
            etaSec = o.intOrNull("eta_sec"),
            tokensUsed = cost?.intOrNull("tokens_used") ?: 0,
            costEstUsd = cost?.dbl("cost_est_usd") ?: 0.0,
            createdAt = o.dbl("created_at") ?: 0.0,
            finishedAt = o.dbl("finished_at"),
            exitCode = o.intOrNull("exit_code"),
        )
    }

    /** Single-run story (GET /jobs/{id}/derived) — same shape as a dashboard run. */
    fun parseRunStory(json: String): Run = parseRun(JSONObject(json))

    // ---- jobs --------------------------------------------------------------------

    fun parseJob(o: JSONObject): Job {
        val spec = o.optJSONObject("spec")
        val resultObj = o.opt("result")
        val (output, verified, evidence) = when (resultObj) {
            is JSONObject -> Triple(
                resultObj.str("output"),
                resultObj.boolOrNull("verified"),
                resultObj.str("evidence"),
            )
            is String -> Triple(resultObj.ifBlank { null }, null, null)
            else -> Triple(null, null, null)
        }
        return Job(
            id = o.str("id") ?: "",
            intent = o.str("intent") ?: "",
            state = o.str("state") ?: "queued",
            workerId = o.str("worker_id"),
            exitCode = o.intOrNull("exit_code"),
            error = o.str("error"),
            resultOutput = output,
            resultVerified = verified,
            resultEvidence = evidence,
            tokensUsed = o.intOrNull("tokens_used") ?: 0,
            createdAt = o.dbl("created_at"),
            finishedAt = o.dbl("finished_at"),
            parentJobId = o.str("parent_job_id"),
            depth = o.intOrNull("depth") ?: 0,
            specKind = spec?.str("kind") ?: "claude",
            specIntent = spec?.str("intent") ?: o.str("intent"),
            specCommand = spec?.str("command"),
            specRequires = spec?.optJSONObject("requires")?.toMap()
                ?: o.optJSONObject("requires")?.toMap()
                ?: emptyMap(),
        )
    }

    fun parseJob(json: String): Job = parseJob(JSONObject(json))

    fun parseJobs(json: String): List<Job> = JSONArray(json).objs().map(::parseJob)

    fun parseTree(json: String): List<Job> = JSONArray(json).objs().map(::parseJob)

    fun parseCancel(json: String): CancelAck =
        CancelAck(cancelled = JSONObject(json).intOrNull("cancelled") ?: 0)

    // ---- follow-up input (R38, API.md §4) ----------------------------------------

    /** Ack for POST /jobs/{id}/input — `{input_id, job_id, state}`. */
    fun parseJobInputAck(json: String): JobInputAck {
        val o = JSONObject(json)
        return JobInputAck(
            inputId = o.str("input_id") ?: "",
            jobId = o.str("job_id") ?: "",
            state = o.str("state") ?: "queued",
        )
    }

    private fun parseJobInput(o: JSONObject): JobInput = JobInput(
        id = o.str("id") ?: "",
        state = o.str("state") ?: "queued",
        detail = o.str("detail"),
        createdAt = o.dbl("created_at") ?: 0.0,
        deliveredAt = o.dbl("delivered_at"),
        createdBy = o.str("created_by"),
    )

    /** GET /jobs/{id}/inputs — `{job_id, state, inputs:[…]}`. */
    fun parseJobInputs(json: String): JobInputs {
        val o = JSONObject(json)
        return JobInputs(
            jobId = o.str("job_id") ?: "",
            state = o.str("state") ?: "",
            inputs = o.optJSONArray("inputs").objs().map(::parseJobInput),
        )
    }

    // ---- logs --------------------------------------------------------------------

    fun parseLogLine(o: JSONObject): LogLine = LogLine(
        seq = o.intOrNull("seq") ?: 0,
        stream = o.str("stream") ?: "stdout",
        data = o.str("data") ?: "",
        ts = o.dbl("ts") ?: 0.0,
    )

    fun parseLogPage(json: String): LogPage {
        val o = JSONObject(json)
        return LogPage(
            jobId = o.str("job_id") ?: "",
            state = o.str("state"),
            logs = o.optJSONArray("logs").objs().map(::parseLogLine),
        )
    }

    // ---- publish (API.md §6) -------------------------------------------------------

    fun parseBlob(json: String): StagedBlob {
        val o = JSONObject(json)
        return StagedBlob(
            id = o.str("id") ?: "",
            name = o.str("name") ?: "",
            size = o.lngOrNull("size") ?: 0L,
            sha256 = o.str("sha256"),
            state = o.str("state") ?: "pending",
            createdAt = o.dbl("created_at") ?: 0.0,
            expiresAt = o.dbl("expires_at") ?: 0.0,
        )
    }

    fun parseSite(o: JSONObject): Site = Site(
        slug = o.str("slug") ?: "",
        url = o.str("url") ?: "",
        publicUrl = o.str("public_url"),
        files = o.intOrNull("files") ?: 0,
        size = o.lngOrNull("size") ?: 0L,
        createdAt = o.dbl("created_at") ?: 0.0,
        updatedAt = o.dbl("updated_at") ?: 0.0,
    )

    fun parseSite(json: String): Site = parseSite(JSONObject(json))

    fun parseSites(json: String): List<Site> = JSONArray(json).objs().map(::parseSite)

    // ---- schedules (API.md §7) -----------------------------------------------------

    fun parseSchedule(o: JSONObject): Schedule = Schedule(
        id = o.str("id") ?: "",
        name = o.str("name"),
        spec = o.optJSONObject("spec")?.toMap() ?: emptyMap(),
        intervalSec = o.dbl("interval_sec") ?: 0.0,
        enabled = o.boolOrNull("enabled") ?: false,
        nextRunAt = o.dbl("next_run_at"),
        lastRunAt = o.dbl("last_run_at"),
        lastJobId = o.str("last_job_id"),
        createdAt = o.dbl("created_at") ?: 0.0,
    )

    fun parseSchedule(json: String): Schedule = parseSchedule(JSONObject(json))

    fun parseSchedules(json: String): List<Schedule> =
        JSONArray(json).objs().map(::parseSchedule)

    // ---- SSE event payloads (data: line already extracted) -----------------------

    /**
     * Parse one SSE frame given its event name and data JSON. Returns null for frames we
     * intentionally ignore (comments, retry hints, unparseable data) so the stream loop
     * can keep going (§5).
     */
    fun parseStreamFrame(event: String, data: String): StreamEvent? = try {
        when (event) {
            "state" -> StreamEvent.State(JSONObject(data).str("state") ?: "")
            "log" -> StreamEvent.Log(parseLogLine(JSONObject(data)))
            "done" -> {
                val o = JSONObject(data)
                StreamEvent.Done(
                    state = o.str("state") ?: "",
                    exitCode = o.intOrNull("exit_code"),
                    error = o.str("error"),
                    resultOutput = flattenResult(o.opt("result")).ifBlank { null },
                    tokensUsed = o.intOrNull("tokens_used"),
                )
            }
            "error" -> StreamEvent.Err(JSONObject(data).str("error") ?: "stream error")
            else -> null
        }
    } catch (_: JSONException) {
        null
    }

    // ---- helpers -----------------------------------------------------------------

    private fun JSONArray?.objs(): List<JSONObject> {
        if (this == null) return emptyList()
        val out = ArrayList<JSONObject>(length())
        for (i in 0 until length()) (opt(i) as? JSONObject)?.let(out::add)
        return out
    }
}
