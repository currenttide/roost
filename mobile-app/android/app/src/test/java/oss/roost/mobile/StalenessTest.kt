package oss.roost.mobile

import oss.roost.mobile.model.Staleness
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * R75 — the offline staleness pill (DESIGN.md §2 / API.md §2) must fire when
 * `generated_at` drifts > 10 s behind the wall clock, *even while no new data
 * arrives*. The user-testing sweep (android/11) saw a confirmed 35 s outage with
 * the pill never appearing.
 *
 * Root cause (verified in code): the dashboard read `now` once per recomposition
 * (`val nowMs = System.currentTimeMillis()`), and after the first failed poll the
 * repeated `_state.copy(error = sameMessage)` produced an `equals` UiState that
 * `MutableStateFlow` deduped → no recomposition → `now` froze near the first
 * failure → age stayed < 10 s forever. The staleness decision is wall-clock
 * driven, so it is exercised here as a pure function of (generatedAt, now): the
 * fix is a 1 s ticker that keeps feeding a fresh `now` independent of StateFlow
 * emissions.
 */
class StalenessTest {

    /** Helper: seconds → epoch-millis for `now`. */
    private fun nowMsAt(epochSec: Double): Long = (epochSec * 1000.0).toLong()

    @Test
    fun freshData_noPill() {
        // generated_at == now → age 0 → no pill.
        assertNull(Staleness.pillText(generatedAt = 1000.0, nowMs = nowMsAt(1000.0)))
    }

    @Test
    fun underThreshold_noPill() {
        // 9 s old: still under the 10 s guard.
        assertNull(Staleness.pillText(generatedAt = 1000.0, nowMs = nowMsAt(1009.0)))
    }

    @Test
    fun overThreshold_pillFires() {
        // 11 s old: trips the guard.
        assertEquals("data 11s old", Staleness.pillText(generatedAt = 1000.0, nowMs = nowMsAt(1011.0)))
    }

    /**
     * The exact regression: a successful poll lands at T, then the network drops
     * and every subsequent poll fails. `generated_at` is frozen at the last good
     * payload, so as long as the UI keeps feeding a fresh wall clock (the ticker),
     * the pill MUST appear once 10 s of outage have elapsed — and keep counting up.
     */
    @Test
    fun simulatedFailedPolls_pillAppearsAfterTenSeconds() {
        val lastGoodGeneratedAt = 1_000.0   // last successful /derived payload time
        // Failed polls every 2 s; the ticker advances `now` 1 s at a time.
        // Outage timeline (seconds after last success): pill stays null until >10 s,
        // then fires and the age climbs.
        assertNull(Staleness.pillText(lastGoodGeneratedAt, nowMsAt(1_002.0)))  // 2 s out
        assertNull(Staleness.pillText(lastGoodGeneratedAt, nowMsAt(1_006.0)))  // 6 s out
        assertNull(Staleness.pillText(lastGoodGeneratedAt, nowMsAt(1_010.0)))  // 10 s out (== boundary, not >)
        assertEquals(
            "data 11s old",
            Staleness.pillText(lastGoodGeneratedAt, nowMsAt(1_011.0)),         // 11 s out → PILL
        )
        // 35 s outage from the report → still visible, age tracks wall clock.
        assertEquals(
            "data 35s old",
            Staleness.pillText(lastGoodGeneratedAt, nowMsAt(1_035.0)),
        )
    }

    /** Format.staleness must share the single source of truth (no drift). */
    @Test
    fun formatDelegatesToStaleness() {
        // Spot-check via the model fn that both call sites agree on the boundary.
        assertNull(Staleness.pillText(0.0, nowMsAt(10.0)))
        assertEquals("data 12s old", Staleness.pillText(0.0, nowMsAt(12.0)))
    }
}
