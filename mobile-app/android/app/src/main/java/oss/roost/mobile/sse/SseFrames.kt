package oss.roost.mobile.sse

import oss.roost.mobile.model.Parsers
import oss.roost.mobile.model.StreamEvent

/**
 * Pure-Kotlin SSE frame splitter for GET /jobs/{id}/stream (API.md §5).
 *
 * The wire format: frames separated by a blank line; within a frame, lines prefixed
 * `event:` and `data:` matter, everything else (comments starting `:`, `id:`, `retry:`)
 * is ignored. `data:` is a single line of JSON in our contract (no multi-line data
 * concatenation needed). This object handles the BYTE/STRING → frame → StreamEvent step
 * so it is testable off-device; the live socket reader lives in SseClient (android side).
 *
 * Why a stateful FrameBuffer: a streaming HTTP body arrives in arbitrary chunks that
 * don't align to frame boundaries. We accumulate and emit only complete frames.
 */
object SseFrames {

    /** Parse a single already-delimited frame block (its lines) into a StreamEvent. */
    fun parseFrame(block: String): StreamEvent? {
        var event: String? = null
        val dataSb = StringBuilder()
        var hasData = false
        for (rawLine in block.split('\n')) {
            val line = rawLine.removeSuffix("\r")
            when {
                line.isEmpty() -> {}
                line.startsWith(":") -> {} // comment / heartbeat
                line.startsWith("event:") -> event = line.substring(6).trim()
                line.startsWith("data:") -> {
                    // Per SSE: strip ONE leading space after the colon if present.
                    val d = line.substring(5).let { if (it.startsWith(" ")) it.substring(1) else it }
                    if (hasData) dataSb.append('\n')
                    dataSb.append(d)
                    hasData = true
                }
                // id:, retry:, and unknown fields are ignored per §5.
            }
        }
        if (event == null || !hasData) return null
        return Parsers.parseStreamFrame(event, dataSb.toString())
    }

    /**
     * Parse a full transcript (e.g. the golden stream_succeeded.sse.txt) into the ordered
     * list of events. Frames split on a blank line (`\n\n`, tolerant of `\r\n\r\n`).
     */
    fun parseTranscript(text: String): List<StreamEvent> {
        val normalized = text.replace("\r\n", "\n")
        return normalized.split("\n\n")
            .filter { it.isNotBlank() }
            .mapNotNull { parseFrame(it) }
    }

    /**
     * Incremental buffer for the live socket: feed raw chunks, get back complete frames as
     * they close on a blank line. The trailing partial frame stays buffered.
     */
    class FrameBuffer {
        private val buf = StringBuilder()

        /** Append a chunk; return any complete frame-blocks now available (in order). */
        fun feed(chunk: String): List<String> {
            buf.append(chunk.replace("\r\n", "\n"))
            val frames = ArrayList<String>()
            while (true) {
                val idx = buf.indexOf("\n\n")
                if (idx < 0) break
                val block = buf.substring(0, idx)
                buf.delete(0, idx + 2)
                if (block.isNotBlank()) frames.add(block)
            }
            return frames
        }
    }
}
