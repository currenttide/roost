package oss.roost.mobile.ui.fleet

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.Fleet
import oss.roost.mobile.model.Worker
import oss.roost.mobile.net.ApiClient

data class FleetUiState(
    /** Rows in display order (busy → idle → stale → unknown → offline, then name). */
    val workers: List<Worker> = emptyList(),
    /** True until the first fetch finishes (drives spinner vs empty-state). */
    val loading: Boolean = true,
    val error: String? = null,
)

/**
 * Backs the Fleet screen (R121, API.md §2a): polls GET /workers every 5 s while
 * the screen is STARTED (start/stop driven by the Compose lifecycle — no
 * background networking, DESIGN §7). All judgment (stale/offline pills, caps
 * summary, sort) lives in the pure [Fleet] layer (JVM-tested); this ViewModel
 * is the Android orchestration around the client, mirroring iOS `FleetStore`.
 */
class FleetViewModel(private val container: AppContainer) : ViewModel() {

    private val _state = MutableStateFlow(FleetUiState())
    val state: StateFlow<FleetUiState> = _state

    private var pollJob: Job? = null

    /**
     * Begin/refresh the poll loop. Idempotent. 5 s: workers heartbeat every
     * ~10 s and the staleness bands are 45/120 s, so this keeps the screen
     * honest without hammering the CP from a phone.
     */
    fun start() {
        if (pollJob?.isActive == true) return
        pollJob = viewModelScope.launch {
            while (isActive) {
                refreshOnce()
                delay(5_000)
            }
        }
    }

    fun stop() {
        pollJob?.cancel()
        pollJob = null
    }

    private suspend fun refreshOnce() {
        try {
            val workers = container.api.workers()
            _state.value = FleetUiState(
                workers = Fleet.sortedForDisplay(workers),
                loading = false,
                error = null,
            )
        } catch (e: ApiClient.ApiException) {
            if (e.status == 401) {
                container.unpair() // 401 → drop to pairing (handled by nav).
                stop()
            }
            _state.value = _state.value.copy(
                error = if (e.status == 403) "Not allowed: ${e.detail}" else e.detail,
                loading = false,
            )
        } catch (e: Exception) {
            // Keep showing the last list; the per-row last-seen ages keep
            // ticking (R75), so stale rows degrade honestly on their own.
            _state.value = _state.value.copy(
                error = "Offline — showing last data.",
                loading = false,
            )
        }
    }
}
