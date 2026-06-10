package oss.roost.mobile.ui.publish

import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Public
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import oss.roost.mobile.AppContainer

/**
 * Publish-a-site sheet (API.md §6, production north star #3): pick a `tar.gz` via
 * the SAF document picker, name it with a live slug preview, ship it ONE-SHOT,
 * then show the live site URL with a share intent. Mirrors the iOS `PublishView`
 * UX and follows the `NewSessionSheet` shape (a `ModalBottomSheet` driven by a
 * ViewModel `StateFlow`).
 *
 * NOTE (R53): authored without an Android emulator in the fleet, so the rendered
 * UI is unverified — the Compose layer compiles against the android-35 stubs and
 * the pure publish logic is covered by the JVM harness, but on-device behavior
 * (picker round-trip, share chooser) has not been observed.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PublishSheet(
    container: AppContainer,
    onDismiss: () -> Unit,
) {
    val vm = remember { PublishViewModel(container) }
    val state by vm.state.collectAsState()
    val context = LocalContext.current

    // SAF document picker: any MIME (gzip is often reported as octet-stream / *
    // by providers), then we sniff the magic bytes in the ViewModel. The screen
    // owns the ContentResolver read; the ViewModel only sees bytes + a name.
    val picker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        try {
            val bytes = context.contentResolver.openInputStream(uri)?.use { it.readBytes() }
            if (bytes == null) {
                vm.fileError("Couldn't read that file.")
            } else {
                vm.loadBundle(displayName(context, uri), bytes)
            }
        } catch (e: Exception) {
            vm.fileError("Couldn't open that file: ${e.message ?: "unknown error"}")
        }
    }

    // R123: same IME treatment as NewSessionSheet — the sheet's own window never sees
    // the app-root imePadding (Theme.kt), so without this the keyboard covered the
    // Publish CTA while naming the site. Expanded-only + imePadding + scroll keeps the
    // Name field and the CTA visible above the keyboard on any screen height.
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
    ) {
        Column(
            Modifier
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .imePadding()
                .padding(horizontal = 20.dp)
                .padding(bottom = 24.dp),
        ) {
            Text("Publish a site", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(12.dp))

            val site = state.site
            if (site != null) {
                // ---- Result: live URL + share + open ----
                Text("Published", style = MaterialTheme.typography.labelMedium)
                Spacer(Modifier.height(4.dp))
                Text(site.slug, style = MaterialTheme.typography.bodyLarge)
                Spacer(Modifier.height(4.dp))
                Text(
                    site.shareUrl,
                    style = MaterialTheme.typography.bodyMedium,
                    fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.primary,
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    "${site.files} files · ${formatBytes(site.size)}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(16.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Button(
                        onClick = { context.startActivity(shareChooser(site.shareUrl)) },
                        modifier = Modifier.weight(1f),
                    ) { Text("Share link") }
                    Spacer(Modifier.width(12.dp))
                    OutlinedButton(
                        onClick = { context.startActivity(openInBrowser(site.shareUrl)) },
                        modifier = Modifier.weight(1f),
                    ) { Text("Open") }
                }
                Spacer(Modifier.height(12.dp))
                TextButton(onClick = onDismiss, modifier = Modifier.fillMaxWidth()) {
                    Text("Done")
                }
            } else {
                // ---- Pick the bundle ----
                OutlinedButton(
                    onClick = { picker.launch(arrayOf("*/*")) },
                    modifier = Modifier.fillMaxWidth(),
                    enabled = !state.publishing,
                ) {
                    Icon(Icons.Filled.Public, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text(state.fileName ?: "Choose a .tar.gz bundle…")
                }
                if (state.hasBundle) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        formatBytes(state.fileSize),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Text(
                    "A gzipped tar of a static site. Published in one transactional " +
                        "upload — nothing is staged.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 6.dp),
                )

                // ---- Name → slug (only once a bundle is loaded) ----
                if (state.hasBundle) {
                    Spacer(Modifier.height(16.dp))
                    OutlinedTextField(
                        value = state.name,
                        onValueChange = vm::setName,
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("Name") },
                        placeholder = { Text("my-site") },
                        singleLine = true,
                        enabled = !state.publishing,
                        isError = state.name.isNotEmpty() && !state.nameValid,
                        keyboardOptions = KeyboardOptions(
                            keyboardType = KeyboardType.Uri,
                            capitalization = KeyboardCapitalization.None,
                        ),
                    )
                    // Live preview of the slug the server will store + the URL path.
                    val hint = when {
                        state.name.isEmpty() ->
                            "Re-publishing an existing name replaces that site."
                        state.nameValid -> "Will publish at /pub/${state.slugPreview}/"
                        else -> "Lowercase letters, numbers, or hyphens (≤40)."
                    }
                    Text(
                        hint,
                        style = MaterialTheme.typography.bodySmall,
                        color = if (state.name.isNotEmpty() && !state.nameValid)
                            MaterialTheme.colorScheme.error
                        else MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 4.dp),
                    )
                }

                state.error?.let {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        it,
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }

                Spacer(Modifier.height(16.dp))
                Button(
                    onClick = { vm.publish() },
                    enabled = state.canPublish,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    if (state.publishing) {
                        CircularProgressIndicator(Modifier.height(18.dp), strokeWidth = 2.dp)
                    } else {
                        Text("Publish")
                    }
                }
            }
        }
    }
}

/** Resolve a content:// URI to its display name (falls back to the last path segment). */
private fun displayName(context: android.content.Context, uri: android.net.Uri): String {
    return try {
        context.contentResolver.query(uri, null, null, null, null)?.use { c ->
            val idx = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
            if (idx >= 0 && c.moveToFirst()) c.getString(idx) else null
        } ?: uri.lastPathSegment ?: "bundle.tar.gz"
    } catch (_: Exception) {
        uri.lastPathSegment ?: "bundle.tar.gz"
    }
}

/** Android share-sheet intent for the published URL (mirrors iOS `ShareLink`). */
private fun shareChooser(url: String): Intent {
    val send = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_TEXT, url)
    }
    return Intent.createChooser(send, "Share site").apply {
        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }
}

/** Open the published URL in the browser (mirrors iOS `Link`). */
private fun openInBrowser(url: String): Intent =
    Intent(Intent.ACTION_VIEW, android.net.Uri.parse(url)).apply {
        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }

/** Human byte size like "32 B", "1.2 KB", "3.4 MB" (decimal, to read like the web). */
private fun formatBytes(count: Long): String {
    val n = count.coerceAtLeast(0)
    if (n < 1000) return "$n B"
    val units = listOf("KB", "MB", "GB", "TB")
    var value = n.toDouble() / 1000
    var unit = 0
    while (value >= 1000 && unit < units.size - 1) {
        value /= 1000
        unit++
    }
    return "%.1f %s".format(value, units[unit])
}
