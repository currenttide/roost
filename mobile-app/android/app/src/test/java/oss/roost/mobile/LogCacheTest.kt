package oss.roost.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import oss.roost.mobile.sse.LogBuffer
import oss.roost.mobile.sse.LogCache
import oss.roost.mobile.sse.RenderedLine

class LogCacheTest {

    private fun line(seq: Int, kind: RenderedLine.Kind = RenderedLine.Kind.STDOUT) =
        RenderedLine(seq, "line $seq", kind)

    @Test
    fun roundTripPreservesLinesAndKinds() {
        val lines = listOf(
            line(1, RenderedLine.Kind.EVENT),
            line(2),
            line(3, RenderedLine.Kind.STDERR),
        )
        assertEquals(lines, LogCache.decode(LogCache.encode(lines)))
    }

    @Test
    fun encodeCapsToTail() {
        val lines = (1..600).map { line(it) }
        val decoded = LogCache.decode(LogCache.encode(lines))
        assertEquals(500, decoded.size)
        assertEquals(101, decoded.first().seq)  // head trimmed, tail kept
        assertEquals(600, decoded.last().seq)
    }

    @Test
    fun decodeToleratesGarbageAndUnknownKinds() {
        assertEquals(emptyList<RenderedLine>(), LogCache.decode("not json"))
        assertEquals(emptyList<RenderedLine>(), LogCache.decode("{}"))
        // Unknown kind degrades to STDOUT; missing seq rows are skipped.
        val decoded = LogCache.decode(
            """[{"seq":5,"text":"x","kind":"SPARKLES"},{"text":"no seq"}]"""
        )
        assertEquals(listOf(RenderedLine(5, "x", RenderedLine.Kind.STDOUT)), decoded)
    }

    @Test
    fun seedRestoresLinesAndDerivesCursor() {
        // The cold-start fix: lines AND cursor come from one artifact, so
        // catch-up (seq > maxSeq) continues where the cache ends.
        val buf = LogBuffer()
        buf.seed(LogCache.decode(LogCache.encode(listOf(line(3), line(1), line(2)))))
        assertEquals(3, buf.maxSeq)
        assertEquals(listOf(1, 2, 3), buf.rendered.map { it.seq })  // re-sorted
    }

    @Test
    fun seedIsNoOpWhenBufferAlreadyHasLines() {
        val buf = LogBuffer()
        buf.accept(oss.roost.mobile.model.LogLine(7, "stdout", "live", 0.0))
        buf.seed(listOf(line(1), line(2)))
        assertTrue(buf.rendered.map { it.seq } == listOf(7))
        assertEquals(7, buf.maxSeq)
    }
}
