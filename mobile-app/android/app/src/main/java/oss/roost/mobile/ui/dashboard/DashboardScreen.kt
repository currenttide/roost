package oss.roost.mobile.ui.dashboard

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import oss.roost.mobile.AppContainer
import oss.roost.mobile.model.HealthGlyph
import oss.roost.mobile.model.Run
import oss.roost.mobile.ui.Semantic
import oss.roost.mobile.ui.common.Format
import oss.roost.mobile.ui.common.LifecycleResume
import oss.roost.mobile.ui.newsession.NewSessionSheet

@Composable
fun DashboardScreen(
    container: AppContainer,
    onOpenSession: (String) -> Unit,
) {
    val vm = remember { DashboardViewModel(container) }
    val state by vm.state.collectAsState()

    // Lifecycle-aware polling: start on RESUME, stop on PAUSE (no background networking).
    LifecycleResume(onResume = vm::start, onPause = vm::stop)

    var showSheet by remember { mutableStateOf(false) }
    var confirmCancel by remember { mutableStateOf<Run?>(null) }
    val nowMs = System.currentTimeMillis()

    Scaffold(
        floatingActionButton = {
            ExtendedFloatingActionButton(
                onClick = { showSheet = true },
                icon = { Icon(Icons.Filled.Mic, contentDescription = null) },
                text = { Text("New session") },
            )
        },
    ) { pad ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(pad),
        ) {
            val derived = state.derived
            VerdictBar(
                level = derived?.fleetVerdict?.level ?: "ok",
                summary = derived?.fleetVerdict?.summary ?: (state.error ?: "Loading…"),
                liveNodes = derived?.workers?.liveCount() ?: 0,
            )
            // Staleness pill (API.md §2): generated_at lagging > 10s.
            derived?.let {
                Format.staleness(it.generatedAt, nowMs)?.let { pill ->
                    StalePill(pill)
                }
            }

            if (derived == null && state.loading) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text("Connecting to the fleet…")
                }
            } else {
                LazyColumn(Modifier.fillMaxSize()) {
                    items(derived?.runs ?: emptyList(), key = { it.runId }) { run ->
                        RunRow(
                            run = run,
                            onClick = { onOpenSession(run.runId) },
                            onCancel = { confirmCancel = run },
                            onRetry = { vm.retry(run.runId) { id -> onOpenSession(id) } },
                        )
                    }
                }
            }
        }
    }

    if (showSheet) {
        NewSessionSheet(
            container = container,
            onDismiss = { showSheet = false },
            onDispatched = { id ->
                showSheet = false
                onOpenSession(id)
            },
        )
    }

    confirmCancel?.let { run ->
        AlertDialog(
            onDismissRequest = { confirmCancel = null },
            title = { Text("Cancel job?") },
            text = { Text(run.goal) },
            confirmButton = {
                TextButton(onClick = {
                    vm.cancel(run.runId); confirmCancel = null
                }) { Text("Cancel job") }
            },
            dismissButton = {
                TextButton(onClick = { confirmCancel = null }) { Text("Keep running") }
            },
        )
    }
}

@Composable
private fun VerdictBar(level: String, summary: String, liveNodes: Int) {
    val ok = level.equals("ok", ignoreCase = true)
    val color = if (ok) Semantic.good else Semantic.bad
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
            text = summary.ifBlank { if (ok) "All healthy" else "Needs attention" },
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.SemiBold,
            modifier = Modifier.weight(1f),
        )
        Spacer(Modifier.width(8.dp))
        Text("$liveNodes nodes", style = MaterialTheme.typography.labelMedium)
    }
}

@Composable
private fun StalePill(text: String) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(Semantic.warn.copy(alpha = 0.18f))
            .padding(horizontal = 16.dp, vertical = 4.dp),
    ) {
        Text(text, style = MaterialTheme.typography.labelSmall, color = Semantic.warn)
    }
}

/**
 * One run row. Tap → session. Long-press surfaces the action menu (cancel for running,
 * retry for failed) — a simple long-press menu avoids a swipe-dependency and is reliable.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun RunRow(
    run: Run,
    onClick: () -> Unit,
    onCancel: () -> Unit,
    onRetry: () -> Unit,
) {
    val mapping = HealthGlyph.map(run.health.status)
    val glyphColor = Format.toneColor(mapping.tone).let {
        if (it == Color.Unspecified) MaterialTheme.colorScheme.onSurface else it
    }
    var menu by remember { mutableStateOf(false) }

    Column(
        Modifier
            .fillMaxWidth()
            // Tap → open session; long-press → row actions (cancel/retry) per DESIGN §3.1.
            .combinedClickable(onClick = onClick, onLongClick = { menu = true })
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            // Glyph (or plain status text when unknown — never crash).
            if (mapping.knownStatus) {
                Text(mapping.glyph, color = glyphColor, modifier = Modifier.width(22.dp))
            } else {
                Text(
                    run.health.status.take(3),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.width(28.dp),
                )
            }
            Text(
                run.goal,
                style = MaterialTheme.typography.bodyLarge,
                fontWeight = FontWeight.Medium,
                modifier = Modifier.weight(1f),
            )
        }
        Spacer(Modifier.height(2.dp))
        Text(
            text = subtitle(run),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 22.dp),
        )
        run.bestLine?.takeIf { it.isNotBlank() }?.let {
            Text(
                it,
                style = MaterialTheme.typography.labelSmall,
                fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                modifier = Modifier.padding(start = 22.dp, top = 2.dp),
            )
        }

        if (menu) {
            Row(
                Modifier
                    .fillMaxWidth()
                    .padding(start = 22.dp, top = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                if (run.state == "running" || run.state == "assigned" || run.state == "queued") {
                    Button(onClick = { menu = false; onCancel() }) { Text("Cancel") }
                }
                if (run.state == "failed") {
                    Button(onClick = { menu = false; onRetry() }) { Text("Retry") }
                }
                TextButton(onClick = { menu = false }) { Text("Close") }
            }
        }
    }
}

private fun subtitle(run: Run): String {
    val parts = ArrayList<String>()
    run.worker?.let { parts.add(it) }
    parts.add(if (run.state == "running") "claude" else run.state)
    when (run.state) {
        "running", "assigned" -> {
            val elapsed = (System.currentTimeMillis() / 1000.0) - run.createdAt
            parts.add(Format.duration(elapsed))
        }
        "succeeded" -> if (run.tokensUsed > 0) parts.add("${Format.tokens(run.tokensUsed)} tok")
        "failed" -> run.exitCode?.let { parts.add("exit $it") }
    }
    return parts.joinToString(" · ")
}
