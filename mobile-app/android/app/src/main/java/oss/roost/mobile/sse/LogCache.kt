package oss.roost.mobile.sse

import org.json.JSONArray
import org.json.JSONObject

/**
 * Pure-Kotlin codec for the per-job offline log cache (DESIGN §5: "single JSON
 * file per job, capped at 500 lines").
 *
 * We cache *rendered* lines ({seq, text, kind}) rather than raw LogLines: the
 * cache exists only to repaint the session view offline / on cold start, and
 * rendered form means no re-parsing of event rows on load. The max cached seq
 * doubles as the resume cursor — seeding lines and cursor from one artifact
 * keeps them consistent (a persisted cursor without its lines would make
 * history invisible after a cold start).
 */
object LogCache {
    const val CAP = 500

    fun encode(lines: List<RenderedLine>, cap: Int = CAP): String {
        val tail = if (lines.size > cap) lines.subList(lines.size - cap, lines.size) else lines
        val arr = JSONArray()
        for (l in tail) {
            arr.put(JSONObject().put("seq", l.seq).put("text", l.text).put("kind", l.kind.name))
        }
        return arr.toString()
    }

    /** Tolerant decode: a corrupt/unknown row is skipped, never a crash. */
    fun decode(json: String): List<RenderedLine> = try {
        val arr = JSONArray(json)
        (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            val seq = o.optInt("seq", -1)
            if (seq < 0) return@mapNotNull null
            val kind = try {
                RenderedLine.Kind.valueOf(o.optString("kind", "STDOUT"))
            } catch (_: IllegalArgumentException) {
                RenderedLine.Kind.STDOUT
            }
            RenderedLine(seq, o.optString("text", ""), kind)
        }.sortedBy { it.seq }
    } catch (_: Exception) {
        emptyList()
    }
}
