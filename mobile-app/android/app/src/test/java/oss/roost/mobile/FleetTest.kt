package oss.roost.mobile

import oss.roost.mobile.model.Fleet
import oss.roost.mobile.model.Parsers
import oss.roost.mobile.model.Worker
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-layer tests for the Fleet screen (R121, API.md §2a): the golden
 * `workers.json` decode (busy + idle-GPU + offline rows), staleness pills
 * mirroring the server's 45/120 s thresholds (the R75 pattern), the
 * capability summary, load + last-seen lines, and the display sort. The
 * example strings here are byte-identical to iOS `FleetTests.swift` — the
 * cross-platform consistency guarantee for the two Fleet screens.
 */
class FleetTest {

    private fun fixtureWorkers(): List<Worker> =
        Parsers.parseWorkers(Fixtures.read("workers.json"))

    /** Seconds → epoch-millis for `nowMs` (matches StalenessTest's helper). */
    private fun nowMsAt(epochSec: Double): Long = (epochSec * 1000.0).toLong()

    // ---- fixture decode (API.md §2a row shape) --------------------------------

    @Test fun workersFixtureCoversStatusVocabulary() {
        val w = fixtureWorkers()
        assertEquals(3, w.size)
        assertEquals(listOf("busy", "idle", "offline"), w.map { it.status })
        assertEquals(
            listOf("fixture-node", "fixture-gpu-node", "fixture-offline-node"),
            w.map { it.name },
        )
        // Load fields: the busy row runs 1/1; the others are 0/1.
        assertEquals(1, w[0].running)
        assertEquals(1, w[0].capacity)
        assertEquals(0, w[1].running)
        // Heterogeneous capabilities survive the tolerant map coercion.
        assertEquals(16, (w[1].capabilities["gpu_vram_gb"] as Number).toInt())
        assertEquals("gpu-host", w[1].capabilities["hostname"])
        assertEquals("aarch64", w[2].capabilities["arch"])
        // The offline row was last seen ~10 min before the live rows.
        assertTrue(w[0].lastSeen!! - w[2].lastSeen!! > Fleet.OFFLINE_AFTER_SEC)
    }

    // ---- staleness pills (R75 pattern, server thresholds 45/120 s) ------------

    @Test fun pillThresholds() {
        val seen = 1_000.0
        // Fresh: no pill.
        assertNull(Fleet.pill("idle", seen, nowMsAt(seen + 10)))
        // 44 s: still fresh; 45 s: stale (>= threshold).
        assertNull(Fleet.pill("idle", seen, nowMsAt(seen + 44)))
        assertEquals(Fleet.Pill.STALE, Fleet.pill("idle", seen, nowMsAt(seen + 45)))
        // 119 s: stale; 120 s: offline.
        assertEquals(Fleet.Pill.STALE, Fleet.pill("busy", seen, nowMsAt(seen + 119)))
        assertEquals(Fleet.Pill.OFFLINE, Fleet.pill("busy", seen, nowMsAt(seen + 120)))
    }

    @Test fun serverWordWinsOfflineAndStale() {
        val seen = 1_000.0
        // A server-declared offline row is offline however fresh the payload.
        assertEquals(Fleet.Pill.OFFLINE, Fleet.pill("offline", seen, nowMsAt(seen + 1)))
        // A server-declared stale row shows stale even with a fresh clock gap.
        assertEquals(Fleet.Pill.STALE, Fleet.pill("stale", seen, nowMsAt(seen + 1)))
        // …but the clock can still escalate a stale row to offline.
        assertEquals(Fleet.Pill.OFFLINE, Fleet.pill("stale", seen, nowMsAt(seen + 500)))
        // No last_seen at all: trust the status word only.
        assertNull(Fleet.pill("idle", null, nowMsAt(seen)))
        assertEquals(Fleet.Pill.OFFLINE, Fleet.pill("offline", null, nowMsAt(seen)))
    }

