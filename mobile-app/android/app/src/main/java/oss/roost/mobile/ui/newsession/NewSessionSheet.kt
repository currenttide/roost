package oss.roost.mobile.ui.newsession

import android.Manifest
import android.content.pm.PackageManager
import android.view.HapticFeedbackConstants
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.dp
import oss.roost.mobile.AppContainer
import oss.roost.mobile.voice.Dictation

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun NewSessionSheet(
    container: AppContainer,
    onDismiss: () -> Unit,
    onDispatched: (String) -> Unit,
) {
    val vm = remember { NewSessionViewModel(container) }
    val state by vm.state.collectAsState()
    val context = LocalContext.current
    val view = LocalView.current

    val dictation = remember { Dictation(context) }
    val micAvailable = remember { dictation.isAvailable() }
    DisposableEffect(Unit) { onDispose { dictation.destroy() } }

    var hasMicPerm by remember {
        mutableStateOf(
            context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
        )
    }
    val permLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> hasMicPerm = granted }

    // Snapshot of the text before listening began, so partials append rather than clobber.
    var prefix by remember { mutableStateOf("") }

    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 20.dp)
                .padding(bottom = 24.dp),
        ) {
            Text("New session", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(12.dp))

            OutlinedTextField(
                value = state.text,
                onValueChange = vm::setText,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(120.dp),
                placeholder = { Text("e.g. fix the flaky auth test in roost-oss") },
                enabled = !state.busy,
            )

            // Recent prompts (last 10) for one-tap reuse.
            if (state.recents.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    state.recents.take(10).forEach { r ->
                        AssistChip(
                            onClick = { vm.setText(r) },
                            label = { Text(r.take(28), maxLines = 1) },
                        )
                    }
                }
            }

            Spacer(Modifier.height(12.dp))
            // target: auto vs pin
            Text("target", style = MaterialTheme.typography.labelMedium)
            Row(Modifier.horizontalScroll(rememberScrollState())) {
                FilterChip(
                    selected = state.pinWorker == null,
                    onClick = { vm.setPin(null) },
                    label = { Text("auto") },
                )
                Spacer(Modifier.width(6.dp))
                state.workers.forEach { w ->
                    FilterChip(
                        selected = state.pinWorker == w.id,
                        onClick = { vm.setPin(w.id) },
                        label = { Text(w.name) },
                    )
                    Spacer(Modifier.width(6.dp))
                }
            }

            Spacer(Modifier.height(8.dp))
            // kind: agent vs command
            Text("kind", style = MaterialTheme.typography.labelMedium)
            Row {
                FilterChip(
                    selected = !state.asCommand,
                    onClick = { vm.setAsCommand(false) },
                    label = { Text("agent") },
                )
                Spacer(Modifier.width(6.dp))
                FilterChip(
                    selected = state.asCommand,
                    onClick = { vm.setAsCommand(true) },
                    label = { Text("command") },
                )
            }

            state.error?.let {
                Spacer(Modifier.height(8.dp))
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }

            Spacer(Modifier.height(16.dp))
            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                Button(
                    onClick = { vm.dispatch(onDispatched) },
                    enabled = !state.busy,
                    modifier = Modifier.weight(1f),
                ) {
                    if (state.busy) {
                        CircularProgressIndicator(Modifier.height(18.dp), strokeWidth = 2.dp)
                    } else {
                        Text("Dispatch")
                    }
                }

                // Mic is hidden entirely when no recognizer exists (DESIGN §4 fallback).
                if (micAvailable) {
                    Spacer(Modifier.width(12.dp))
                    MicButton(
                        onPressStart = {
                            if (!hasMicPerm) {
                                permLauncher.launch(Manifest.permission.RECORD_AUDIO)
                                return@MicButton
                            }
                            view.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
                            prefix = state.text.let { if (it.isBlank()) "" else "$it " }
                            dictation.start(
                                onPartial = { p -> vm.appendPartial(prefix + p) },
                                onFinal = { f -> if (f.isNotBlank()) vm.setText(prefix + f) },
                                onError = { /* swallow; user can retry or type */ },
                            )
                        },
                        onPressEnd = {
                            view.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                            dictation.stop()
                        },
                    )
                }
            }
        }
    }
}

/** A hold-to-talk button: press starts dictation, release stops it. */
@Composable
private fun MicButton(onPressStart: () -> Unit, onPressEnd: () -> Unit) {
    Button(
        onClick = {},
        modifier = Modifier.pointerInput(Unit) {
            detectTapGestures(
                onPress = {
                    onPressStart()
                    // Suspend until release/cancel, then fire onPressEnd exactly once.
                    try {
                        awaitRelease()
                    } finally {
                        onPressEnd()
                    }
                },
            )
        },
    ) {
        Icon(Icons.Filled.Mic, contentDescription = "Hold to talk")
    }
}
