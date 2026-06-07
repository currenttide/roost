package oss.roost.mobile.model

/**
 * The dashboard staleness guard (DESIGN.md §2 / API.md §2): if the `/derived`
 * payload's `generated_at` drifts more than [THRESHOLD_SEC] behind the wall
 * clock, the UI shows a "data N s old" pill instead of silently rendering a
 * stale frame — the same lesson as the mac panel's stale-render bug.
 *
 * WHY a pure function (no android.*): this is the single source of truth for the
 * decision, shared by `ui/common/Format.staleness` and exercised by the Linux
 * JVM harness. The R75 bug was NOT in this arithmetic — it was that the dashboard
 * read the wall clock once per recomposition and `MutableStateFlow` deduped
 * repeated identical failure states, so `now` never advanced and `ageSec` froze
 * below the threshold. The fix drives `now` from a 1 s ticker; this function
 * stays the honest, testable core that the ticker feeds.
 */
object Staleness {

    /** Show the pill once data is older than this many seconds. */
    const val THRESHOLD_SEC: Double = 10.0

    /**
     * Pill text when the data is stale, else null.
     *
     * @param generatedAt the payload's `generated_at` (epoch seconds, fractional).
     * @param nowMs the current wall clock in epoch millis (System.currentTimeMillis()).
     */
    fun pillText(generatedAt: Double, nowMs: Long): String? {
        val ageSec = (nowMs / 1000.0) - generatedAt
        return if (ageSec > THRESHOLD_SEC) "data ${ageSec.toInt()}s old" else null
    }
}
