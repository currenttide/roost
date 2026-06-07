package oss.roost.mobile

import oss.roost.mobile.model.Schedule
import oss.roost.mobile.model.ScheduleInterval
import oss.roost.mobile.model.ScheduleIntervalPreset
import oss.roost.mobile.model.ScheduleListReducer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-logic tests for the interval-schedule flow (API.md §7): the `every` grammar
 * (parse + 30 s floor), the compact interval formatter, and the list reducers. All
 * android-free so they run on the kotlinc + JUnitCore Linux harness. Mirrors the
 * iOS `SchedulesTests`. The grammar block is a CROSS-CONTRACT pin: every literal is
 * copied from the server's `tests/test_schedules.py`
 * (`test_parse_every_units_and_numbers`, `test_parse_every_garbage`,
 * `test_create_validates_interval`) so client/server drift is caught.
 */
class SchedulesTest {

    // ---- every grammar — mirrors server.py `parse_every` + the 30s floor ----

    /** Accepted, verbatim from `test_parse_every_units_and_numbers` (string cases). */
    @Test fun parseEveryUnitsAndNumbers() {
        assertEquals(30.0, ScheduleInterval.parse("30s"))
        assertEquals(300.0, ScheduleInterval.parse("5m"))
        assertEquals(7200.0, ScheduleInterval.parse("2h"))
        assertEquals(86400.0, ScheduleInterval.parse("1d"))
        assertEquals(90.0, ScheduleInterval.parse("90"))      // bare numeric string
        assertEquals(5400.0, ScheduleInterval.parse("1.5h"))  // decimal value
    }

    /** Rejected, from `test_parse_every_garbage` (string cases) → null like None. */
    @Test fun parseEveryGarbage() {
        assertNull(ScheduleInterval.parse("soon"))
        assertNull(ScheduleInterval.parse("5 fortnights"))
        assertNull(ScheduleInterval.parse(""))
        assertNull(ScheduleInterval.parse("h"))        // unit with no number
        assertNull(ScheduleInterval.parse("5x"))       // unknown unit
        assertNull(ScheduleInterval.parse("5m30s"))    // single unit only
    }

    /** Server lowercases before matching, so an uppercase unit parses. */
    @Test fun parseEveryIsCaseInsensitive() {
        assertEquals(21600.0, ScheduleInterval.parse("6H"))
        assertEquals(86400.0, ScheduleInterval.parse("1D"))
    }

    /** Surrounding whitespace is tolerated by `_EVERY_RE` (`^\s* … \s*$`). */
    @Test fun parseEveryToleratesWhitespace() {
        assertEquals(1800.0, ScheduleInterval.parse("  30m  "))
        assertEquals(90.0, ScheduleInterval.parse(" 90 "))
    }

    // ---- 30s floor — mirrors SCHEDULE_MIN_INTERVAL_SEC + test_create_validates_interval

    @Test fun floorMatchesServer() {
        // `test_create_validates_interval` posts "5s" and expects 400 (under floor).
        assertTrue(ScheduleInterval.parse("5s") != null)   // parses fine…
        assertFalse(ScheduleInterval.isValid("5s"))        // …but is below the floor
        assertFalse(ScheduleInterval.isValid("29"))        // bare seconds, under floor
        // Exactly at the floor is accepted (server uses `interval < MIN`).
        assertTrue(ScheduleInterval.isValid("30s"))
        assertTrue(ScheduleInterval.isValid("30"))
        assertEquals(30.0, ScheduleInterval.MIN_SECONDS, 0.0)
    }

    @Test fun isValidRejectsUnparseable() {
        assertFalse(ScheduleInterval.isValid("soon"))
        assertFalse(ScheduleInterval.isValid(""))
    }

    @Test fun validationMessageDistinguishesCauses() {
        assertNull(ScheduleInterval.validationMessage(""))      // empty → no message
        assertNull(ScheduleInterval.validationMessage("6h"))    // valid → no message
        assertEquals(
            "Use seconds or <N>[smhd] — e.g. 30s, 15m, 6h, 1d.",
            ScheduleInterval.validationMessage("soon"),
        )
        assertEquals("Minimum interval is 30s.", ScheduleInterval.validationMessage("5s"))
    }

    @Test fun allPresetsAreValid() {
        for (preset in ScheduleIntervalPreset.ALL) {
            assertTrue("preset not valid: $preset", ScheduleInterval.isValid(preset))
        }
    }

    // ---- format — byte-for-byte cli.py `_fmt_interval` (30s / 5m / 6h / 1d) ----

