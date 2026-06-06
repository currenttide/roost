package oss.roost.mobile.ui.pair

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import oss.roost.mobile.AppContainer

/**
 * Pairing screen: manual paste field + deep-link auto-fill. No in-app scanner (the user
 * scans the QR with the system camera / Lens, which opens roost:// → MainActivity).
 */
@Composable
fun PairScreen(
    container: AppContainer,
    deepLink: String?,
    onDeepLinkConsumed: () -> Unit,
    onPaired: () -> Unit,
) {
    val vm = remember { PairViewModel(container) }
    val state by vm.state.collectAsState()

    // A deep link arriving (camera scan) auto-fills + submits once.
    LaunchedEffect(deepLink) {
        if (deepLink != null && deepLink.startsWith("roost://")) {
            vm.onInputChange(deepLink)
            vm.submit(deepLink)
            onDeepLinkConsumed()
        }
    }

    LaunchedEffect(state.paired) { if (state.paired) onPaired() }

    Scaffold { pad ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(pad)
                .padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Text("Pair with Roost", style = MaterialTheme.typography.headlineSmall)
            Spacer(Modifier.height(8.dp))
            Text(
                "On the host run  roost pair  and scan the QR with your camera, " +
                    "or paste the roost:// code below.",
                style = MaterialTheme.typography.bodyMedium,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(24.dp))
            OutlinedTextField(
                value = state.input,
                onValueChange = vm::onInputChange,
                modifier = Modifier.fillMaxWidth(),
                label = { Text("roost://pair?d=…") },
                singleLine = false,
                enabled = !state.busy,
                isError = state.error != null,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            )
            if (state.error != null) {
                Spacer(Modifier.height(8.dp))
                Text(
                    state.error!!,
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            Spacer(Modifier.height(20.dp))
            Button(
                onClick = { vm.submit() },
                enabled = !state.busy,
                modifier = Modifier.fillMaxWidth(),
            ) {
                if (state.busy) {
                    CircularProgressIndicator(
                        modifier = Modifier.height(18.dp),
                        strokeWidth = 2.dp,
                    )
                } else {
                    Text("Pair")
                }
            }
        }
    }
}
