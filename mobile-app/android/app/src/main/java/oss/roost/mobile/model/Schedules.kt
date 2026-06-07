package oss.roost.mobile.model

/**
 * Pure helpers for the interval-schedule flow (API.md §7). Android-free (no
 * android.*), so the JVM/kotlinc harness exercises them exactly like the iOS
 * `ScheduleInterval`/`ScheduleListReducer` (Foundation-only) layer they mirror.
 *
 * The server is authoritative — it parses `every` and 400s an unparseable value
 * or one below the 30 s floor — but we parse/validate the SAME grammar on the
 * phone so the Create button only enables when the server will accept it, and we
 * format `interval_sec` back to the compact `30s`/`5m`/`6h`/`1d` display the CLI
 * uses. The grammar is pinned to `roost/server.py`:
 *   - `parse_every`  — `^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$` (lowercased first), or
 *     a bare number → seconds; multipliers s=1 m=60 h=3600 d=86400.
 *   - `SCHEDULE_MIN_INTERVAL_SEC = 30.0` — `every` must be `>= 30`.
 *   - `_fmt_interval` (cli.py) — d/h/m when `sec >= div && sec % div == 0`, else `<sec>s`.
 */
object ScheduleInterval {
    /** The server's 30 s floor (`SCHEDULE_MIN_INTERVAL_SEC`). Below this → 400. */
    const val MIN_SECONDS: Double = 30.0

    /** Multipliers for the single trailing unit, matching the server's table. */
    private val UNITS = mapOf('s' to 1.0, 'm' to 60.0, 'h' to 3600.0, 'd' to 86400.0)

    /** The unit-suffix grammar from `server.py::_EVERY_RE` (matched case-insensitively). */
    private val EVERY = Regex("""^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$""")

    /**
     * Parse `every` exactly as the server's `parse_every` does (the STRING path:
     * a unit-suffixed value or a bare numeric string). Returns the interval in
     * seconds, or null when it can't be parsed (the caller maps null → "invalid").
     *
     * String grammar only — the phone always sends `every` as a string (§7a), so
     * the server's number/bool branches are never exercised here. A bare numeric
     * string ("90") still parses, matching `parse_every`'s `float(every)` fallback.
     */
    fun parse(every: String): Double? {
        val lowered = every.lowercase()
        EVERY.matchEntire(lowered)?.let { m ->
            val value = m.groupValues[1].toDoubleOrNull() ?: return null
            val unit = m.groupValues[2].firstOrNull() ?: return null
            val mult = UNITS[unit] ?: return null
            return value * mult
        }
        // No unit suffix: fall back to a bare number, like `float(every)`.
        return lowered.trim().toDoubleOrNull()
    }

    /**
     * True iff `every` parses AND is at or above the 30 s floor — i.e. the exact
     * condition under which `POST /schedules` returns 200 for the interval. Gates
     * the Create button so an invalid interval never round-trips.
     */
    fun isValid(every: String): Boolean {
        val sec = parse(every) ?: return false
        return sec >= MIN_SECONDS
    }

    /**
     * A friendly reason the current `every` is rejected, or null when it's valid.
     * Distinguishes "can't parse" from "below the floor" (the server returns
     * distinct 400s for the two). Empty input returns null (just disabled, no msg).
     */
    fun validationMessage(every: String): String? {
        if (every.trim().isEmpty()) return null
        val sec = parse(every) ?: return "Use seconds or <N>[smhd] — e.g. 30s, 15m, 6h, 1d."
        if (sec < MIN_SECONDS) return "Minimum interval is 30s."
        return null
    }

    /**
     * Compact human interval from a second count — byte-for-byte the CLI's
     * `_fmt_interval` (`30s` / `5m` / `6h` / `1d`): prefer the largest whole unit.
     */
    fun format(sec: Double): String {
        for ((unit, div) in listOf('d' to 86400.0, 'h' to 3600.0, 'm' to 60.0)) {
            if (sec >= div && sec % div == 0.0) {
                return "${(sec / div).toInt()}$unit"
            }
        }
        return "${sec.toInt()}s"
    }

    /**
     * Relative "next run in …" / "ran … ago" label from an absolute epoch and a
     * reference now. Returns null for a null timestamp. A due/overdue next-run
     * reads as "now" rather than a negative interval (the tick fires imminently).
     */
    fun relative(epoch: Double?, now: Double): String? {
        if (epoch == null) return null
        val delta = epoch - now
        if (delta <= 0) {
            val ago = -delta
            return if (ago < 1) "now" else "${format(Math.round(ago).toDouble())} ago"
        }
        return "in ${format(Math.round(delta).toDouble())}"
    }
}

/**
 * Interval presets the Create sheet offers as quick chips, mirroring the cadences
 * `roost schedule --every` documents (30m / 6h / 1d) plus the floor and a couple
 * of common middles. Each is a valid `every` string by construction. Mirrors iOS
 * `ScheduleIntervalPreset`.
 */
object ScheduleIntervalPreset {
    val ALL: List<String> = listOf("30s", "5m", "15m", "30m", "1h", "6h", "12h", "1d")
}

/**
 * Pure state reducer for the schedules list (API.md §7b–§7d). Keeping the
 * list-mutation logic out of the ViewModel makes the toggle/delete/upsert
 * transitions testable on the JVM, mirroring how the publish/notify flows pushed
 * their decisions into the pure layer (and iOS `ScheduleListReducer`).
 *
 * The server returns the full updated [Schedule] on create and on PATCH, and a
 * `{deleted, id}` ack on DELETE — so the reducer takes the authoritative object
 * (or id) and produces the next list, never inventing fields.
 */
object ScheduleListReducer {
    /**
     * Replace the schedule with the same `id` (e.g. after a toggle returns the
     * updated object), preserving list order. If no row matches, the list is
     * returned unchanged — a PATCH for an id we don't have shouldn't fabricate one.
     */
    fun upsertExisting(list: List<Schedule>, updated: Schedule): List<Schedule> =
        if (list.any { it.id == updated.id }) {
            list.map { if (it.id == updated.id) updated else it }
        } else {
            list
        }

    /**
     * Insert a freshly-created schedule at the front (the list is newest-first per
     * §7b), de-duplicating by id so a create that races a refresh can't double it.
     */
    fun prepend(list: List<Schedule>, created: Schedule): List<Schedule> =
        listOf(created) + list.filter { it.id != created.id }

    /** Drop the schedule with `id` after a successful DELETE. */
    fun remove(list: List<Schedule>, id: String): List<Schedule> =
        list.filter { it.id != id }
}
