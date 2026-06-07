package oss.roost.mobile.ui.common

import androidx.compose.ui.graphics.Color
import oss.roost.mobile.model.HealthGlyph
import oss.roost.mobile.model.Staleness
import oss.roost.mobile.ui.Semantic

/** UI-side formatting helpers (pure, no Compose state). */
object Format {

    /** Compact elapsed/duration like "4m 12s", "12m", "1h 03m". `secs` may be fractional. */
    fun duration(secs: Double): String {
        val s = secs.toLong().coerceAtLeast(0)
        val h = s / 3600
        val m = (s % 3600) / 60
        val sec = s % 60
        return when {
            h > 0 -> "${h}h ${m.pad()}m"
            m > 0 -> "${m}m ${sec.pad()}s"
            else -> "${sec}s"
        }
    }

    private fun Long.pad(): String = toString().padStart(2, '0')

    /**
     * "data Ns old" pill text when generated_at lags (API.md §2 staleness guard).
     * Delegates to the pure [Staleness] core so the UI and the Linux harness share
     * a single source of truth (R75).
     */
    fun staleness(generatedAt: Double, nowMs: Long): String? =
        Staleness.pillText(generatedAt, nowMs)

    /** Map a health Tone to a concrete color from the theme. */
    fun toneColor(tone: HealthGlyph.Tone): Color = when (tone) {
        HealthGlyph.Tone.GOOD -> Semantic.good
        HealthGlyph.Tone.BAD -> Semantic.bad
        HealthGlyph.Tone.WARN -> Semantic.warn
        HealthGlyph.Tone.ACTIVE -> Semantic.active
        HealthGlyph.Tone.NEUTRAL -> Color.Unspecified
    }

    fun tokens(n: Int): String = when {
        n >= 1_000_000 -> "%.1fM".format(n / 1_000_000.0)
        n >= 1_000 -> "%.1fk".format(n / 1_000.0)
        else -> n.toString()
    }
}
