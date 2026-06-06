package oss.roost.mobile.ui.dashboard

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.Derived
import oss.roost.mobile.model.Parsers
import oss.roost.mobile.model.Run
import oss.roost.mobile.model.Worker
import oss.roost.mobile.net.ApiClient

data class DashboardUiState(
    val derived: Derived? = null,
    val error: String? = null,
    val loading: Boolean = true,
)

/**
 * Polls GET /derived every 2s while the screen is STARTED (start/stop driven by the
 * Compose lifecycle, API.md §2: "never in background"). Sorts runs running/assigned-first
 * then created_at desc, and exposes the live-node count.
 */
class DashboardViewModel(private val container: AppContainer) : ViewModel() {

    private val _state = MutableStateFlow(DashboardUiState())
    val state: StateFlow<DashboardUiState> = _state

    private var pollJob: Job? = null

    /** Begin/refresh the 2s poll loop. Idempotent — calling twice keeps one loop. */
    fun start() {
        if (pollJob?.isActive == true) return
        pollJob = viewModelScope.launch {
            while (isActive) {
                refreshOnce()
                delay(2_000)
            }
        }
    }

    fun stop() {
        pollJob?.cancel()
        pollJob = null
    }

    fun refreshNow() {
        viewModelScope.launch { refreshOnce() }
    }

    private suspend fun refreshOnce() {
        try {
            val raw = container.api.derivedRaw(limit = 40)
            val d = Parsers.parseDerived(raw)
            _state.value = DashboardUiState(derived = d.sortedForDisplay(), error = null, loading = false)
            // Write-behind to the offline cache (DESIGN §5): last good payload
            // repaints the dashboard on a cold start while the CP is unreachable.
            withContext(Dispatchers.IO) { container.cache.saveDerived(raw) }
        } catch (e: ApiClient.ApiException) {
            if (e.status == 401) {
                container.unpair() // 401 → drop to pairing (handled by nav).
                stop()
            }
            _state.value = _state.value.copy(error = e.detail, loading = false)
        } catch (e: Exception) {
            loadCachedIfEmpty()
            _state.value = _state.value.copy(error = e.message ?: "network error", loading = false)
        }
    }

    /**
     * Offline cold start: nothing fetched yet and the CP is unreachable → render
     * the cached payload. Its old generated_at trips the staleness pill (§2), so
     * the UI is honest about age with no extra state.
     */
    private suspend fun loadCachedIfEmpty() {
        if (_state.value.derived != null) return
        val cached = withContext(Dispatchers.IO) { container.cache.loadDerived() } ?: return
        runCatching { Parsers.parseDerived(cached) }.getOrNull()?.let {
            _state.value = _state.value.copy(derived = it.sortedForDisplay(), loading = false)
        }
    }

    /** Cancel a running job (confirm handled by the UI). */
    fun cancel(runId: String) {
        viewModelScope.launch {
            try { container.api.cancel(runId); refreshOnce() } catch (_: Exception) {}
        }
    }

    /** Retry a failed run by resubmitting its spec (client-side, API.md §4). */
    fun retry(runId: String, onSubmitted: (String) -> Unit) {
        viewModelScope.launch {
            try {
                val job = container.api.job(runId)
                val pin = (job.specRequires["worker"] as? String)
                val newJob = container.api.submit(
                    intent = job.specIntent ?: job.intent,
                    kind = job.specKind,
                    pinWorker = pin,
                    command = job.specCommand,
                )
                refreshOnce()
                onSubmitted(newJob.id)
            } catch (_: Exception) {}
        }
    }
}

/** Sort: running/assigned first, then created_at desc (API.md §2). */
private fun Derived.sortedForDisplay(): Derived = copy(
    runs = runs.sortedWith(
        compareByDescending<Run> { it.isActive }.thenByDescending { it.createdAt }
    )
)

/** Live nodes = idle+busy (API.md §2). */
fun List<Worker>.liveCount(): Int = count { it.isLive }
