package oss.roost.mobile.ui.fleet

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.Fleet
import oss.roost.mobile.model.Worker
import oss.roost.mobile.ui.Semantic
import oss.roost.mobile.ui.common.LifecycleResume

/**
 * Fleet screen (R121, API.md §2a): every worker — name, status, capability
 * summary, load, last-seen — so an operator can answer "is my fleet up" from
 * the couch. Reached from the dashboard overflow menu (the same pattern as
 * Publish R53 / Notifications R55 / Schedules R61) and mirrors the iOS
 * `FleetView` UX.
 *
 * All judgment (stale/offline pills, caps summary, sort) is in the pure
 * `model.Fleet` layer (JVM-tested); this screen is the Compose shell over
 * `FleetViewModel`. A 1 s ticker drives `nowMs` so the pills and ages keep
 * advancing even when no new payload arrives (the R75 lesson — see
 * DashboardScreen's staleness note).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FleetScreen(
    container: AppContainer,
    onBack: () -> Unit,
) {
    val vm = remember { FleetViewModel(container) }
    val state by vm.state.collectAsState()

    // Lifecycle-aware polling: start on RESUME, stop on PAUSE (no background networking).
    LifecycleResume(onResume = vm::start, onPause = vm::stop)

    // R75: pills/ages are wall-clock driven; tick `nowMs` independent of polls.
    var nowMs by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            nowMs = System.currentTimeMillis()
            delay(1_000)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Fleet") },
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
                .padding(pad),
        ) {
            when {
                state.workers.isNotEmpty() -> {
                    FleetHeadline(workers = state.workers, nowMs = nowMs)
                    state.error?.let { ErrorLine(it) }
                    LazyColumn(Modifier.fillMaxSize()) {
                        items(state.workers, key = { it.id }) { worker ->
                            WorkerRow(worker = worker, nowMs = nowMs)
                        }
                    }
                }
                state.loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text("Loading the fleet…")
                }
                else -> Column(Modifier.padding(16.dp)) {
                    state.error?.let { ErrorLine(it) }
                    Text(
                        "No workers enrolled. Add one with `roost worker` or the "
                            + "roost-onboard skill.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}

/** "3 of 4 up" — up means server-live AND fresh by the client clock (§2a). */
@Composable
private fun FleetHeadline(workers: List<Worker>, nowMs: Long) {
    val up = workers.count { Fleet.isUp(it.status, it.lastSeen, nowMs) }
    val total = workers.size
    val color = when {
        up == total -> Semantic.good
        up == 0 -> Semantic.bad
        else -> Semantic.warn
    }
    Row(
        Modifier
            .fillMaxWidth()
            .background(color.copy(alpha = 0.18f))
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(10.dp).clip(CircleShape).background(color))
        Spacer(Modifier.width(10.dp))
        Text(
            Fleet.headline(up, total),
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.SemiBold,
        )
    }
}

@Composable
private fun ErrorLine(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.labelSmall,
        color = Semantic.warn,
        modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
    )
}

/**
 * One worker row: status dot + name + stale/offline pill, then the capability
 * summary, then status · load · last-seen (ticking).
 */
@Composable
private fun WorkerRow(worker: Worker, nowMs: Long) {
    val pill = Fleet.pill(worker.status, worker.lastSeen, nowMs)
    val up = Fleet.isUp(worker.status, worker.lastSeen, nowMs)
    val dotColor = when {
        up -> Semantic.good
        pill == Fleet.Pill.STALE -> Semantic.warn
        else -> MaterialTheme.colorScheme.onSurfaceVariant
    }

    Column(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 10.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(10.dp).clip(CircleShape).background(dotColor))
            Spacer(Modifier.width(8.dp))
            Text(
                worker.displayName,
                style = MaterialTheme.typography.bodyLarge,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            pill?.let { StatusPill(it) }
        }
        Fleet.capsSummary(worker.capabilities)?.let {
            Spacer(Modifier.height(2.dp))
            Text(
                it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.padding(start = 18.dp),
            )
        }
        Spacer(Modifier.height(2.dp))
        Text(
            listOf(
                worker.status,
                Fleet.loadText(worker.running, worker.capacity),
                Fleet.lastSeenText(worker.lastSeen, nowMs),
            ).joinToString(" · "),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 1,
            modifier = Modifier.padding(start = 18.dp),
        )
    }
}

/** The stale/offline badge: amber for stale, red for offline (API.md §2a). */
@Composable
private fun StatusPill(pill: Fleet.Pill) {
    val color = if (pill == Fleet.Pill.OFFLINE) Semantic.bad else Semantic.warn
    Text(
        pill.label,
        style = MaterialTheme.typography.labelSmall,
        color = color,
        modifier = Modifier
            .clip(RoundedCornerShape(8.dp))
            .background(color.copy(alpha = 0.18f))
            .padding(horizontal = 8.dp, vertical = 2.dp),
    )
}
