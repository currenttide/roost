package oss.roost.mobile.store

import android.content.Context
import java.io.File

/**
 * On-device offline cache (DESIGN §5): the last good /derived body plus one
 * capped log file per job, so the app renders last-known state (with the
 * staleness pill) when the control plane is unreachable.
 *
 * Raw response strings are cached, not parsed models — load goes back through
 * the same tolerant Parsers, so the cache can never drift from the contract.
 * App-private storage; cleared on unpair (the fleet's goals/logs shouldn't
 * outlive the pairing).
 */
class OfflineCache(context: Context) {

    private val dir: File = File(context.filesDir, "roost_cache").apply { mkdirs() }

    // ---- dashboard ----------------------------------------------------------------

    fun saveDerived(rawJson: String) = runQuiet { File(dir, DERIVED).writeText(rawJson) }

    fun loadDerived(): String? = runQuiet { File(dir, DERIVED).takeIf { it.exists() }?.readText() }

    // ---- per-job logs (encoded by sse.LogCache) -------------------------------------

    fun saveLogs(jobId: String, encoded: String) = runQuiet {
        File(dir, logName(jobId)).writeText(encoded)
        prune()
    }

    fun loadLogs(jobId: String): String? =
        runQuiet { File(dir, logName(jobId)).takeIf { it.exists() }?.readText() }

    /** Wipe everything (called on unpair). */
    fun clear() = runQuiet { dir.listFiles()?.forEach { it.delete() }; Unit }

    /** Keep the newest [MAX_LOG_FILES] log files; jobs age out naturally. */
    private fun prune() {
        val logs = dir.listFiles { f -> f.name.startsWith(LOG_PREFIX) } ?: return
        if (logs.size <= MAX_LOG_FILES) return
        logs.sortedBy { it.lastModified() }
            .take(logs.size - MAX_LOG_FILES)
            .forEach { it.delete() }
    }

    // File ids are server-issued hex, but sanitize anyway — never trust a path part.
    private fun logName(jobId: String) = LOG_PREFIX + jobId.filter { it.isLetterOrDigit() } + ".json"

    /** Cache I/O must never take the app down; a miss is just a cold cache. */
    private inline fun <T> runQuiet(block: () -> T): T? = try {
        block()
    } catch (_: Exception) {
        null
    }

    private companion object {
        const val DERIVED = "derived.json"
        const val LOG_PREFIX = "logs_"
        const val MAX_LOG_FILES = 30
    }
}
