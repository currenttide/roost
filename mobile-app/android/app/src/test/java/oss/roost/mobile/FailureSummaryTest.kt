package oss.roost.mobile

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import oss.roost.mobile.model.DistilledLine
import oss.roost.mobile.model.Parsers

/**
 * R122: failed-agent results render DISTILLED, not as raw stream-json walls.
 *
 * A worker can report a failure whose `result.output`/`error` is one or more
 * raw Anthropic stream-json lines (the UAT "failed-agent rows render raw JSON"
 * finding). `DistilledLine.failureSummary` / `.failureLine` REUSE the SPEC.md
 * transform + truncation rules (no new transform branch — the golden fixtures
 * in `mobile-app/fixtures/distilled/cases.json` still pin the underlying
 * transform): stream-json lines distil, noise suppresses, and verbatim
 * passthrough lines are whitespace-collapsed + capped at 200 per SPEC rule 5.
 *
 * The `// PARITY:` cases below are mirrored byte-for-byte in the iOS harness
 * (`FailureSummaryTests.swift`) so the two phones can't drift on the failure
 * rendering either.
 */
class FailureSummaryTest {

    // ---- PARITY cases (mirrored in iOS FailureSummaryTests.swift) ----

    @Test fun rawResultEnvelopeWallDistilsToPhaseDivider() {
        // PARITY P1: the most common wall — the final `result` envelope.
        val wall = """{"type":"result","subtype":"error_during_execution","is_error":true,""" +
            """"duration_ms":4521,"num_turns":3,""" +
            """"usage":{"input_tokens":9114,"output_tokens":201}}"""
        assertEquals("✗ failed", DistilledLine.failureSummary(wall))
        assertEquals("✗ failed", DistilledLine.failureLine(wall))
    }

    @Test fun assistantEnvelopeWallDistilsToItsText() {
        // PARITY P2: an `assistant` envelope reported as the failure output.
        val wall = """{"type":"assistant","message":{"content":[{"type":"text",""" +
            """"text":"I could not reach the host — connection refused."}]}}"""
        assertEquals(
            "I could not reach the host — connection refused.",
            DistilledLine.failureSummary(wall),
        )
    }

    @Test fun plainErrorTextPassesThroughCollapsed() {
        // PARITY P3: an honest plain-text error is kept (rule 1), with each
        // line whitespace-collapsed per rule 5.
        val text = "verification failed after 2 self-heal attempt(s): evidence  says\nno artifact"
        assertEquals(
            "verification failed after 2 self-heal attempt(s): evidence says\nno artifact",
            DistilledLine.failureSummary(text),
        )
    }

    @Test fun nonJsonWallIsCappedAt200() {
        // PARITY P4: a JSON-ish-but-unparseable wall (e.g. a Python dict repr)
        // passes through rule 1 but is capped at RESULT_MAX with a single `…`.
        val wall = "{'type': 'result', 'is_error': True, " + "x".repeat(200)
        val out = DistilledLine.failureSummary(wall)
        assertEquals(201, out?.length)   // 200 chars + U+2026
        assertEquals(wall.substring(0, 200) + "…", out)
    }

    @Test fun mixedLinesDistilSuppressAndPassThrough() {
        // PARITY P5: stream-json distils, noise suppresses, plain text stays.
        val text = """{"type":"system","subtype":"init"}""" + "\n" +
            """{"type":"rate_limit_event"}""" + "\n" +
            "exit_code=1"
        assertEquals("🔎 starting…\nexit_code=1", DistilledLine.failureSummary(text))
    }

    @Test fun allNoiseSuppressesToNull() {
        // PARITY P6: nothing survives → null (caller falls back to its state line).
        assertNull(DistilledLine.failureSummary("""{"type":"rate_limit_event"}"""))
    }

    @Test fun nullAndBlankAreNull() {
        // PARITY P7.
        assertNull(DistilledLine.failureSummary(null))
        assertNull(DistilledLine.failureSummary(""))
        assertNull(DistilledLine.failureSummary("   \n  "))
    }

    @Test fun failureLineTakesFirstSurvivingLine() {
        // PARITY P8: a multi-block assistant envelope distils to several lines;
        // the dashboard row shows the first.
        val wall = """{"type":"assistant","message":{"content":[{"type":"text",""" +
            """"text":"Let me check"},{"type":"tool_use","name":"Read",""" +
            """"input":{"file_path":"/etc/hostname"}}]}}"""
        assertEquals("Let me check\n→ Read: /etc/hostname", DistilledLine.failureSummary(wall))
        assertEquals("Let me check", DistilledLine.failureLine(wall))
    }

    // ---- Wiring: dashboard run row (Run.bestLine) ----

    private fun runRow(state: String, result: String, narration: String? = null) =
        Parsers.parseRun(
            JSONObject()
                .put("run_id", "j1")
                .put("state", state)
                .put("result", result)
                .apply { narration?.let { put("narration", it) } },
        )

    @Test fun failedRunBestLineDistilsResultWall() {
        val r = runRow("failed", """{"type":"result","is_error":true}""")
        assertEquals("✗ failed", r.bestLine)
    }

    @Test fun nonFailedRunBestLineUnchanged() {
        // A succeeded row keeps today's verbatim behavior — R122 touches only
        // failure rendering.
        val wall = """{"type":"result","is_error":false}"""
        assertEquals(wall, runRow("succeeded", wall).bestLine)
    }

    @Test fun failedRunBestLinePrefersNarrationAndDistilsIt() {
        val r = runRow(
            "failed", "exit_code=1",
            narration = """{"type":"assistant","message":{"content":[{"type":"text","text":"boom"}]}}""",
        )
        assertEquals("boom", r.bestLine)
    }

    @Test fun failedRunBestLineNullWhenEverythingSuppresses() {
        val r = runRow("failed", """{"type":"rate_limit_event"}""")
        assertNull(r.bestLine)
    }
}