    @Test fun isUpAndHeadline() {
        val w = fixtureWorkers()
        // "Now" = the busy row's heartbeat: live rows are up, offline is not.
        val now = nowMsAt(w[0].lastSeen!!)
        val up = w.filter { Fleet.isUp(it.status, it.lastSeen, now) }
        assertEquals(listOf("fixture-node", "fixture-gpu-node"), up.map { it.name })
        assertEquals("2 of 3 up", Fleet.headline(up.size, w.size))
        // The same rows 10 minutes later: every heartbeat is ancient → 0 up.
        val later = nowMsAt(w[0].lastSeen!! + 600)
        assertEquals(0, w.count { Fleet.isUp(it.status, it.lastSeen, later) })
        // Unknown status never counts as up, however fresh.
        assertFalse(Fleet.isUp("rebooting", w[0].lastSeen, now))
    }

    // ---- capability summary (cross-platform string contract) ------------------

    @Test fun capsSummaryFixtureRows() {
        val w = fixtureWorkers()
        // GPU node: hostname · gpu · arch · cpus (mirrors `roost workers`).
        assertEquals("gpu-host · gpu 16GB · x86_64 · 8 cpu", Fleet.capsSummary(w[1].capabilities))
        // Offline pi: no GPU key at all → no gpu segment.
        assertEquals("attic-pi · aarch64 · 2 cpu", Fleet.capsSummary(w[2].capabilities))
        // Original busy row advertises only tools/cpus → cpus only.
        assertEquals("4 cpu", Fleet.capsSummary(w[0].capabilities))
    }

    @Test fun capsSummaryEdgeCases() {
        assertNull(Fleet.capsSummary(emptyMap()))
        assertNull(Fleet.capsSummary(mapOf("tools" to listOf("python3"))))
        // R41: a broken GPU probe is flagged, not silently bare.
        assertEquals(
            "box · gpu: detection failed",
            Fleet.capsSummary(mapOf("gpu_detection" to "failed", "hostname" to "box")),
        )
        // Fractional VRAM keeps its decimal (Jetson-style 30.7).
        assertEquals("gpu 30.7GB", Fleet.capsSummary(mapOf("gpu_vram_gb" to 30.7)))
    }

    // ---- load + last-seen lines -------------------------------------------------

    @Test fun loadText() {
        assertEquals("1/1 running", Fleet.loadText(1, 1))
        assertEquals("2/4 running", Fleet.loadText(2, 4))
        // Older CP omitting the fields → honest zeros over a 1-slot default.
        assertEquals("0/1 running", Fleet.loadText(null, null))
        assertEquals("0/1 running", Fleet.loadText(0, 0))
    }

    @Test fun lastSeenText() {
        val now = nowMsAt(10_000.0)
        assertEquals("just now", Fleet.lastSeenText(10_000.0 - 3, now))
        assertEquals("seen 30s ago", Fleet.lastSeenText(10_000.0 - 30, now))
        assertEquals("seen 1m ago", Fleet.lastSeenText(10_000.0 - 90, now))
        assertEquals("seen 2h ago", Fleet.lastSeenText(10_000.0 - 7_200, now))
        assertEquals("seen 2d ago", Fleet.lastSeenText(10_000.0 - 200_000, now))
        assertEquals("never seen", Fleet.lastSeenText(null, now))
        // Clock skew (heartbeat ahead of the phone) clamps to "just now".
        assertEquals("just now", Fleet.lastSeenText(10_000.0 + 60, now))
    }

    // ---- display sort -------------------------------------------------------------

    @Test fun sortRanksAndTieBreaks() {
        fun w(id: String, name: String, status: String) =
            Worker(id = id, name = name, status = status, lastSeen = null)
        val sorted = Fleet.sortedForDisplay(
            listOf(
                w("1", "zeta", "offline"),
                w("2", "beta", "idle"),
                w("3", "alpha", "idle"),
                w("4", "mike", "busy"),
                w("5", "sierra", "stale"),
                w("6", "quark", "rebooting"),   // unknown status: above offline
            ),
        )
        assertEquals(
            listOf("mike", "alpha", "beta", "sierra", "quark", "zeta"),
            sorted.map { it.name },
        )
        // Equal rank + name: id breaks the tie (total order, no row jumping).
        val dup = Fleet.sortedForDisplay(
            listOf(w("b", "same", "idle"), w("a", "same", "idle")),
        )
        assertEquals(listOf("a", "b"), dup.map { it.id })
    }

    @Test fun sortFixture() {
        val sorted = Fleet.sortedForDisplay(fixtureWorkers())
        assertEquals(
            listOf("fixture-node", "fixture-gpu-node", "fixture-offline-node"),
            sorted.map { it.name },
        )
    }
}
