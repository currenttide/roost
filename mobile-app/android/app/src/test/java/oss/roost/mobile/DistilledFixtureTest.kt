package oss.roost.mobile

import oss.roost.mobile.model.DistilledLine
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Cross-platform contract guard for the distilled live-stream transform (R109).
 *
 * Loads the ONE canonical golden-fixture file
 * `mobile-app/fixtures/distilled/cases.json` (shared with the CLI reference
 * `roost.cli.distill_log_line` and iOS R108) and asserts that the Kotlin
 * `DistilledLine.from` produces EXACTLY each case's expected output — `null`
 * meaning the line is suppressed. Expectations are NOT hand-rolled here: they
 * come straight from the shared fixtures, so any platform that drifts from the
 * contract fails this test. See `mobile-app/fixtures/distilled/SPEC.md`.
 */
class DistilledFixtureTest {

    @Test fun everyGoldenCaseMatches() {
        val root = JSONObject(Fixtures.read("distilled/cases.json"))
        val cases = root.getJSONArray("cases")
        assertTrue("fixtures should contain cases", cases.length() > 0)

        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            val note = case.optString("note", "case #$i")
            val raw = case.getString("raw")
            val actual = DistilledLine.from(raw)
            if (case.isNull("distilled")) {
                assertNull("[$note] should be suppressed (null)", actual)
            } else {
                assertEquals("[$note] distilled mismatch", case.getString("distilled"), actual)
            }
        }
    }

    @Test fun coversFullGoldenSet() {
        // Pin the fixture count so a silently-shrunk fixture file is caught. The
        // floor rose to 65 in R113 (SPEC-branch + adversarial-shape expansion).
        val cases = JSONObject(Fixtures.read("distilled/cases.json")).getJSONArray("cases")
        assertTrue("expected the full R107+R113 golden set", cases.length() >= 66)
    }

    @Test fun nullAndPlainInputsAreSafe() {
        // Pure-function contract: never throws; non-JSON passes through verbatim.
        assertNull(DistilledLine.from(null))
        assertEquals("plain text", DistilledLine.from("plain text"))
        assertEquals("", DistilledLine.from(""))
    }

    // R113: is_error uses JSON truthiness, not optBoolean (the Android outlier).
    // A truthy non-bool is_error (1, "yes") must mark the failure/error — matching
    // the CLI/iOS. Pinned directly here, not only via the shared fixtures.
    @Test fun isErrorUsesJsonTruthinessNotOptBoolean() {
        assertEquals("✗ failed", DistilledLine.from("""{"type":"result","is_error":1}"""))
        assertEquals("✗ failed", DistilledLine.from("""{"type":"result","is_error":"yes"}"""))
        assertEquals("✓ done", DistilledLine.from("""{"type":"result","is_error":0}"""))
        assertEquals("✓ done", DistilledLine.from("""{"type":"result","is_error":""}"""))
        val tr = { e: String -> """{"type":"user","message":{"content":[{"type":"tool_result","is_error":$e,"content":"boom"}]}}""" }
        assertEquals("  ⎿ ✗ boom", DistilledLine.from(tr("1")))
        assertEquals("  ⎿ ✗ boom", DistilledLine.from(tr("\"yes\"")))
        assertEquals("  ⎿ boom", DistilledLine.from(tr("0")))
    }

    // R113: non-string hint values are skipped (bare arrow), and non-string text
    // is suppressed — so the three clients stay byte-identical on malformed input.
    @Test fun nonStringHintAndTextDoNotLeakCoercion() {
        val tu = { v: String -> """{"type":"assistant","message":{"content":[{"type":"tool_use","name":"X","input":{"command":$v}}]}}""" }
        assertEquals("→ X", DistilledLine.from(tu("42")))
        assertEquals("→ X", DistilledLine.from(tu("true")))
        assertEquals("→ X", DistilledLine.from(tu("[\"a\",\"b\"]")))
        // non-string command skipped, falls through to file_path
        assertEquals("→ X: /p", DistilledLine.from(
            """{"type":"assistant","message":{"content":[{"type":"tool_use","name":"X","input":{"command":0,"file_path":"/p"}}]}}"""))
        // non-string / null text -> suppressed
        assertNull(DistilledLine.from("""{"type":"assistant","message":{"content":[{"type":"text","text":123}]}}"""))
        assertNull(DistilledLine.from("""{"type":"assistant","message":{"content":[{"type":"text","text":null}]}}"""))
    }
}
