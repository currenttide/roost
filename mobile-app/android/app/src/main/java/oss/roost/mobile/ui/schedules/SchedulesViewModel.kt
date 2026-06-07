package oss.roost.mobile.ui.schedules

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.Schedule
import oss.roost.mobile.model.ScheduleInterval
import oss.roost.mobile.model.ScheduleListReducer
import oss.roost.mobile.net.ApiClient

data class SchedulesUiState(
    /** The current list, newest-first (API.md §7b). */
    val schedules: List<Schedule> = emptyList(),
    val loading: Boolean = false,
    val creating: Boolean = false,
    /** Inline error for the list / create form; cleared as the user edits. */
    val error: String? = null,

    // ---- create-form draft (mirrors `roost schedule "<goal>" --every <i>`) ----

    /** The task one-liner the scheduled job runs each interval. */
    val taskText: String = "",
    /** The `every` string sent to the server (seconds or `<N>[smhd]`). */
    val every: String = "6h",
    /** Optional human label for the schedule. */
    val name: String = "",
    /** Agent (`claude`) vs raw `command`, mirroring the New-session toggle. */
    val isCommand: Boolean = false,
) {
    /** Live preview of the interval the server will store, or null when invalid. */
    val intervalPreview: String?
        get() = ScheduleInterval.parse(every)
            ?.takeIf { it >= ScheduleInterval.MIN_SECONDS }
            ?.let { ScheduleInterval.format(it) }

    /** The reason `every` is rejected, or null when valid (drives the field hint). */
    val intervalMessage: String? get() = ScheduleInterval.validationMessage(every)

    /**
     * Create is allowed once there's a task, a valid interval, and we're idle —
     * exactly the conditions under which `POST /schedules` returns 200.
     */
    val canCreate: Boolean
        get() = taskText.isNotBlank() && ScheduleInterval.isValid(every) && !creating
}

/**
 * Backs the schedules screen (API.md §7): list the interval schedules the control
 * plane re-runs on a cadence, create one from a task + interval, toggle each
 * on/off, and delete. A phone front door scheduling recurring work is the point (§7).
 *
 * All interval grammar/format/validation + list reducers live in the pure `model`
 * layer (`ScheduleInterval`/`ScheduleListReducer`, JVM-tested); this ViewModel is
 * the Android orchestration around the client + the create form's draft fields,
 * mirroring iOS `SchedulesStore`.
 */
class SchedulesViewModel(private val container: AppContainer) : ViewModel() {

    private val _state = MutableStateFlow(SchedulesUiState())
    val state: StateFlow<SchedulesUiState> = _state

    init {
        load()
    }

    // ---- draft edits ----

    fun setTask(s: String) { _state.value = _state.value.copy(taskText = s, error = null) }
    fun setEvery(s: String) { _state.value = _state.value.copy(every = s, error = null) }
    fun setName(s: String) { _state.value = _state.value.copy(name = s, error = null) }
    fun setIsCommand(v: Boolean) { _state.value = _state.value.copy(isCommand = v) }

    // ---- loads / mutations ----

    /** Fetch the schedules list (API.md §7b). 401 → unpair + pairing; 403 → show. */
    fun load() {
        _state.value = _state.value.copy(loading = true)
        viewModelScope.launch {
            try {
                val list = container.api.schedules()
                _state.value = _state.value.copy(schedules = list, loading = false, error = null)
            } catch (e: ApiClient.ApiException) {
                handle(e, fallback = "Couldn't load schedules.")
                _state.value = _state.value.copy(loading = false)
            } catch (e: Exception) {
                _state.value = _state.value.copy(loading = false, error = "Couldn't load schedules.")
            }
        }
    }

