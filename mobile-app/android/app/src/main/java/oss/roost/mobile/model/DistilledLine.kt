package oss.roost.mobile.model

import org.json.JSONArray
import org.json.JSONObject

/**
 * Distilled live-stream transform (R109) — a PURE Kotlin mirror of the CLI
 * reference `roost.cli.distill_log_line`. The language-neutral contract lives at
 * `mobile-app/fixtures/distilled/SPEC.md`; the golden fixtures
 * `mobile-app/fixtures/distilled/cases.json` pin it (see `DistilledFixtureTest`).
 *
 * Each call takes ONE raw `log` SSE `data` line (one Anthropic stream-json
 * envelope, or one line of a `command` job's plain stdout) and returns a single
 * readable line, or `null` to SUPPRESS the line. Never throws on odd input —
 * when in doubt, pass the line through verbatim so nothing is lost.
 *
 * This is android-free (only `org.json`, a platform class) so it runs on the
 * bare kotlinc + JUnit Linux harness; iOS (R108) mirrors the same fixtures.
 */
object DistilledLine {

    /** Tool-input keys, in priority order, used to summarise a tool_use call. */
    private val TOOL_HINT_KEYS = listOf(
        "command", "file_path", "path", "pattern", "query", "url", "description",
        "prompt", "intent",
    )
    /** Max characters for a one-line tool-use summary / truncated tool result. */
    private const val HINT_MAX = 80
    private const val RESULT_MAX = 200

    /**
     * Distil ONE raw `data` line into a readable line, or `null` to suppress it.
     *
     * Non-stream-json input (a `command` job's plain stdout, or a roost-internal
     * JSON `event` envelope) passes through verbatim. Recognised Anthropic
     * stream-json envelopes are distilled per SPEC.md; base64 signatures,
     * reasoning blobs, and oversized tool-result bodies are suppressed/truncated.
     */
    fun from(data: String?): String? {
        if (data == null) return null
        val text = data.trim()
        // Rule 1: not JSON → passthrough verbatim (preserve the original, untrimmed).
        if (text.isEmpty() || !text.startsWith("{")) return data
        val obj: JSONObject = try {
            JSONObject(text)
        } catch (_: Exception) {
            return data // looked like JSON but isn't — passthrough verbatim.
        }
        val mtype = obj.optString("type", "")
        // Rule 3: recognised Anthropic stream-json envelopes distil by `type`.
        when (mtype) {
            "system" -> return if (obj.optString("subtype", "") == "init") "🔎 starting…" else null
            "rate_limit_event" -> return null
            "result" -> return if (obj.optBoolean("is_error", false)) "✗ failed" else "✓ done"
            "assistant", "user" -> return distillMessage(obj)
        }
        // Rule 2: JSON object without a recognised stream-json type (e.g. roost's
        // own `{"type":"started",...}` event envelope) → passthrough verbatim.
        return data
    }

    private fun distillMessage(obj: JSONObject): String? {
        val msg = obj.optJSONObject("message") ?: return null
        // `message.content` is either a string or a list of content blocks.
        val contentStr = msg.opt("content")
        if (contentStr is String) {
            val flat = firstLine(contentStr, RESULT_MAX)
            return flat.ifEmpty { null }
        }
        val list = msg.optJSONArray("content") ?: return null
        val out = ArrayList<String>()
        for (i in 0 until list.length()) {
            val item = list.optJSONObject(i) ?: continue
            when (item.optString("type", "")) {
                "text" -> {
                    val flat = firstLine(item.optString("text", ""), RESULT_MAX)
                    if (flat.isNotEmpty()) out.add(flat)
                }
                "tool_use" -> out.add(distillToolUse(item))
                "tool_result" -> out.add(distillToolResult(item))
                // thinking / redacted_thinking (signature blob) → suppressed.
                // any other block type → ignored.
            }
        }
        return if (out.isEmpty()) null else out.joinToString("\n")
    }

    private fun distillToolUse(item: JSONObject): String {
        val name = item.optString("name", "").ifEmpty { "tool" }
        val inp = item.optJSONObject("input")
        var hint = ""
        if (inp != null) {
            for (k in TOOL_HINT_KEYS) {
                if (!inp.has(k)) continue
                val v = inp.opt(k)
                if (isTruthy(v)) {
                    hint = firstLine(stringify(v), HINT_MAX)
                    break
                }
            }
        }
        return if (hint.isNotEmpty()) "→ $name: $hint" else "→ $name"
    }

    private fun distillToolResult(item: JSONObject): String {
        val content = item.opt("content")
        var resultText = ""
        when (content) {
            is String -> resultText = content
            is JSONArray -> for (i in 0 until content.length()) {
                val blk = content.opt(i)
                if (blk is JSONObject) {
                    if (blk.optString("type", "") == "text" && blk.optString("text", "").isNotEmpty()) {
                        resultText = blk.optString("text", "")
                        break
                    }
                } else if (blk is String) {
                    resultText = blk
                    break
                }
            }
        }
        val summary = if (resultText.isNotEmpty()) firstLine(resultText, RESULT_MAX) else "(result)"
        return if (item.optBoolean("is_error", false)) "  ⎿ ✗ $summary" else "  ⎿ $summary"
    }

    /** First line of `text`, whitespace-collapsed to a single line, capped at `limit`. */
    private fun firstLine(text: String, limit: Int): String {
        // Split on ANY whitespace and rejoin with single spaces (flattens multi-line).
        val flat = text.split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
        return if (flat.length > limit) flat.substring(0, limit) + "…" else flat
    }

    /** Mirrors Python truthiness for the hint-key check (`if v:`). */
    private fun isTruthy(v: Any?): Boolean = when (v) {
        null, JSONObject.NULL -> false
        is String -> v.isNotEmpty()
        is Boolean -> v
        is Number -> v.toDouble() != 0.0
        is JSONArray -> v.length() > 0
        is JSONObject -> v.length() > 0
        else -> true
    }

    /** Mirrors Python `str(v)` for a hint value (the CLI calls `_first_line(v, …)`). */
    private fun stringify(v: Any?): String = when (v) {
        null, JSONObject.NULL -> ""
        is String -> v
        else -> v.toString()
    }
}
