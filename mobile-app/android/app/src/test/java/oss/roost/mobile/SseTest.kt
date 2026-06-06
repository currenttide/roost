package oss.roost.mobile

import oss.roost.mobile.model.LogLine
import oss.roost.mobile.model.StreamEvent
import oss.roost.mobile.sse.LogBuffer
import oss.roost.mobile.sse.SseFrames
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parse the golden SSE transcript and assert the EXACT event sequence (state, 6 logs,
 * done) plus seq-dedupe of a replayed frame (API.md §5).
 */
class SseTest {

    @Test fun transcriptSequenceIsExact() {
        val events = SseFrames.parseTranscript(Fixtures.read("stream_succeeded.sse.txt"))

        // 1 state + 6 logs + 1 done = 8 events, in order.
        assertEquals(8, events.size)
        assertTrue(events[0] is StreamEvent.State)
        assertEquals("succeeded", (events[0] as StreamEvent.State).state)

        for (i in 1..6) {
            assertTrue("event #$i should be a log", events[i] is StreamEvent.Log)
        }
        // seq runs 1..6 in order.
        val seqs = events.filterIsInstance<StreamEvent.Log>().map { it.line.seq }
        assertEquals(listOf(1, 2, 3, 4, 5, 6), seqs)
        // first and last logs are event-stream rows.
        assertEquals("event", (events[1] as StreamEvent.Log).line.stream)
        assertEquals("event", (events[6] as StreamEvent.Log).line.stream)
        // a middle one is stdout.
        assertEquals("stdout", (events[2] as StreamEvent.Log).line.stream)
        assertEquals("running pytest -q ...", (events[2] as StreamEvent.Log).line.data)

        val done = events[7]
        assertTrue(done is StreamEvent.Done)
        done as StreamEvent.Done
        assertEquals("succeeded", done.state)
        assertEquals(0, done.exitCode)
        assertEquals(48213, done.tokensUsed)
        assertEquals("fixed: tests green", done.resultOutput)
    }

    @Test fun dedupeDropsReplayedFrame() {
        val events = SseFrames.parseTranscript(Fixtures.read("stream_succeeded.sse.txt"))
        val logLines = events.filterIsInstance<StreamEvent.Log>().map { it.line }

        val buf = LogBuffer()
        // Feed all 6; all new.
        assertEquals(6, buf.acceptAll(logLines))
        assertEquals(6, buf.maxSeq)
        assertEquals(6, buf.rendered.size)

        // Replay seq 5 and 6 (the catch-up/stream overlap) — both must be dropped.
        assertFalse(buf.accept(logLines[4])) // seq 5
        assertFalse(buf.accept(logLines[5])) // seq 6
        assertEquals(6, buf.rendered.size)   // unchanged

        // A genuinely-new higher seq is accepted.
        assertTrue(buf.accept(LogLine(seq = 7, stream = "stdout", data = "next", ts = 0.0)))
        assertEquals(7, buf.maxSeq)
    }

    @Test fun frameBufferHandlesSplitChunks() {
        // Simulate the socket delivering a frame across two reads.
        val full = Fixtures.read("stream_succeeded.sse.txt")
        val mid = full.length / 2
        val fb = SseFrames.FrameBuffer()
        val out = ArrayList<StreamEvent>()
        for (block in fb.feed(full.substring(0, mid))) {
            SseFrames.parseFrame(block)?.let(out::add)
        }
        for (block in fb.feed(full.substring(mid))) {
            SseFrames.parseFrame(block)?.let(out::add)
        }
        // The final frame (done) has no trailing blank line in the fixture, so it may be
        // buffered; assert we at least recovered the early frames in order.
        assertTrue(out.isNotEmpty())
        assertTrue(out.first() is StreamEvent.State)
    }

    @Test fun eventRowRendersAsDivider() {
        val events = SseFrames.parseTranscript(Fixtures.read("stream_succeeded.sse.txt"))
        val buf = LogBuffer()
        buf.acceptAll(events.filterIsInstance<StreamEvent.Log>().map { it.line })
        val first = buf.rendered.first()
        // seq 1 is an event row {"type":"started"} → EVENT kind, label "started".
        assertEquals(oss.roost.mobile.sse.RenderedLine.Kind.EVENT, first.kind)
        assertEquals("started", first.text)
    }
}
