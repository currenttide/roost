package oss.roost.mobile

import oss.roost.mobile.model.Composer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-logic tests for the session follow-up composer (DESIGN §3.2 / API.md §4,
 * R38): the Send-enable gate, the byte-cap, and the outcome line. All android-free
 * so they run on the kotlinc + JUnitCore Linux harness. Mirrors the iOS
 * `ComposerTests`. The 64 KiB cap is a CROSS-CONTRACT pin against the server's
 * `JOB_INPUT_MAX_BYTES` (`roost/server.py`) so client/server drift is caught.
 */
class ComposeTest {

    @Test fun maxBytesMatchesServer() {
        // Pinned to server.py: JOB_INPUT_MAX_BYTES = 64 * 1024.
        assertEquals(64 * 1024, Composer.MAX_BYTES)
    }

    @Test fun canSendGate() {
        assertFalse("empty is rejected (server 400)", Composer.canSend(""))
        assertFalse("whitespace-only is rejected", Composer.canSend("   \n\t "))
        assertTrue("a normal message sends", Composer.canSend("re-run the test suite"))
        assertTrue("leading/trailing space is fine if there's content",
            Composer.canSend("  fix the bug  "))
    }

    @Test fun byteCapUsesUtf8Length() {
        // A multi-byte char counts its UTF-8 bytes, exactly like the server's
        // `len(text.encode("utf-8"))`. "é" is 2 bytes.
        assertEquals(2, Composer.byteLength("é"))
        // At the cap: 64 KiB of ASCII is sendable; one byte over is not.
        val atCap = "a".repeat(Composer.MAX_BYTES)
        assertTrue(Composer.canSend(atCap))
        assertNull(Composer.validationMessage(atCap))
        val overCap = "a".repeat(Composer.MAX_BYTES + 1)
        assertFalse(Composer.canSend(overCap))
        assertEquals("Message too long (max 64 KB).", Composer.validationMessage(overCap))
    }

    @Test fun validationMessageEmptyIsSilent() {
        // Empty draft = no error text, just a disabled button (mirrors iOS).
        assertNull(Composer.validationMessage(""))
        assertNull(Composer.validationMessage("   "))
        assertNull(Composer.validationMessage("a valid message"))
    }

    @Test fun outcomeLines() {
        // command jobs deliver to stdin; agent/docker jobs run with stdin closed
        // and are honestly DROPPED with a reason (API.md §4 delivery semantics).
        assertEquals("Delivered ✓ (to process)", Composer.outcome("delivered", null))
        assertEquals("Delivered ✓ (stdin)", Composer.outcome("delivered", "stdin"))
        assertEquals("Dropped — agent runs with stdin closed",
            Composer.outcome("dropped", "agent runs with stdin closed"))
        assertEquals("Dropped — undeliverable", Composer.outcome("dropped", null))
        assertTrue(Composer.outcome("queued", null).startsWith("Queued"))
    }
}
