package oss.roost.mobile.ui.session

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.Composer
import oss.roost.mobile.model.Run
import oss.roost.mobile.model.StreamEvent
import oss.roost.mobile.net.ApiClient
import oss.roost.mobile.sse.LogBuffer
import oss.roost.mobile.sse.LogCache
import oss.roost.mobile.sse.RenderedLine

data class DoneResult(
    val state: String,
    val exitCode: Int?,
    val error: String?,
    val result: String?,
    val tokensUsed: Int?,
)

data class SessionUiState(
    val story: Run? = null,
    val lines: List<RenderedLine> = emptyList(),
    val state: String = "queued",
    val connected: Boolean = false,
    val done: DoneResult? = null,
    val children: List<ChildRow> = emptyList(),
    val error: String? = null,
    // Follow-up composer (DESIGN §3.2 / API.md §4, R38).
    val draft: String = "",
    val sending: Boolean = false,
    val sendOutcome: String? = null,   // last input's queued/delivered/dropped line
) {
    /** Composer is live only while the job can still accept input (server 409s a
     *  terminal job). Mirrors the server's `terminal` gate + iOS `isTerminal`. */
    val isTerminal: Boolean
        get() = state == "succeeded" || state == "failed" || state == "cancelled" || done != null
}

data class ChildRow(val id: String, val intent: String, val state: String, val depth: Int)

/**
 * Drives the session screen. On attach it:
 *   1. loads the header (GET /jobs/{id}/derived),
 *   2. pages catch-up logs from the persisted cursor (GET /logs?since=) until drained,
 *   3. attaches the SSE stream with since=<cursor> and folds events in, deduping by seq.
 * On background it stops; on foreground it re-pages then re-attaches (API.md §5).
 *
 * The cursor (max seq) is persisted per job id via SecureStore so a cold start resumes.
 */
