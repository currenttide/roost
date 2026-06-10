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
            "result" -> return if (isTruthy(obj.opt("is_error"))) "✗ failed" else "✓ done"
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
                    // Render only a STRING `text` (real stream-json always is); a
                    // null/non-string `text` → empty → block suppressed, so the
                    // three clients agree instead of leaking a coercion like "123".
                    val t = item.opt("text")
                    val flat = if (t is String) firstLine(t, RESULT_MAX) else ""
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
        val nameVal = item.opt("name")
        val name = if (nameVal is String && nameVal.isNotEmpty()) nameVal else "tool"
        val inp = item.optJSONObject("input")
        var hint = ""
        if (inp != null) {
            for (k in TOOL_HINT_KEYS) {
                val v = inp.opt(k)
                // Render only a non-empty STRING hint, so the three clients stay
                // byte-identical: a number/bool/list/object hint coerces
                // differently per language, so such values are skipped and the
                // scan continues to the next key (SPEC.md rule 4).
                if (v is String && v.isNotEmpty()) {
                    hint = firstLine(v, HINT_MAX)
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
                    // Render only a non-empty STRING `text` (org.json optString would
                    // coerce e.g. 123 -> "123"; Python/iOS require a string). R113.
                    val t = blk.opt("text")
                    if (blk.optString("type", "") == "text" && t is String && t.isNotEmpty()) {
                        resultText = t
                        break
                    }
                } else if (blk is String && blk.isNotEmpty()) {
                    resultText = blk
                    break
                }
            }
        }
        val summary = if (resultText.isNotEmpty()) firstLine(resultText, RESULT_MAX) else "(result)"
        // JSON truthiness (not optBoolean, which only parses true/false/0/1) so a
        // truthy non-bool is_error (e.g. 1, "yes") marks the error — matches the
        // CLI/iOS (SPEC.md truthiness note). optBoolean was the cross-platform
        // outlier here: it returned false for is_error: 1 / "yes".
        return if (isTruthy(item.opt("is_error"))) "  ⎿ ✗ $summary" else "  ⎿ $summary"
    }

    // ---- Failure-result rendering (R122) ----

    /**
     * Distil a FAILED job's result/error text for the failure panes — the
     * dashboard row line (via [failureLine]) and the session result card. UAT
     * found failed-agent rows rendering raw stream-json walls: a worker can
     * report a failure whose `result.output`/`error` IS one or more raw
     * Anthropic stream-json lines (e.g. the final `result` envelope), which the
     * phones previously displayed verbatim.
     *
     * This REUSES the SPEC.md contract unchanged (no new transform branch):
     * each line of `text` goes through [from] (SPEC rules 1–4) — recognised
     * stream-json envelopes distil, noise suppresses — and a line that passed
     * through VERBATIM (not stream-json) is whitespace-collapsed and capped at
     * RESULT_MAX per SPEC rule 5, so a one-line non-JSON wall can't render
     * either. Already-distilled lines are kept as-is (within the SPEC caps by
     * construction; re-truncating would drift from the transcript rendering).
     * Returns null for null/blank input or when every line is suppressed —
     * callers then fall back to their state/health line.
     *
     * Cross-platform: iOS mirrors this exactly (`Net/Distill.swift
     * failureSummary`); both platforms pin the same parity cases in their
     * Linux-harness tests.
     */
    fun failureSummary(text: String?): String? {
        if (text == null || text.isBlank()) return null
        val out = ArrayList<String>()
        for (line in text.split("\n")) {
            val d = from(line) ?: continue   // suppressed noise
            if (d == line) {
                // Passthrough (not stream-json): SPEC rule-5 collapse + cap.
                val flat = firstLine(line, RESULT_MAX)
                if (flat.isNotEmpty()) out.add(flat)
            } else {
                out.add(d)
            }
        }
        return if (out.isEmpty()) null else out.joinToString("\n")
    }

    /**
     * One-line variant for a dashboard run row: the first surviving line of
     * [failureSummary] (each surviving line is already collapsed + capped).
     */
    fun failureLine(text: String?): String? =
        failureSummary(text)?.split("\n")?.first()

    /** First line of `text`, whitespace-collapsed to a single line, capped at `limit`. */
    private fun firstLine(text: String, limit: Int): String {
        // Split on ANY whitespace and rejoin with single spaces (flattens multi-line).
        val flat = text.split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
        return if (flat.length > limit) flat.substring(0, limit) + "…" else flat
    }

    /** JSON truthiness for the `is_error` checks (mirrors Python `if v:`): truthy
     *  unless missing, null, false, 0, "", or an empty list/object. */
    private fun isTruthy(v: Any?): Boolean = when (v) {
        null, JSONObject.NULL -> false
        is String -> v.isNotEmpty()
        is Boolean -> v
        is Number -> v.toDouble() != 0.0
        is JSONArray -> v.length() > 0
        is JSONObject -> v.length() > 0
        else -> true
    }
}
