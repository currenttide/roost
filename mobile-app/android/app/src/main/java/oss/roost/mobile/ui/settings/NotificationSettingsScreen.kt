package oss.roost.mobile.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import oss.roost.mobile.AppContainer

/**
 * Notification-settings screen (R37 / DESIGN.md §6 v1.1). The user enters the ntfy
 * topic their control plane is configured to POST to (`roost serve --notify-url
 * https://ntfy.sh/<topic>`); the app stores the canonical subscribe URL. The
 * device-only half (subscribing + showing the system notification) is the
 * UnifiedPush/ntfy binding (untested here); this screen makes the topic an
 * explicit, validated setting.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun NotificationSettingsScreen(
    container: AppContainer,
    onBack: () -> Unit,
) {
    val vm = remember { NotificationSettingsViewModel(container) }
    val state by vm.state.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Notifications") },
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
            Text("Job notifications", style = MaterialTheme.typography.titleMedium)
            Text(
                "Enter the ntfy topic your control plane posts to "
                    + "(roost serve --notify-url …). Then subscribe to the same topic "
                    + "in the ntfy app to get a push when a job finishes. A bare name "
                    + "uses ntfy.sh; paste a full URL for a self-hosted server.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            OutlinedTextField(
                value = state.input,
                onValueChange = vm::onInputChange,
                label = { Text("ntfy topic or URL") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )

            state.preview?.let {
                Text(
                    "Subscribes to: $it",
                    style = MaterialTheme.typography.bodySmall,
                    fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            Button(
                onClick = vm::save,
                enabled = state.canSave,
                modifier = Modifier.fillMaxWidth(),
            ) { Text("Save") }

            state.savedUrl?.let { saved ->
                Text(
                    "Watching: $saved",
                    style = MaterialTheme.typography.bodySmall,
                    fontFamily = FontFamily.Monospace,
                )
                OutlinedButton(
                    onClick = vm::clear,
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("Stop watching") }
            }

            state.error?.let {
                Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
            }
        }
    }
}
