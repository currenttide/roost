package oss.roost.mobile.ui.publish

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.BundleCheck
import oss.roost.mobile.model.PublishError
import oss.roost.mobile.model.PublishSizeGuard
import oss.roost.mobile.model.PublishSlug
import oss.roost.mobile.model.Site
import oss.roost.mobile.net.ApiClient

data class PublishUiState(
    /** Display name of the chosen bundle (seeds the slug default + the row). */
    val fileName: String? = null,
    /** Byte size of the chosen bundle, for the size row + the cap guard. */
    val fileSize: Long = 0,
    /** Site name → slug. Seeded from the filename; user-editable. */
    val name: String = "",
    /** The published site once the one-shot call returns (the result card). */
    val site: Site? = null,
    val publishing: Boolean = false,
    val error: String? = null,
) {
    /** Slug the server will store, previewed live from `name` (API.md §6a). */
    val slugPreview: String get() = PublishSlug.normalize(name)

    /** Whether the typed name yields a slug the server will accept. */
    val nameValid: Boolean get() = PublishSlug.isValid(name)

    /** True once a (gzip, in-cap) bundle is loaded — drives the Name section. */
    val hasBundle: Boolean get() = fileName != null && fileSize > 0

    /**
     * Publish is allowed once a bundle is loaded, the name yields a valid slug,
     * and we're not mid-flight (mirrors iOS `PublishStore.canPublish`).
     */
    val canPublish: Boolean get() = hasBundle && nameValid && !publishing
}

/**
 * Backs the publish-a-site sheet (API.md §6, production north star #3). Holds the
 * picked `tar.gz` bytes, derives a default slug from its filename, and ships it
 * with the ONE-SHOT call (`publishBundle`) — preferred on the phone because
 * nothing is staged, so a dropped connection can't leave a dangling blob (§6a).
 * The resulting [Site] carries the live URL the UI shows + shares.
 *
 * All slug/bundle/size/error logic lives in the pure `model` layer
 * (`PublishSlug`/`BundleCheck`/`PublishSizeGuard`/`PublishError`, JVM-tested);
 * this ViewModel is the Android orchestration around the SAF picker + client,
 * mirroring iOS `PublishStore`.
 */
class PublishViewModel(private val container: AppContainer) : ViewModel() {

    private val _state = MutableStateFlow(PublishUiState())
    val state: StateFlow<PublishUiState> = _state

    /** Raw bundle bytes held until publish; never put in UI state. */
    private var bytes: ByteArray? = null

    fun setName(s: String) {
        _state.value = _state.value.copy(name = s, error = null)
    }

    /**
     * Accept a bundle the SAF document picker handed us (already read to bytes by
     * the screen, which owns the ContentResolver). Sniffs the gzip magic (the
     * one-shot endpoint 400s a non-tar.gz body) and the size cap, then proposes a
     * slug from the filename. Surfaces a friendly error rather than throwing.
     */
    fun loadBundle(fileName: String, data: ByteArray) {
        if (!BundleCheck.looksLikeGzip(data)) {
            bytes = null
            _state.value = _state.value.copy(
                fileName = null, fileSize = 0, site = null,
                error = "Pick a .tar.gz bundle (this file isn't gzip).",
            )
            return
        }
        if (!PublishSizeGuard.isWithinCap(data.size.toLong())) {
            bytes = null
            _state.value = _state.value.copy(
                fileName = null, fileSize = 0, site = null,
                error = "Bundle is too large to publish.",
            )
            return
        }
        bytes = data
        val current = _state.value
        // Only seed the name if the user hasn't already typed one.
        val seeded = if (current.name.isBlank()) PublishSlug.suggestion(fileName) else current.name
        _state.value = current.copy(
            fileName = fileName,
            fileSize = data.size.toLong(),
            name = seeded,
            site = null,
            error = null,
        )
    }

    /** Surface a picker/read failure inline (the screen couldn't open the file). */
    fun fileError(message: String) {
        _state.value = _state.value.copy(error = message)
    }

    /**
     * Ship the bundle one-shot. On success stores the [Site] (the screen shows
     * its URL + a share affordance). Error handling goes through the pure
     * `PublishError.map`: 401 → unpair + drop to pairing; 403 → show, stay
     * paired; 413 → too large; 400/other → detail (API.md §1/§6a).
     */
    fun publish() {
        val s = _state.value
        val data = bytes
        if (data == null || !s.nameValid || s.publishing) {
            if (data != null && !s.nameValid) {
                _state.value = s.copy(
                    error = "Name must be lowercase letters, numbers, or hyphens (≤40).",
                )
            }
            return
        }
        _state.value = s.copy(publishing = true, error = null)
        viewModelScope.launch {
            try {
                val site = container.api.publishBundle(name = s.name, bytes = data)
                _state.value = _state.value.copy(publishing = false, site = site)
            } catch (e: ApiClient.ApiException) {
                val mapped = PublishError.map(e.status, e.detail)
                if (mapped.unpair) container.unpair()
                _state.value = _state.value.copy(publishing = false, error = mapped.message)
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    publishing = false,
                    error = e.message ?: "Publish failed.",
                )
            }
        }
    }
}
