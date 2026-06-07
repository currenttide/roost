package oss.roost.mobile.ui.schedules

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.Schedule
import oss.roost.mobile.model.ScheduleInterval
import oss.roost.mobile.model.ScheduleIntervalPreset

/**
 * Schedules screen (API.md §7): list the interval schedules the control plane
 * re-runs on a cadence, create one from a task + interval, toggle each on/off, and
 * delete with a confirm. Reached from the dashboard overflow menu (the same
 * pattern as Publish (R53) and Notifications (R55)) and mirrors the iOS
 * `SchedulesView` UX.
 *
 * All interval grammar/format/validation + list reducers are in the pure `model`
 * layer (`ScheduleInterval`/`ScheduleListReducer`, JVM-tested); this screen is the
 * Compose shell over `SchedulesViewModel`.
 *
 * NOTE (R61): authored without an Android emulator in the fleet, so the rendered
 * UI is unverified — the Compose layer compiles against the android-35 stubs and
 * the pure schedule logic is covered by the JVM harness, but on-device behavior
 * (toggle round-trip, swipe/confirm) has not been observed.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SchedulesScreen(
    container: AppContainer,
    onBack: () -> Unit,
) {
    val vm = remember { SchedulesViewModel(container) }
    val state by vm.state.collectAsState()
    var confirmDelete by remember { mutableStateOf<Schedule?>(null) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Schedules") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
    ) { pad ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(pad)
                .padding(16.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            CreateSection(state = state, vm = vm)

            HorizontalDivider()

            Text("Active schedules", style = MaterialTheme.typography.titleMedium)
            if (state.schedules.isEmpty() && !state.loading) {
                Text(
                    "No schedules yet. Create one above to run a task on a fixed interval.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            state.schedules.forEach { schedule ->
                ScheduleRow(
                    schedule = schedule,
                    onToggle = { vm.toggle(schedule) },
                    onDelete = { confirmDelete = schedule },
                )
            }

            state.error?.let {
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
        }
    }

    confirmDelete?.let { schedule ->
        AlertDialog(
            onDismissRequest = { confirmDelete = null },
            title = { Text("Delete this schedule?") },
            text = { Text(schedule.taskSummary) },
            confirmButton = {
                TextButton(onClick = {
                    vm.delete(schedule); confirmDelete = null
                }) { Text("Delete") }
            },
            dismissButton = {
                TextButton(onClick = { confirmDelete = null }) { Text("Keep") }
            },
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CreateSection(state: SchedulesUiState, vm: SchedulesViewModel) {
    Text("New schedule", style = MaterialTheme.typography.titleMedium)

    OutlinedTextField(
        value = state.taskText,
        onValueChange = vm::setTask,
        modifier = Modifier.fillMaxWidth(),
        label = { Text("Task to run each interval") },
        minLines = 1,
        maxLines = 3,
        enabled = !state.creating,
    )

    // Agent vs command kind (mirrors the New-session toggle).
    SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
        SegmentedButton(
            selected = !state.isCommand,
            onClick = { vm.setIsCommand(false) },
            shape = SegmentedButtonDefaults.itemShape(index = 0, count = 2),
        ) { Text("Agent") }
        SegmentedButton(
            selected = state.isCommand,
            onClick = { vm.setIsCommand(true) },
            shape = SegmentedButtonDefaults.itemShape(index = 1, count = 2),
        ) { Text("Command") }
    }

    // Interval: free-text field accepting the server's `every` grammar + presets.
    OutlinedTextField(
        value = state.every,
        onValueChange = vm::setEvery,
        modifier = Modifier.fillMaxWidth(),
        label = { Text("Interval (e.g. 6h)") },
        singleLine = true,
        isError = state.every.isNotEmpty() && state.intervalMessage != null,
        enabled = !state.creating,
    )
    Row(
        Modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        ScheduleIntervalPreset.ALL.forEach { preset ->
            FilterChip(
                selected = state.every == preset,
                onClick = { vm.setEvery(preset) },
                label = { Text(preset) },
            )
        }
    }

    // Interval hint: preview when valid, the parse/floor reason when not, else help.
    val hint = state.intervalPreview?.let {
        "Runs every $it; first run one interval from now."
    } ?: state.intervalMessage
        ?: "Interval: seconds or <N>[smhd] — e.g. 30s, 15m, 6h, 1d. Minimum 30s."
    Text(
        hint,
        style = MaterialTheme.typography.bodySmall,
        color = if (state.every.isNotEmpty() && state.intervalMessage != null)
            MaterialTheme.colorScheme.error
        else MaterialTheme.colorScheme.onSurfaceVariant,
    )

    OutlinedTextField(
        value = state.name,
        onValueChange = vm::setName,
        modifier = Modifier.fillMaxWidth(),
        label = { Text("Label (optional)") },
        singleLine = true,
        enabled = !state.creating,
    )

    Button(
        onClick = { vm.create() },
        enabled = state.canCreate,
        modifier = Modifier.fillMaxWidth(),
    ) {
        if (state.creating) {
            CircularProgressIndicator(Modifier.height(18.dp), strokeWidth = 2.dp)
        } else {
            Text("Create schedule")
        }
    }
}

/**
 * One schedule row: interval + label, enabled switch, task summary, the next-run
 * clock, and a delete button. Toggle/delete call back into the ViewModel; the row
 * only renders the authoritative [Schedule] (clock via [ScheduleInterval]).
 */
@Composable
private fun ScheduleRow(
    schedule: Schedule,
    onToggle: () -> Unit,
    onDelete: () -> Unit,
) {
    Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                ScheduleInterval.format(schedule.intervalSec),
                style = MaterialTheme.typography.bodyLarge,
                fontWeight = FontWeight.SemiBold,
                fontFamily = FontFamily.Monospace,
            )
            schedule.name?.takeIf { it.isNotEmpty() }?.let {
                Spacer(Modifier.width(8.dp))
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Spacer(Modifier.weight(1f))
            Switch(checked = schedule.enabled, onCheckedChange = { onToggle() })
            IconButton(onClick = onDelete) {
                Icon(Icons.Filled.Delete, contentDescription = "Delete schedule")
            }
        }
        Text(
            schedule.taskSummary,
            style = MaterialTheme.typography.bodyMedium,
            maxLines = 2,
        )
        Text(
            clockLine(schedule),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/** "Next run in 5h" while enabled; "Paused" when off (mirrors iOS `clockLine`). */
private fun clockLine(schedule: Schedule): String {
    if (!schedule.enabled) return "Paused"
    val now = System.currentTimeMillis() / 1000.0
    val next = ScheduleInterval.relative(schedule.nextRunAt, now)
    return if (next != null) "Next run $next" else "Scheduled"
}