    /**
     * Build the §3-shaped spec from the draft. Agent jobs carry
     * `hierarchy.can_dispatch` (the worker injects the roost MCP) exactly like a
     * `POST /jobs` submit; command jobs carry only `command` (API.md §3/§7a).
     */
    private fun draftSpec(): Map<String, Any?> {
        val s = _state.value
        val task = s.taskText.trim()
        return if (s.isCommand) {
            mapOf("kind" to "command", "command" to task, "requires" to emptyMap<String, Any?>())
        } else {
            mapOf(
                "kind" to "claude",
                "intent" to task,
                "requires" to emptyMap<String, Any?>(),
                "hierarchy" to mapOf("can_dispatch" to true),
            )
        }
    }

    /**
     * Create a schedule from the draft, prepend it (newest-first), and reset the
     * task/label. Error handling: 401 → unpair; 403/400 → show the detail (§7a).
     */
    fun create() {
        val s = _state.value
        if (s.taskText.isBlank() || !ScheduleInterval.isValid(s.every) || s.creating) {
            if (!ScheduleInterval.isValid(s.every)) {
                _state.value = s.copy(
                    error = "Interval must be seconds or <N>[smhd], at least 30s.")
            }
            return
        }
        val label = s.name.trim().ifBlank { null }
        _state.value = s.copy(creating = true, error = null)
        viewModelScope.launch {
            try {
                val created = container.api.createSchedule(
                    spec = draftSpec(), every = s.every, name = label)
                _state.value = _state.value.copy(
                    schedules = ScheduleListReducer.prepend(_state.value.schedules, created),
                    creating = false,
                    taskText = "",
                    name = "",
                    error = null,
                )
            } catch (e: ApiClient.ApiException) {
                handle(e, fallback = "Couldn't create: ${e.detail}")
                _state.value = _state.value.copy(creating = false)
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    creating = false, error = "Couldn't create the schedule.")
            }
        }
    }

    /**
     * Toggle a schedule's `enabled` (API.md §7c). Re-enabling restarts the clock
     * server-side; we swap in the returned object. 404 → it's gone, drop it.
     */
    fun toggle(schedule: Schedule) {
        viewModelScope.launch {
            try {
                val updated = container.api.setScheduleEnabled(schedule.id, !schedule.enabled)
                _state.value = _state.value.copy(
                    schedules = ScheduleListReducer.upsertExisting(_state.value.schedules, updated),
                    error = null,
                )
            } catch (e: ApiClient.ApiException) {
                if (e.status == 404) {
                    _state.value = _state.value.copy(
                        schedules = ScheduleListReducer.remove(_state.value.schedules, schedule.id))
                } else {
                    handle(e, fallback = "Couldn't update the schedule.")
                }
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = "Couldn't update the schedule.")
            }
        }
    }

    /**
     * Delete a schedule (API.md §7d). On success (or a 404 — already gone) drop it
     * from the list.
     */
    fun delete(schedule: Schedule) {
        viewModelScope.launch {
            try {
                container.api.deleteSchedule(schedule.id)
                _state.value = _state.value.copy(
                    schedules = ScheduleListReducer.remove(_state.value.schedules, schedule.id),
                    error = null,
                )
            } catch (e: ApiClient.ApiException) {
                if (e.status == 404) {
                    _state.value = _state.value.copy(
                        schedules = ScheduleListReducer.remove(_state.value.schedules, schedule.id))
                } else {
                    handle(e, fallback = "Couldn't delete the schedule.")
                }
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = "Couldn't delete the schedule.")
            }
        }
    }

    /**
     * Common API-error handling (API.md §1): 401 → unpair + bounce to pairing;
     * 403 → "Not allowed: <detail>", stay paired (scope bug); otherwise the
     * caller's fallback message.
     */
    private fun handle(e: ApiClient.ApiException, fallback: String) {
        when (e.status) {
            401 -> {
                container.unpair()
                _state.value = _state.value.copy(error = "Pairing expired — pair again.")
            }
            403 -> _state.value = _state.value.copy(error = "Not allowed: ${e.detail}")
            else -> _state.value = _state.value.copy(error = fallback)
        }
    }
}