    @Test fun formatPrefersLargestWholeUnit() {
        assertEquals("30s", ScheduleInterval.format(30.0))
        assertEquals("5m", ScheduleInterval.format(300.0))
        assertEquals("30m", ScheduleInterval.format(1800.0))
        assertEquals("6h", ScheduleInterval.format(21600.0))
        assertEquals("1d", ScheduleInterval.format(86400.0))
        assertEquals("1h", ScheduleInterval.format(3600.0))
        assertEquals("90s", ScheduleInterval.format(90.0))   // not whole minutes
        assertEquals("90m", ScheduleInterval.format(5400.0)) // whole minutes, not hours
    }

    @Test fun formatParseRoundTrip() {
        for (sec in listOf(30.0, 300.0, 900.0, 1800.0, 3600.0, 21600.0, 43200.0, 86400.0)) {
            val formatted = ScheduleInterval.format(sec)
            assertEquals("round-trip failed for $sec → $formatted",
                sec, ScheduleInterval.parse(formatted))
        }
    }

    // ---- relative clock ----

    @Test fun relativeFutureAndPast() {
        assertEquals("in 30m", ScheduleInterval.relative(1_000 + 1800.0, 1_000.0))
        assertEquals("1h ago", ScheduleInterval.relative(1_000 - 3600.0, 1_000.0))
        assertNull(ScheduleInterval.relative(null, 1_000.0))
    }

    @Test fun relativeDueReadsAsNow() {
        assertEquals("now", ScheduleInterval.relative(1_000.0, 1_000.0))
        assertEquals("now", ScheduleInterval.relative(999.5, 1_000.0))
    }

    // ---- list reducers (API.md §7b–§7d) ----

    private fun sched(id: String, enabled: Boolean = true, name: String? = null) =
        Schedule(
            id = id, name = name, spec = emptyMap(), intervalSec = 1800.0, enabled = enabled,
            nextRunAt = 1_000.0, lastRunAt = null, lastJobId = null, createdAt = 1.0,
        )

    @Test fun prependPutsNewestFirstAndDedupes() {
        val list = listOf(sched("a"), sched("b"))
        val out = ScheduleListReducer.prepend(list, sched("c"))
        assertEquals(listOf("c", "a", "b"), out.map { it.id })
        // A create that races a refresh (same id present) doesn't double it.
        val out2 = ScheduleListReducer.prepend(out, sched("a"))
        assertEquals(listOf("a", "c", "b"), out2.map { it.id })
    }

    @Test fun upsertReplacesInPlace() {
        val list = listOf(sched("a", enabled = true), sched("b", enabled = true))
        val out = ScheduleListReducer.upsertExisting(list, sched("a", enabled = false))
        assertEquals(listOf("a", "b"), out.map { it.id })   // order preserved
        assertEquals(false, out.first().enabled)            // object swapped
    }

    @Test fun upsertUnknownIdLeavesListUnchanged() {
        val list = listOf(sched("a"), sched("b"))
        val out = ScheduleListReducer.upsertExisting(list, sched("z"))
        assertEquals(listOf("a", "b"), out.map { it.id })   // never fabricates a row
    }

    @Test fun removeDropsById() {
        val list = listOf(sched("a"), sched("b"), sched("c"))
        assertEquals(listOf("a", "c"), ScheduleListReducer.remove(list, "b").map { it.id })
        // Removing an id that isn't present is a no-op.
        assertEquals(listOf("a", "b", "c"), ScheduleListReducer.remove(list, "z").map { it.id })
    }

    // ---- taskSummary (list-row one-liner; mirrors iOS Schedule.taskSummary) ----

    @Test fun taskSummaryPrefersIntentThenCommand() {
        val agent = sched("a").copy(spec = mapOf("kind" to "claude", "intent" to "tidy the repo"))
        assertEquals("tidy the repo", agent.taskSummary)
        val cmd = sched("b").copy(spec = mapOf("kind" to "command", "command" to "echo hi"))
        assertEquals("echo hi", cmd.taskSummary)
    }

    @Test fun taskSummaryFallsBackToNameThenKind() {
        val named = sched("c", name = "nightly").copy(spec = mapOf("kind" to "claude"))
        assertEquals("nightly", named.taskSummary)
        val kindOnly = sched("d").copy(spec = mapOf("kind" to "docker"))
        assertEquals("docker job", kindOnly.taskSummary)
        val nothing = sched("e").copy(spec = emptyMap())
        assertEquals("scheduled job", nothing.taskSummary)
    }
}
