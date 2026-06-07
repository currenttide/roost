package oss.roost.mobile.model

/**
 * The job-kind segment of a session/dashboard subtitle (R85).
 *
 * WHY a pure function (no android.*): the session header and the dashboard row
 * both showed a HARDCODED "claude" segment, so a plain `command` job rendered
 * "… · claude · succeeded" (user-testing/android/05,08). The honest label is the
 * job's effective kind, which the server now reports on every run row as `kind`
 * (API.md §2). This is the single source of truth for that segment, shared by
 * `ui/session.sessionSubtitle` and `ui/dashboard.subtitle` and exercised by the
 * Linux JVM harness (the ui/ package is not compiled there).
 *
 * Honesty: an older control plane omits `kind` (null) — we then DROP the segment
 * rather than guess. Showing nothing is truthful; showing "claude" for a command
 * job is the bug.
 */
object Subtitle {

    /** The kind label to show, or null when the kind is unknown (omit the segment). */
    fun kindSegment(kind: String?): String? =
        kind?.trim()?.takeIf { it.isNotEmpty() }
}
