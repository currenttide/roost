package oss.roost.mobile

import oss.roost.mobile.model.BundleCheck
import oss.roost.mobile.model.PublishError
import oss.roost.mobile.model.PublishSizeGuard
import oss.roost.mobile.model.PublishSlug
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-logic tests for the publish flow's slug derivation/validation, the gzip
 * sniff, the file-size guard, and the error mapping (API.md §6). All android-free
 * so they run on the kotlinc + JUnitCore Linux harness. Mirrors the iOS
 * `PublishTests` (RoostTests/PublishTests.swift) scenarios and the server's
 * `normalize_slug` (`roost/publish.py`): lowercase, spaces→`-`, validate
 * `^[a-z0-9][a-z0-9-]{0,39}$`.
 */
class PublishTest {

    // ---- normalize — same transform the server applies before validating ----

    @Test fun normalizeLowercasesSpacesAndTrims() {
        assertEquals("my-site", PublishSlug.normalize("  My Site  "))
        assertEquals("hello-world", PublishSlug.normalize("Hello World"))
        assertEquals("already-ok", PublishSlug.normalize("already-ok"))
    }

    // ---- isValid — gates the Publish button ----

    @Test fun validSlugs() {
        val good = listOf(
            "a", "site", "my-site", "0", "phone-oneshot",
            "a1-b2-c3", "a".repeat(40),
        )
        for (s in good) assertTrue("expected valid: $s", PublishSlug.isValid(s))
        // A name with spaces is valid because normalize() fixes it first.
        assertTrue(PublishSlug.isValid("My Site"))
    }

    @Test fun invalidSlugs() {
        val bad = listOf(
            "",                 // empty
            "   ",              // whitespace only
            "-leading",         // can't start with hyphen
            "Has_Underscore",   // underscore not allowed
            "dot.name",         // dot not allowed
            "slash/name",       // slash not allowed
            "über",             // non-ascii
            "a".repeat(41),     // one over the 40 window
        )
        for (s in bad) assertFalse("expected invalid: $s", PublishSlug.isValid(s))
    }

    @Test fun validityMatchesServerRegexAcrossLengths() {
        // The grammar is start char + up to 39 more = 1..40 total.
        assertTrue(PublishSlug.isValid("x".repeat(1)))
        assertTrue(PublishSlug.isValid("x".repeat(40)))
        assertFalse(PublishSlug.isValid("x".repeat(41)))
    }

    // ---- suggestion — default slug proposed from the picked filename ----

    @Test fun suggestionStripsTarSuffixes() {
        assertEquals("my-site", PublishSlug.suggestion("my-site.tar.gz"))
        assertEquals("my-site", PublishSlug.suggestion("my-site.tgz"))
        assertEquals("my-site", PublishSlug.suggestion("my-site.tar"))
        // Case-insensitive suffix match.
        assertEquals("site", PublishSlug.suggestion("Site.TAR.GZ"))
    }

    @Test fun suggestionCoercesIllegalCharacters() {
        // Spaces → hyphens; underscores/dots → hyphens; runs collapse; ends trim.
        assertEquals("my-site", PublishSlug.suggestion("My Site.tar.gz"))
        assertEquals("my-cool-site", PublishSlug.suggestion("my_cool_site.tar.gz"))
        assertEquals("weird", PublishSlug.suggestion("__weird__.tar.gz"))
        assertEquals("a-b", PublishSlug.suggestion("a...b.tar.gz"))
    }

    @Test fun suggestionTruncatesToWindow() {
        val long = "a".repeat(60) + ".tar.gz"
        val s = PublishSlug.suggestion(long)
        assertEquals(40, s.length)
        assertTrue(PublishSlug.isValid(s))
    }

    @Test fun suggestionEmptyWhenNothingSurvives() {
        // No alnum to keep → no proposal; the UI then asks for a name.
        assertEquals("", PublishSlug.suggestion("___.tar.gz"))
        assertEquals("", PublishSlug.suggestion(".tar.gz"))
    }

    @Test fun suggestionsAreAlwaysValidOrEmpty() {
        val names = listOf(
            "index.tar.gz", "My Portfolio Site.tgz", "v2.0-release.tar",
            "weird ___ name.tar.gz", "résumé.tar.gz",
        )
        for (name in names) {
            val s = PublishSlug.suggestion(name)
            assertTrue("suggestion for $name was $s", s.isEmpty() || PublishSlug.isValid(s))
        }
    }

    // ---- gzip sniff — pre-reject an obviously-wrong pick before upload ----

    @Test fun looksLikeGzipMagic() {
        // Real gzip streams start with 1f 8b.
        assertTrue(BundleCheck.looksLikeGzip(byteArrayOf(0x1f, 0x8b.toByte(), 0x08, 0x00)))
    }

    @Test fun rejectsNonGzip() {
        assertFalse(BundleCheck.looksLikeGzip(byteArrayOf()))                   // empty
        assertFalse(BundleCheck.looksLikeGzip(byteArrayOf(0x1f)))               // 1 byte
        assertFalse(BundleCheck.looksLikeGzip(byteArrayOf(0x50, 0x4b)))         // zip ("PK")
        assertFalse(BundleCheck.looksLikeGzip("<html>".toByteArray()))          // html
    }

    // ---- size guard — pre-reject a too-big pick (mirrors SITE_MAX_BYTES) ----

    @Test fun sizeGuardAcceptsWithinCap() {
        assertTrue(PublishSizeGuard.isWithinCap(1))
        assertTrue(PublishSizeGuard.isWithinCap(1_000_000))
        assertTrue(PublishSizeGuard.isWithinCap(PublishSizeGuard.MAX_BYTES))   // exactly at cap
    }

    @Test fun sizeGuardRejectsEmptyAndOversize() {
        assertFalse(PublishSizeGuard.isWithinCap(0))                            // empty body → 400
        assertFalse(PublishSizeGuard.isWithinCap(PublishSizeGuard.MAX_BYTES + 1))
        assertFalse(PublishSizeGuard.isWithinCap(Long.MAX_VALUE))
    }

    @Test fun sizeGuardCapMatchesServer() {
        // Mirror of roost/publish.py::SITE_MAX_BYTES (256 MiB).
        assertEquals(256L * 1024 * 1024, PublishSizeGuard.MAX_BYTES)
    }

    // ---- error mapping — 401→pairing / 403 / 413 / 400 (mirrors PublishStore) ----

    @Test fun errorMap401DropsToPairing() {
        val m = PublishError.map(401, "invalid bearer token")
        assertTrue(m.unpair)
    }

    @Test fun errorMap403StaysPaired() {
        val m = PublishError.map(403, "admin auth required")
        assertFalse(m.unpair)
        assertTrue(m.message.contains("admin auth required"))
    }

    @Test fun errorMap413IsSizeMessage() {
        val m = PublishError.map(413, "bundle exceeds 268435456 bytes uncompressed")
        assertFalse(m.unpair)
        assertEquals("Bundle is too large to publish.", m.message)
    }

    @Test fun errorMap400AndOtherShowDetail() {
        val bad = PublishError.map(400, "not a valid tar.gz bundle")
        assertFalse(bad.unpair)
        assertTrue(bad.message.contains("not a valid tar.gz bundle"))
        // Any other status falls through to the same generic shape.
        val other = PublishError.map(500, "boom")
        assertFalse(other.unpair)
        assertTrue(other.message.contains("boom"))
    }
}
