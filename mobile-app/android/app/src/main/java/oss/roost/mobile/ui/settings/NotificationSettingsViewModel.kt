package oss.roost.mobile.ui.settings

import androidx.lifecycle.ViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.NtfyTopic

data class NotifySettingsUiState(
    val input: String = "",
    val savedUrl: String? = null,
    val error: String? = null,
) {
    /** Live preview of the normalized subscribe URL (null until valid). */
    val preview: String? get() = NtfyTopic.normalize(input)
    val canSave: Boolean get() = preview != null
}

/**
 * Notification-settings VM (R37 / DESIGN.md §6 v1.1). Holds the ntfy topic the app
 * subscribes to for terminal-job pushes. The control plane is configured with
 * `--notify-url` and POSTs there on terminal jobs; it does NOT expose that topic
 * over the API, so this is a manual SETTING (DESIGN.md §6). The pure
 * normalize/validate is in [NtfyTopic] (android-free, JVM-tested); this VM is the
 * thin persistence around it via the existing SecureStore.
 */
class NotificationSettingsViewModel(private val container: AppContainer) : ViewModel() {

    private val _state = MutableStateFlow(NotifySettingsUiState())
    val state: StateFlow<NotifySettingsUiState> = _state

    init {
        val saved = container.store.loadNotifyTopicUrl()
        _state.value = NotifySettingsUiState(
            // Seed the field with the bare topic for friendly editing.
            input = saved?.let { NtfyTopic.displayTopic(it) } ?: "",
            savedUrl = saved,
        )
    }

    fun onInputChange(s: String) {
        _state.value = _state.value.copy(input = s, error = null)
    }

    /** Persist the normalized topic URL, or set an inline error if it's invalid. */
    fun save() {
        val url = NtfyTopic.normalize(_state.value.input)
        if (url == null) {
            _state.value = _state.value.copy(
                error = "Enter an ntfy topic (e.g. roost-yang) or a full https://ntfy.sh/… URL.",
            )
            return
        }
        container.store.saveNotifyTopicUrl(url)
        _state.value = _state.value.copy(savedUrl = url, error = null)
    }

    /** Forget the configured topic (stop watching). */
    fun clear() {
        container.store.clearNotifyTopicUrl()
        _state.value = NotifySettingsUiState()
    }
}
