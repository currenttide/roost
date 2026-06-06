package oss.roost.mobile.ui.pair

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.PairUri
import oss.roost.mobile.model.Parsers
import oss.roost.mobile.net.ApiClient
import oss.roost.mobile.store.Pairing

data class PairUiState(
    val input: String = "",
    val busy: Boolean = false,
    val error: String? = null,
    val paired: Boolean = false,
)

/**
 * Decodes a roost://pair payload (deep link or pasted), probes GET /healthz with the
 * decoded url, and only on success stores the pairing (API.md §1). Any failure is shown
 * inline; nothing is persisted until the probe succeeds.
 */
class PairViewModel(private val container: AppContainer) : ViewModel() {

    private val _state = MutableStateFlow(PairUiState())
    val state: StateFlow<PairUiState> = _state

    fun onInputChange(s: String) {
        _state.value = _state.value.copy(input = s, error = null)
    }

    /** Decode + probe + persist. Works for a full URI, bare `d`, or raw JSON paste. */
    fun submit(raw: String = _state.value.input) {
        if (_state.value.busy) return
        val text = raw.trim()
        if (text.isEmpty()) {
            _state.value = _state.value.copy(error = "Paste a roost:// pairing code")
            return
        }
        _state.value = _state.value.copy(busy = true, error = null)
        viewModelScope.launch {
            val payload = try {
                PairUri.decode(text)
            } catch (e: Parsers.PairVersionException) {
                fail(e.message ?: "Update the app"); return@launch
            } catch (_: Exception) {
                fail("Not a valid pairing code"); return@launch
            }
            // Probe reachability with a throwaway client carrying the new url+token.
            val probe = ApiClient(baseUrl = payload.url, token = payload.token)
            val ok = try {
                probe.healthz().ok
            } catch (e: ApiClient.ApiException) {
                fail("Server rejected pairing (${e.status})"); return@launch
            } catch (_: Exception) {
                fail("Can't reach ${payload.url}"); return@launch
            }
            if (!ok) { fail("Server health check failed"); return@launch }
            container.setPaired(Pairing(payload.url, payload.token, payload.name))
            _state.value = _state.value.copy(busy = false, paired = true)
        }
    }

    private fun fail(msg: String) {
        _state.value = _state.value.copy(busy = false, error = msg)
    }
}
