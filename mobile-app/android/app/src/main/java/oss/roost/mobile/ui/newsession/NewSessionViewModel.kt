package oss.roost.mobile.ui.newsession

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.Worker
import oss.roost.mobile.net.ApiClient

data class NewSessionUiState(
    val text: String = "",
    val pinWorker: String? = null,   // null = auto-place
    val asCommand: Boolean = false,  // false = claude (agent)
    val workers: List<Worker> = emptyList(),
    val recents: List<String> = emptyList(),
    val busy: Boolean = false,
    val error: String? = null,
)

/**
 * Backs the new-session sheet (DESIGN §3.3). Loads the worker list for the pin picker and
 * recent prompts for one-tap reuse; on dispatch sends ONLY the contract fields (API.md §3)
 * and returns the new job id so the UI can jump into the session.
 */
class NewSessionViewModel(private val container: AppContainer) : ViewModel() {

    private val _state = MutableStateFlow(
        NewSessionUiState(recents = container.store.recentPrompts())
    )
    val state: StateFlow<NewSessionUiState> = _state

    init {
        viewModelScope.launch {
            try {
                _state.value = _state.value.copy(workers = container.api.workers())
            } catch (_: Exception) { /* picker just stays empty; auto-place still works */ }
        }
    }

    fun setText(s: String) { _state.value = _state.value.copy(text = s, error = null) }
    fun appendPartial(s: String) {
        // Live dictation replaces the current uncommitted tail; simplest is to set it as
        // the whole field while listening (the sheet snapshots the pre-listen prefix).
        _state.value = _state.value.copy(text = s)
    }
    fun setPin(workerId: String?) { _state.value = _state.value.copy(pinWorker = workerId) }
    fun setAsCommand(on: Boolean) { _state.value = _state.value.copy(asCommand = on) }

    fun dispatch(onDispatched: (String) -> Unit) {
        val s = _state.value
        val text = s.text.trim()
        if (text.isEmpty()) {
            _state.value = s.copy(error = "Say or type what to do")
            return
        }
        if (s.busy) return
        _state.value = s.copy(busy = true, error = null)
        viewModelScope.launch {
            try {
                val kind = if (s.asCommand) "command" else "claude"
                val job = container.api.submit(
                    intent = text,
                    kind = kind,
                    pinWorker = s.pinWorker,
                    command = if (s.asCommand) text else null,
                )
                container.store.pushRecentPrompt(text)
                _state.value = _state.value.copy(busy = false)
                onDispatched(job.id)
            } catch (e: ApiClient.ApiException) {
                if (e.status == 401) container.unpair()
                _state.value = _state.value.copy(busy = false, error = e.detail)
            } catch (e: Exception) {
                _state.value = _state.value.copy(busy = false, error = e.message ?: "dispatch failed")
            }
        }
    }
}
