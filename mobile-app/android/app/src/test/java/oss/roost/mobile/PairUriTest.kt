package oss.roost.mobile

import oss.roost.mobile.model.PairUri
import oss.roost.mobile.model.Parsers
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Test

/**
 * Pairing-URI decode round-trip including base64url padding restoration and v>1 rejection
 * (API.md §1). Pure JVM (java.util.Base64), no android.* needed.
 */
class PairUriTest {

    private fun b64url(s: String): String =
        java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(s.toByteArray(Charsets.UTF_8))

    @Test fun roundTripFullUri() {
        val json = """{"v":1,"url":"http://192.168.1.193:8787","token":"rst-mob-abc","name":"yang-iphone"}"""
        val uri = "roost://pair?d=" + b64url(json)
        val p = PairUri.decode(uri)
        assertEquals(1, p.v)
        assertEquals("http://192.168.1.193:8787", p.url)
        assertEquals("rst-mob-abc", p.token)
        assertEquals("yang-iphone", p.name)
    }

    @Test fun paddingRestoredForEveryRemainder() {
        // Craft tokens whose base64 length % 4 hits 2 and 3 (the cases needing padding).
        for (name in listOf("a", "ab", "abc", "abcd", "abcde")) {
            val json = """{"v":1,"url":"http://h:8787","token":"$name"}"""
            val stripped = b64url(json) // encoder already strips padding
            assertEquals(name, PairUri.decode(stripped).token)
        }
    }

    @Test fun restorePaddingMath() {
        assertEquals("YWJj", PairUri.restorePadding("YWJj"))   // len%4==0 unchanged
        assertEquals("YQ==", PairUri.restorePadding("YQ"))     // +2
        assertEquals("YWI=", PairUri.restorePadding("YWI"))    // +1
    }

    @Test fun bareBase64AndRawJsonBothDecode() {
        val json = """{"v":1,"url":"http://h:8787","token":"t"}"""
        assertEquals("t", PairUri.decode(b64url(json)).token) // bare d
        assertEquals("t", PairUri.decode(json).token)         // raw JSON paste
    }

    @Test fun rejectsFutureVersion() {
        val json = """{"v":2,"url":"http://h:8787","token":"t"}"""
        try {
            PairUri.decode(b64url(json))
            fail("v>1 must be rejected")
        } catch (e: Parsers.PairVersionException) {
            assertEquals(2, e.version)
        }
    }

    @Test fun trailingSlashTrimmedFromUrl() {
        val json = """{"v":1,"url":"http://h:8787/","token":"t"}"""
        assertEquals("http://h:8787", PairUri.decode(b64url(json)).url)
    }

    @Test fun garbageThrows() {
        try {
            PairUri.decode("roost://pair?d=%%%not-base64-or-json")
            fail("garbage must throw")
        } catch (e: Exception) {
            // expected: either base64 or JSON parse failure
        }
    }
}
