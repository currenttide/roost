package oss.roost.mobile.sse

import oss.roost.mobile.model.Ansi
import oss.roost.mobile.model.LogLine

/**
 * Ordered, de-duplicated log accumulator (API.md §5 resume protocol, rule 4).
 *
 * Pure Kotlin so the dedupe/cursor logic is unit-tested off-device. The overlap between
 * a catch-up `/logs?since=` page and the re-attached stream can replay frames; we drop any
 * line whose seq <= the max seq already seen. `maxSeq` is the cursor to persist per job.
 */
class LogBuffer(private val cap: Int = 2000) {
    private val lines = ArrayList<RenderedLine>()
    var maxSeq: Int = 0
        private set

    val rendered: List<RenderedLine> get() = lines

    /** Accept a line; returns true if it was new (appended), false if a dup was dropped. */
    fun accept(line: LogLine): Boolean {
        if (line.seq <= maxSeq) return false
        maxSeq = line.seq
        lines.add(RenderedLine.from(line))
        // Cap memory: the design budgets ~500 lines on-disk; in-memory we keep more but
        // trim the head so a very chatty job doesn't grow unbounded.
        if (lines.size > cap) lines.subList(0, lines.size - cap).clear()
        return true
    }

    fun acceptAll(batch: List<LogLine>): Int = batch.count { accept(it) }

    /** Seed the cursor from a persisted value without emitting lines (on cold start). */
    fun restoreCursor(seq: Int) { if (seq > maxSeq) maxSeq = seq }

    /**
     * Seed from the offline cache on cold start: restore lines AND derive the
     * cursor from them in one step. Lines and cursor must come from the same
     * artifact — a cursor restored without its lines would leave pre-cursor
     * history permanently invisible (catch-up pages only seq > cursor).
     * No-op unless the buffer is empty.
     */
    fun seed(cached: List<RenderedLine>) {
        if (lines.isNotEmpty() || cached.isEmpty()) return
        lines.addAll(cached.sortedBy { it.seq })
        maxSeq = maxOf(maxSeq, cached.maxOf { it.seq })
    }
}

/** A log line prepared for display: ANSI stripped, event rows flagged as dividers. */
data class RenderedLine(
    val seq: Int,
    val text: String,
    val kind: Kind,
) {
    enum class Kind { STDOUT, STDERR, EVENT }

    companion object {
        fun from(line: LogLine): RenderedLine = when (line.stream) {
            "event" -> RenderedLine(line.seq, EventLine.label(line.data), Kind.EVENT)
            "stderr" -> RenderedLine(line.seq, Ansi.strip(line.data), Kind.STDERR)
            else -> RenderedLine(line.seq, Ansi.strip(line.data), Kind.STDOUT)
        }
    }
}

/**
 * `stream:"event"` rows carry lifecycle JSON like {"type":"started"}. Render a short
 * divider label; skip/parse-fail returns a neutral label rather than crashing (§4).
 */
object EventLine {
    fun label(data: String): String = try {
        val type = org.json.JSONObject(data).optString("type", "")
        if (type.isBlank()) "event" else type
    } catch (_: Exception) {
        // Unparseable event payload — show nothing meaningful but don't crash.
        "event"
    }
}
