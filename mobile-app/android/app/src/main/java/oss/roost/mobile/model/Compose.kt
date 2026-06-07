package oss.roost.mobile.model

/**
 * Pure helpers for the session follow-up composer (DESIGN §3.2, API.md §4). The
 * server is authoritative — `POST /jobs/{id}/input` 400s empty text and 413s a
 * body over 64 KiB — but we validate the SAME rules on the phone so the Send
 * button only enables when the server will accept it, and the user gets an
 * instant reason otherwise. Android-free (no android.*), so the JVM/kotlinc
 * harness exercises it exactly like the iOS `Composer` (Foundation-only) layer.
 *
 * Pinned to `roost/server.py::send_job_input`:
 *   - empty `text` (after the server's truthiness check) → 400.
 *   - UTF-8 byte length > `JOB_INPUT_MAX_BYTES` (64 KiB) → 413.
 * The server's emptiness check is `if not text`, which rejects "" but ACCEPTS a
 * whitespace-only string; we trim for the Send-enabled gate (a blank message is
 * never useful to send) while measuring the UNtrimmed payload for the size cap,
 * since that is exactly the bytes we POST.
 */
object Composer {
    /** The server's `JOB_INPUT_MAX_BYTES` (64 KiB) — over this → 413. */
    const val MAX_BYTES: Int = 64 * 1024

    /** UTF-8 byte length of the message exactly as it would be POSTed. */
    fun byteLength(text: String): Int = text.toByteArray(Charsets.UTF_8).size

    /**
     * True iff `text` would be accepted by `POST /input`: non-blank after trim
     * (so Send is disabled for an all-whitespace draft) and within the byte cap.
     * Gates the Send button so an invalid message never round-trips.
     */
    fun canSend(text: String): Boolean {
        if (text.trim().isEmpty()) return false
        return byteLength(text) <= MAX_BYTES
    }

    /**
     * A friendly reason the current draft can't be sent, or null when it's valid
     * (or merely empty — empty = no message yet, just a disabled button, no error).
     * Distinguishes the two server rejections (400 empty vs 413 too-large).
     */
    fun validationMessage(text: String): String? {
        if (text.trim().isEmpty()) return null
        if (byteLength(text) > MAX_BYTES) return "Message too long (max 64 KB)."
        return null
    }

    /**
     * One-line outcome for a posted input given its delivery [JobInput.state] and
     * `detail`, mirroring the CLI's `roost send` reporting (cli.py). `delivered` →
     * a checkmark; `dropped` → the honest reason (agent/docker jobs run with stdin
     * closed); `queued` → still waiting on the worker. Mirrors iOS `Composer.outcome`.
     */
    fun outcome(state: String, detail: String?): String = when (state) {
        "delivered" -> "Delivered ✓ (${detail ?: "to process"})"
        "dropped" -> "Dropped — ${detail ?: "undeliverable"}"
        else -> "Queued — waiting for the worker to deliver it"
    }
}
