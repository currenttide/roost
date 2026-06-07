package oss.roost.mobile.store

import android.content.Context
import android.content.SharedPreferences

/** The paired control plane: url + mobile token. */
data class Pairing(val url: String, val token: String, val name: String?)

/**
 * Persists the pairing (url+token) with the token encrypted via KeystoreBox, plus the
 * non-secret bits in plain prefs. Also holds the per-job SSE cursor and the recent-prompt
 * cache (both non-secret — plain SharedPreferences per API.md §5 "persist max seq" and
 * DESIGN §3.3 "last 10 prompts, stored locally").
 */
class SecureStore(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("roost_mobile", Context.MODE_PRIVATE)
    private val box = KeystoreBox()

    // ---- pairing -----------------------------------------------------------------

    fun savePairing(p: Pairing) {
        prefs.edit()
            .putString(KEY_URL, p.url)
            .putString(KEY_TOKEN_ENC, box.encrypt(p.token))
            .putString(KEY_NAME, p.name)
            .apply()
    }

    fun loadPairing(): Pairing? {
        val url = prefs.getString(KEY_URL, null) ?: return null
        val enc = prefs.getString(KEY_TOKEN_ENC, null) ?: return null
        val token = box.decrypt(enc) ?: return null
        return Pairing(url, token, prefs.getString(KEY_NAME, null))
    }

    fun clearPairing() {
        prefs.edit().remove(KEY_URL).remove(KEY_TOKEN_ENC).remove(KEY_NAME).apply()
    }

    val isPaired: Boolean get() = prefs.contains(KEY_TOKEN_ENC)

    // ---- per-job SSE cursor (max seq) --------------------------------------------

    fun loadCursor(jobId: String): Int = prefs.getInt(cursorKey(jobId), 0)

    fun saveCursor(jobId: String, seq: Int) {
        // Monotonic: never move the cursor backwards.
        if (seq > loadCursor(jobId)) prefs.edit().putInt(cursorKey(jobId), seq).apply()
    }

    // ---- recent prompts (last 10) ------------------------------------------------

    fun recentPrompts(): List<String> {
        val raw = prefs.getString(KEY_RECENTS, "") ?: ""
        return raw.split('\n').filter { it.isNotBlank() }
    }

    fun pushRecentPrompt(prompt: String) {
        val trimmed = prompt.trim()
        if (trimmed.isEmpty()) return
        val list = ArrayList(recentPrompts())
        list.remove(trimmed)         // de-dup, move-to-front
        list.add(0, trimmed)
        while (list.size > 10) list.removeAt(list.size - 1)
        // Newlines separate entries, so collapse any inside a prompt to spaces.
        val encoded = list.joinToString("\n") { it.replace('\n', ' ') }
        prefs.edit().putString(KEY_RECENTS, encoded).apply()
    }

    private fun cursorKey(jobId: String) = "cursor_$jobId"

    // ---- notification topic (R37 / DESIGN.md §6) ---------------------------------
    // Non-secret (a pub/sub channel name, not a token) → plain prefs. Stored as the
    // canonical subscribe URL produced by NtfyTopic.normalize().

    fun loadNotifyTopicUrl(): String? = prefs.getString(KEY_NOTIFY_TOPIC, null)

    fun saveNotifyTopicUrl(url: String) {
        prefs.edit().putString(KEY_NOTIFY_TOPIC, url).apply()
    }

    fun clearNotifyTopicUrl() {
        prefs.edit().remove(KEY_NOTIFY_TOPIC).apply()
    }

    private companion object {
        const val KEY_URL = "cp_url"
        const val KEY_TOKEN_ENC = "cp_token_enc"
        const val KEY_NAME = "cp_name"
        const val KEY_RECENTS = "recent_prompts"
        const val KEY_NOTIFY_TOPIC = "notify_topic_url"
    }
}
