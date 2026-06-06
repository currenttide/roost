package oss.roost.mobile.model

/**
 * Map health.status → a glyph for the dashboard/session UI (API.md §2 closed enum).
 * Unknown values fall through to the raw text with no glyph — "render, don't crash" (§7).
 */
object HealthGlyph {

    enum class Tone { GOOD, WARN, BAD, NEUTRAL, ACTIVE }

    data class Mapping(val glyph: String, val tone: Tone, val knownStatus: Boolean)

    fun map(status: String): Mapping = when (status) {
        "verified", "done" -> Mapping("✓", Tone.GOOD, true)            // ✓
        "failed" -> Mapping("✗", Tone.BAD, true)                       // ✗
        "cancelled" -> Mapping("−", Tone.NEUTRAL, true)               // −
        "running", "verifying", "self-healing" -> Mapping("▶", Tone.ACTIVE, true) // ▶
        "queued" -> Mapping("○", Tone.NEUTRAL, true)                  // ○
        "waiting" -> Mapping("◔", Tone.NEUTRAL, true)                 // ◔
        "unverified", "unplaceable", "stuck?" -> Mapping("⚠", Tone.WARN, true)    // ⚠
        // Unknown: no glyph, neutral tone, knownStatus=false → UI shows plain text.
        else -> Mapping("", Tone.NEUTRAL, false)
    }
}
