package oss.roost.mobile.sse

/**
 * Projects the buffered `RenderedLine`s into the rows the session view shows,
 * for the current raw/distilled toggle (R109). PURE so the toggle's behaviour is
 * unit-tested on the Linux harness, separate from the Composable.
 *
 *  - DISTILLED (the DEFAULT, `showRaw == false`): keep only lines whose
 *    `distilled` is non-null (the cross-platform `DistilledLine` transform
 *    suppressed the rest — event noise, thinking/signature blobs, rate-limit
 *    pings), and show that distilled text. Matches `roost stream` (no flag).
 *  - RAW (`showRaw == true`): show every line's raw `text` (ANSI-stripped;
 *    event rows render as dividers). Matches `roost stream --verbose`.
 */
object SessionLines {

    fun forDisplay(lines: List<RenderedLine>, showRaw: Boolean): List<RenderedLine> =
        if (showRaw) {
            lines
        } else {
            lines.mapNotNull { l ->
                val d = l.distilled ?: return@mapNotNull null
                // Render the distilled text via `text` so the row Composable stays
                // a single field; distilled rows are never EVENT dividers.
                l.copy(text = d, kind = RenderedLine.Kind.STDOUT)
            }
        }
}