class SessionViewModel(
    private val container: AppContainer,
    private val jobId: String,
) : ViewModel() {

    private val _state = MutableStateFlow(SessionUiState())
    val state: StateFlow<SessionUiState> = _state

    private val logs = LogBuffer()
    private var streamJob: Job? = null
    private var seeded = false
    private var linesSincePersist = 0

    fun start() {
        loadStory()
        attach()
    }

    /**
     * Cold-start seed from the offline cache (DESIGN §5): repaint last-known
     * lines immediately and derive the resume cursor from them. NOTE: this
     * deliberately replaces seeding from the bare persisted cursor — a cursor
     * without its lines would hide all pre-cursor history after a cold start.
     */
    private suspend fun seedFromCacheOnce() {
        if (seeded) return
        seeded = true
        val encoded = withContext(Dispatchers.IO) { container.cache.loadLogs(jobId) } ?: return
        logs.seed(LogCache.decode(encoded))
        if (logs.rendered.isNotEmpty()) emitLines()
    }

    fun stop() {
        streamJob?.cancel()
        streamJob = null
        _state.value = _state.value.copy(connected = false)
        persistCursor()
    }

    private fun loadStory() {
        viewModelScope.launch {
            try {
                val story = container.api.jobStory(jobId)
                _state.value = _state.value.copy(story = story, state = story.state)
            } catch (e: ApiClient.ApiException) {
                handleAuth(e)
                _state.value = _state.value.copy(error = e.detail)
            } catch (_: Exception) { /* header is best-effort */ }
        }
    }

    /** Catch-up paging then SSE attach. Re-entrant: cancels any prior stream first. */
    fun attach() {
        if (streamJob?.isActive == true) return
        streamJob = viewModelScope.launch {
            // 0) Repaint last-known lines from the offline cache (also seeds the cursor).
            seedFromCacheOnce()
            // 1) Page the gap from the persisted cursor until a page is short (drained).
            try {
                while (true) {
                    val page = container.api.logs(jobId, since = logs.maxSeq, limit = 1000)
                    val added = logs.acceptAll(page.logs)
                    page.state?.let { _state.value = _state.value.copy(state = it) }
                    emitLines()
                    if (page.logs.size < 1000 || added == 0) break
                }
                persistCursor()
            } catch (e: ApiClient.ApiException) {
                handleAuth(e)
            } catch (_: Exception) { /* fall through to stream, which retries */ }

            // 2) Attach the live stream resuming at the cursor; reconnect handled inside.
            container.sse.stream(
                jobId = jobId,
                sinceProvider = { logs.maxSeq },
                onConnected = {
                    _state.value = _state.value.copy(connected = true)
                },
                onEvent = { ev -> onEvent(ev) },
            )
            _state.value = _state.value.copy(connected = false)
        }
    }

    private fun onEvent(ev: StreamEvent) {
        when (ev) {
            is StreamEvent.State -> _state.value = _state.value.copy(state = ev.state)
            is StreamEvent.Log -> {
                if (logs.accept(ev.line)) {
                    emitLines()
                    // Throttle disk writes: every 25 lines is plenty (stop/done
                    // flush the tail), and a chatty job won't thrash flash.
                    if (++linesSincePersist >= 25) persistCursor()
                }
            }
            is StreamEvent.Done -> {
                _state.value = _state.value.copy(
                    state = ev.state,
                    connected = false,
                    done = DoneResult(ev.state, ev.exitCode, ev.error, ev.resultOutput, ev.tokensUsed),
                )
                persistCursor()
            }
            is StreamEvent.Err -> {
                if (ev.error.contains("401")) container.unpair()
                _state.value = _state.value.copy(error = ev.error)
            }
        }
    }

    private fun emitLines() {
        _state.value = _state.value.copy(lines = ArrayList(logs.rendered))
    }

    /** Persist cursor + the capped rendered tail (one artifact: see seedFromCacheOnce). */
    private fun persistCursor() {
        linesSincePersist = 0
        container.store.saveCursor(jobId, logs.maxSeq)
        val snapshot = ArrayList(logs.rendered)
        if (snapshot.isEmpty()) return
        viewModelScope.launch(Dispatchers.IO) {
            container.cache.saveLogs(jobId, LogCache.encode(snapshot))
        }
    }

    fun cancel() {
        viewModelScope.launch {
            try { container.api.cancel(jobId) } catch (_: Exception) {}
        }
    }

    /** Composer text changed (DESIGN §3.2). */
    fun onDraftChange(text: String) {
        _state.value = _state.value.copy(draft = text)
    }

    /**
     * Send the composer draft as a follow-up input (R38, API.md §4): POST it,
     * clear the field, then poll the inputs queue so the user learns whether it
     * was delivered or dropped (agent/docker jobs run with stdin closed → dropped
     * with a reason). Mirrors `roost send --wait` (cli.py) and iOS `SessionStore`.
     */
    fun sendFollowUp() {
        val text = _state.value.draft
        if (_state.value.sending || !Composer.canSend(text)) return
        viewModelScope.launch {
            _state.value = _state.value.copy(sending = true, error = null, sendOutcome = null)
            val ack = try {
                container.api.sendInput(jobId, text)
            } catch (e: ApiClient.ApiException) {
                handleAuth(e)
                _state.value = _state.value.copy(sending = false, error = e.detail)
                return@launch
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    sending = false, error = e.message ?: "send failed")
                return@launch
            }
            // Sent: clear the field, show it's queued, then poll for the outcome.
            _state.value = _state.value.copy(
                draft = "", sending = false,
                sendOutcome = Composer.outcome("queued", null))
            pollInputOutcome(ack.inputId)
        }
    }

    /** Poll GET /jobs/{id}/inputs for ~10 s until this input leaves the queue. */
    private fun pollInputOutcome(inputId: String) {
        viewModelScope.launch {
            repeat(10) {
                try {
                    val row = container.api.inputs(jobId).inputs.firstOrNull { it.id == inputId }
                    if (row != null && row.state != "queued") {
                        _state.value = _state.value.copy(
                            sendOutcome = Composer.outcome(row.state, row.detail))
                        return@launch
                    }
                } catch (_: Exception) { /* keep the queued line; polling is best-effort */ }
                kotlinx.coroutines.delay(1000)
            }
        }
    }

    fun loadTree() {
        viewModelScope.launch {
            try {
                val tree = container.api.tree(jobId)
                _state.value = _state.value.copy(
                    children = tree.filter { it.id != jobId }
                        .map { ChildRow(it.id, it.intent, it.state, it.depth) },
                )
            } catch (_: Exception) {}
        }
    }

    private fun handleAuth(e: ApiClient.ApiException) {
        if (e.status == 401) { container.unpair(); stop() }
    }
}
