package oss.roost.mobile.model

/**
 * Pure presentation layer for the Fleet screen (R121, API.md §2a): status
 * pills, capability summaries, load and last-seen lines, and the display
 * sort. No android.* imports — the Linux JVM harness exercises it — and an
 * exact mirror of iOS `Fleet` (Roost/Net/Fleet.swift): `FleetTest` /
 * `FleetTests.swift` assert the same example strings on both platforms so
 * the two phones can never describe the same fleet differently.
 */
object Fleet {

    // ---- staleness (the R75 pattern, API.md §2a) -----------------------------

    /**
     * Mirror of the server's read-time thresholds (roost/server.py
     * STALE_AFTER / OFFLINE_AFTER): a heartbeat gap >= 45 s reads stale,
     * >= 120 s reads offline. The client re-applies them against its own
     * ticker-driven wall clock so a node that dies while a payload sits in
     * hand degrades honestly on screen instead of staying green.
     */
    const val STALE_AFTER_SEC: Double = 45.0
    const val OFFLINE_AFTER_SEC: Double = 120.0

    /** The row's warning pill; absent (null) when the node is fresh. */
    enum class Pill(val label: String) { STALE("stale"), OFFLINE("offline") }

    /**
     * Decide the pill for a worker row (API.md §2a):
     * 1. the server's word wins in the offline direction (`status: "offline"`
     *    is offline however fresh the payload);
     * 2. otherwise a client-clock heartbeat gap >= 120 s is offline;
     * 3. `status: "stale"` or a gap >= 45 s is stale;
     * 4. else no pill.
     */
    fun pill(status: String, lastSeen: Double?, nowMs: Long): Pill? {
        if (status == "offline") return Pill.OFFLINE
        val gap = lastSeen?.let { (nowMs / 1000.0) - it }
        if (gap != null && gap >= OFFLINE_AFTER_SEC) return Pill.OFFLINE
        if (status == "stale") return Pill.STALE
        if (gap != null && gap >= STALE_AFTER_SEC) return Pill.STALE
        return null
    }

    /**
     * A worker counts as UP only when the server says live (idle|busy) AND the
     * client clock agrees (no stale/offline pill). Unknown statuses render as
     * text but never count as up (API.md §9).
     */
    fun isUp(status: String, lastSeen: Double?, nowMs: Long): Boolean =
        (status == "idle" || status == "busy") && pill(status, lastSeen, nowMs) == null

    /** The screen headline: "3 of 4 up". */
    fun headline(up: Int, total: Int): String = "$up of $total up"

    // ---- capability summary (API.md §2a) -------------------------------------

    /**
     * One glanceable line from the free-form capability map, mirroring the
     * `roost workers` CLI summary: hostname, GPU (VRAM, or the R41 "detection
     * failed" flag — a broken probe is NOT a bare node), arch, CPU count.
     * Unknown keys are ignored (additive contract); null when nothing
     * summarizable is present.
     */
    fun capsSummary(caps: Map<String, Any?>): String? {
        val parts = ArrayList<String>()
        (caps["hostname"] as? String)?.let(parts::add)
        if (caps["gpu_detection"] == "failed") {
            parts.add("gpu: detection failed")
        } else {
            (caps["gpu_vram_gb"] as? Number)?.let { parts.add("gpu ${num(it.toDouble())}GB") }
        }
        (caps["arch"] as? String)?.let(parts::add)
        (caps["cpus"] as? Number)?.let { parts.add("${num(it.toDouble())} cpu") }
        return if (parts.isEmpty()) null else parts.joinToString(" · ")
    }

    /**
     * Integral numbers drop the decimal ("16"), fractional keep it ("30.7") —
     * identical formatting on both platforms.
     */
    private fun num(v: Double): String =
        if (v == Math.rint(v)) v.toLong().toString() else v.toString()

    // ---- load + last-seen lines ----------------------------------------------

    /**
     * "1/4 running" — in-flight jobs against capacity (capacity >= 1 always;
     * older CPs may omit either field → honest zeros).
     */
    fun loadText(running: Int?, capacity: Int?): String =
        "${maxOf(running ?: 0, 0)}/${maxOf(capacity ?: 1, 1)} running"

    /**
     * Compact relative heartbeat age: "just now" (< 5 s), then s/m/h/d.
     * Driven by a 1 s ticker in the screen (R75: the clock must keep advancing
     * even when no new payload arrives).
     */
    fun lastSeenText(lastSeen: Double?, nowMs: Long): String {
        if (lastSeen == null) return "never seen"
        val gap = maxOf((nowMs / 1000.0) - lastSeen, 0.0)
        return when {
            gap < 5 -> "just now"
            gap < 60 -> "seen ${gap.toInt()}s ago"
            gap < 3600 -> "seen ${(gap / 60).toInt()}m ago"
            gap < 86400 -> "seen ${(gap / 3600).toInt()}h ago"
            else -> "seen ${(gap / 86400).toInt()}d ago"
        }
    }

    // ---- display sort ----------------------------------------------------------

    /**
     * Working nodes first, dead ones last, alphabetical within a band:
     * busy(0) < idle(1) < stale(2) < unknown(3) < offline(4).
     */
    fun rank(status: String): Int = when (status) {
        "busy" -> 0
        "idle" -> 1
        "stale" -> 2
        "offline" -> 4
        else -> 3
    }

    /**
     * Sort for the Fleet list: rank, then case-insensitive name, then id
     * (a total order, so rows never jump between equal polls).
     */
    fun sortedForDisplay(workers: List<Worker>): List<Worker> =
        workers.sortedWith(
            compareBy({ rank(it.status) }, { it.displayName.lowercase() }, { it.id })
        )
}
