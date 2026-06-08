package oss.roost.mobile

import oss.roost.mobile.model.LogLine
import oss.roost.mobile.sse.LogBuffer
import oss.roost.mobile.sse.RenderedLine
import oss.roost.mobile.sse.SessionLines
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The raw/distilled session-view projection (R109). DISTILLED is the DEFAULT
 * (`showRaw == false`): event noise, thinking/signature blobs, and rate-limit
 * pings vanish; assistant text + "→ Tool" + "  ⎿ result" + phase dividers remain.
 * RAW (`showRaw == true`) shows every line. Pure, so it runs on the Linux harness.
 */
class SessionLinesTest {

    private fun log(seq: Int, stream: String, data: String) = LogLine(seq, stream, data, 0.0)

    /** A realistic mixed stream: event envelopes + agent stream-json + plain stdout. */
    private fun mixedBuffer(): LogBuffer {
        val buf = LogBuffer()
        buf.acceptAll(listOf(
            log(1, "event", """{"type":"started","attempt":1}"""),
            log(2, "stdout", """{"type":"system","subtype":"init","model":"x"}"""),
            log(3, "stdout", """{"type":"rate_limit_event","rate_limit_info":{}}"""),
            log(4, "stdout", """{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"","signature":"BLOB"}]}}"""),
            log(5, "stdout", """{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"ls -la"}}]}}"""),
            log(6, "stdout", """{"type":"assistant","message":{"content":[{"type":"text","text":"Done."}]}}"""),
            log(7, "stdout", """{"type":"result","subtype":"success","is_error":false}"""),
            log(8, "event", """{"type":"succeeded"}"""),
        ))
        return buf
    }

    @Test fun distilledIsTheDefaultAndSuppressesNoise() {
        val display = SessionLines.forDisplay(mixedBuffer().rendered, showRaw = false)
        // event rows, rate-limit, and thinking blob are gone; the rest distil.
        assertEquals(
            listOf("🔎 starting…", "→ Bash: ls -la", "Done.", "✓ done"),
            display.map { it.text },
        )
        // Distilled rows are never EVENT dividers (rendered as plain transcript text).
        assertEquals(emptyList<RenderedLine.Kind>(),
            display.map { it.kind }.filter { it == RenderedLine.Kind.EVENT })
    }

    @Test fun rawShowsEveryLineUnchanged() {
        val buf = mixedBuffer()
        val display = SessionLines.forDisplay(buf.rendered, showRaw = true)
        // All 8 lines survive; raw view keeps the event divider labels + raw text.
        assertEquals(8, display.size)
        assertEquals(buf.rendered, display)
        assertEquals("started", display.first().text)              // event label, raw only
        assertEquals(RenderedLine.Kind.EVENT, display.first().kind)
    }

    @Test fun plainCommandStdoutPassesThroughInBothModes() {
        val buf = LogBuffer()
        buf.accept(log(1, "stdout", "hello from a command job"))
        assertEquals(listOf("hello from a command job"),
            SessionLines.forDisplay(buf.rendered, showRaw = false).map { it.text })
        assertEquals(listOf("hello from a command job"),
            SessionLines.forDisplay(buf.rendered, showRaw = true).map { it.text })
    }
}
