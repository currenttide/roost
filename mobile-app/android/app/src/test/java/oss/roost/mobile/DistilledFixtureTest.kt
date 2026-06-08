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

    @Test fun coversAllSixteenCases() {
        // Pin the fixture count so a silently-shrunk fixture file is caught.
        val cases = JSONObject(Fixtures.read("distilled/cases.json")).getJSONArray("cases")
        assertTrue("expected the full R107 golden set", cases.length() >= 16)
    }

    @Test fun nullAndPlainInputsAreSafe() {
        // Pure-function contract: never throws; non-JSON passes through verbatim.
        assertNull(DistilledLine.from(null))
        assertEquals("plain text", DistilledLine.from("plain text"))
        assertEquals("", DistilledLine.from(""))
    }
}
