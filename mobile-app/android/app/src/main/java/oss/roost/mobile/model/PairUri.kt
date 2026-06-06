package oss.roost.mobile.model

/**
 * Pure-Kotlin decoder for roost://pair?d=<base64url(JSON, padding stripped)> (API.md §1).
 *
 * android.* free so it is unit-testable. java.util.Base64 is JDK/Android-26+ safe.
 */
object PairUri {

    /**
     * Decode a full pairing URI (or a bare base64url `d` value, or even raw JSON) into a
     * PairPayload. Restores stripped base64 padding before decoding. Throws on malformed
     * input or v>1 (Parsers.PairVersionException), which the caller surfaces to the user.
     */
    fun decode(input: String): PairPayload {
        val raw = input.trim()
        val d = extractD(raw)
        // If it already looks like JSON (manual paste of the inner doc), use it directly.
        val json = if (looksLikeJson(d)) d else String(decodeBase64Url(d), Charsets.UTF_8)
        return Parsers.parsePairPayload(json)
    }

    private fun looksLikeJson(s: String): Boolean = s.trimStart().startsWith("{")

    /** Pull the `d` query value out of a roost://pair?d=… URI; otherwise return as-is. */
    private fun extractD(s: String): String {
        val scheme = "roost://pair"
        if (!s.startsWith(scheme)) return s
        val q = s.substringAfter('?', "")
        for (pair in q.split('&')) {
            val eq = pair.indexOf('=')
            if (eq > 0 && pair.substring(0, eq) == "d") {
                return urlDecode(pair.substring(eq + 1))
            }
        }
        return s
    }

    /** Restore '=' padding to a length multiple of 4 (API.md: pad by `len % 4`). */
    fun restorePadding(b64: String): String {
        val rem = b64.length % 4
        return if (rem == 0) b64 else b64 + "=".repeat(4 - rem)
    }

    fun decodeBase64Url(b64url: String): ByteArray {
        val padded = restorePadding(b64url)
        // base64url alphabet (-,_). Use the URL decoder; tolerate accidental std (+,/) too.
        return try {
            java.util.Base64.getUrlDecoder().decode(padded)
        } catch (_: IllegalArgumentException) {
            java.util.Base64.getDecoder().decode(padded)
        }
    }

    /** Minimal percent-decode for the query value (handles %XX and '+'). */
    private fun urlDecode(s: String): String {
        if ('%' !in s && '+' !in s) return s
        val out = StringBuilder(s.length)
        var i = 0
        while (i < s.length) {
            val c = s[i]
            when {
                c == '%' && i + 2 < s.length -> {
                    val hex = s.substring(i + 1, i + 3).toIntOrNull(16)
                    if (hex != null) { out.append(hex.toChar()); i += 3 }
                    else { out.append(c); i++ }
                }
                c == '+' -> { out.append(' '); i++ }
                else -> { out.append(c); i++ }
            }
        }
        return out.toString()
    }
}
