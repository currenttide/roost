package oss.roost.mobile

import oss.roost.mobile.model.Ansi
import oss.roost.mobile.model.HealthGlyph
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Health-glyph mapping (incl. unknown-doesn't-crash) and ANSI stripping. */
class MapperTest {

    @Test fun knownStatusesMap() {
        assertEquals("✓", HealthGlyph.map("verified").glyph)
        assertEquals("✓", HealthGlyph.map("done").glyph)
        assertEquals("✗", HealthGlyph.map("failed").glyph)
        assertEquals("−", HealthGlyph.map("cancelled").glyph)
        assertEquals("▶", HealthGlyph.map("running").glyph)
        assertEquals("▶", HealthGlyph.map("self-healing").glyph)
        assertEquals("○", HealthGlyph.map("queued").glyph)
        assertEquals("◔", HealthGlyph.map("waiting").glyph)
        assertEquals("⚠", HealthGlyph.map("unplaceable").glyph)
        assertEquals("⚠", HealthGlyph.map("stuck?").glyph)
        assertTrue(HealthGlyph.map("verified").knownStatus)
    }

    @Test fun unknownStatusDoesNotCrash() {
        val m = HealthGlyph.map("brand_new_server_status")
        assertFalse(m.knownStatus)        // UI falls back to plain text
        assertEquals("", m.glyph)         // no glyph
        assertEquals(HealthGlyph.Tone.NEUTRAL, m.tone)
        // also empty string must be safe.
        assertFalse(HealthGlyph.map("").knownStatus)
    }

    @Test fun ansiStripping() {
        // CSI color codes removed; visible text preserved.
        assertEquals("hello", Ansi.strip("[31mhello[0m"))
        assertEquals("ab", Ansi.strip("a[1;32mb"))
        // No escapes → untouched (fast path).
        assertEquals("plain text", Ansi.strip("plain text"))
        // OSC sequence (title set) terminated by BEL is dropped.
        assertEquals("x", Ansi.strip("]0;window titlex"))
    }
}
