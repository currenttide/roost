package oss.roost.mobile.model

/**
 * Strip ANSI/VT100 escape sequences from a log line so it renders as clean monospace
 * (API.md §4 "strip ANSI codes"). Pure Kotlin, no regex backtracking risk — a single
 * forward scan that skips CSI (ESC '[' … final-byte) and OSC (ESC ']' … BEL/ST) runs.
 */
object Ansi {
    private const val ESC = '' // ESC
    private const val BEL = '' // BEL (one OSC terminator)

    fun strip(s: String): String {
        if (ESC !in s) return s
        val out = StringBuilder(s.length)
        var i = 0
        while (i < s.length) {
            val c = s[i]
            if (c == ESC && i + 1 < s.length) {
                when (s[i + 1]) {
                    '[' -> {
                        // CSI: skip until a final byte in @–~ (0x40..0x7E).
                        i += 2
                        while (i < s.length && s[i] !in '@'..'~') i++
                        if (i < s.length) i++ // consume the final byte
                    }
                    ']' -> {
                        // OSC: skip until BEL or ESC '\' (ST).
                        i += 2
                        while (i < s.length && s[i] != BEL &&
                            !(s[i] == ESC && i + 1 < s.length && s[i + 1] == '\\')) i++
                        if (i < s.length && s[i] == ESC) i++ // ST's backslash
                        if (i < s.length) i++
                    }
                    else -> i += 2 // two-char escape (e.g. ESC c) — drop both
                }
            } else {
                out.append(c); i++
            }
        }
        return out.toString()
    }
}
