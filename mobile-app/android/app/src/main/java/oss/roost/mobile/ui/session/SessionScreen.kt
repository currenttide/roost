package oss.roost.mobile.ui.session

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import oss.roost.mobile.AppContainer
import oss.roost.mobile.sse.RenderedLine
import oss.roost.mobile.ui.Semantic
import oss.roost.mobile.ui.common.Format
import oss.roost.mobile.ui.common.LifecycleResume

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SessionScreen(
    container: AppContainer,
    jobId: String,
    onOpenChild: (String) -> Unit,
    onBack: () -> Unit,
) {
    val vm = remember(jobId) { SessionViewModel(container, jobId) }
    val state by vm.state.collectAsState()
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    var showTree by remember { mutableStateOf(false) }

    LifecycleResume(onResume = vm::start, onPause = vm::stop)

    // Auto-follow: only when the user is already near the bottom, so scrolling up to read
    // history isn't yanked back down (DESIGN §3.2 auto-follow tail + jump-to-bottom FAB).
    val atBottom by remember {
        derivedStateOf {
            val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            last >= state.lines.size - 2
        }
    }
    LaunchedEffect(state.lines.size) {
        if (atBottom && state.lines.isNotEmpty()) {
            listState.scrollToItem(state.lines.size - 1)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(
                            state.story?.goal ?: jobId,
                            maxLines = 1,
                            style = MaterialTheme.typography.titleMedium,
                        )
                        Text(
                            sessionSubtitle(state),
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
        floatingActionButton = {
            if (!atBottom && state.lines.isNotEmpty()) {
                FloatingActionButton(onClick = {
                    scope.launch { listState.scrollToItem(state.lines.size - 1) }
                }) {
                    Icon(Icons.Filled.KeyboardArrowDown, contentDescription = "Jump to latest")
                }
            }
        },
    ) { pad ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(pad),
        ) {
            state.error?.let {
                Text(
                    it,
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
                )
            }

            LazyColumn(
                state = listState,
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth(),
            ) {
                items(state.lines, key = { it.seq }) { line -> LogRow(line) }
                state.done?.let { item { ResultCard(it) } }
                if (showTree && state.children.isNotEmpty()) {
                    item { HorizontalDivider() }
                    items(state.children, key = { "child_" + it.id }) { child ->
                        ChildRowView(child, onClick = { onOpenChild(child.id) })
                    }
                }
            }

            HorizontalDivider()
            Row(
                Modifier
                    .fillMaxWidth()
                    .padding(12.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                val running = state.state == "running" || state.state == "assigned" ||
                    state.state == "queued"
                if (running) {
                    Button(onClick = vm::cancel) { Text("Cancel") }
                }
                OutlinedButton(onClick = {
                    showTree = !showTree
                    if (showTree) vm.loadTree()
                }) {
                    Text(if (showTree) "Hide tree" else "Tree ▸")
                }
            }
        }
    }
}

@Composable
private fun LogRow(line: RenderedLine) {
    when (line.kind) {
        RenderedLine.Kind.EVENT ->
            Row(
                Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                HorizontalDivider(Modifier.weight(1f))
                Text(
                    "  ${line.text}  ",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                HorizontalDivider(Modifier.weight(1f))
            }
        else ->
            Text(
                text = line.text,
                fontFamily = FontFamily.Monospace,
                fontSize = 13.sp,
                color = if (line.kind == RenderedLine.Kind.STDERR) Semantic.bad
                else MaterialTheme.colorScheme.onSurface,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 1.dp),
            )
    }
}

@Composable
private fun ResultCard(done: DoneResult) {
    Card(Modifier.padding(16.dp).fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Text(done.state.uppercase(), fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(6.dp))
            done.exitCode?.let { Text("exit code: $it", style = MaterialTheme.typography.bodySmall) }
            done.result?.takeIf { it.isNotBlank() }?.let {
                Text(it, style = MaterialTheme.typography.bodyMedium)
            }
            done.error?.takeIf { it.isNotBlank() }?.let {
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
            done.tokensUsed?.takeIf { it > 0 }?.let {
                Text("${Format.tokens(it)} tokens", style = MaterialTheme.typography.labelSmall)
            }
        }
    }
}

@Composable
private fun ChildRowView(child: ChildRow, onClick: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(start = (16 + child.depth * 16).dp, end = 16.dp, top = 8.dp, bottom = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("↳ ", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(child.intent, maxLines = 1, modifier = Modifier.weight(1f))
        Spacer(Modifier.width(8.dp))
        Text(child.state, style = MaterialTheme.typography.labelSmall)
        Spacer(Modifier.width(8.dp))
        OutlinedButton(onClick = onClick) { Text("Open") }
    }
}

private fun sessionSubtitle(s: SessionUiState): String {
    val parts = ArrayList<String>()
    s.story?.worker?.let { parts.add(it) }
    parts.add("claude")
    parts.add(s.state)
    if (!s.connected && (s.state == "running" || s.state == "assigned")) {
        parts.add("reconnecting…")
    }
    return parts.joinToString(" · ")
}
