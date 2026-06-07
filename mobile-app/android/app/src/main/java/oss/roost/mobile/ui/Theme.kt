package oss.roost.mobile.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color

// Roost palette: calm slate background, blue accent, semantic green/red/amber for verdicts.
private val Blue = Color(0xFF4C8DFF)
private val Green = Color(0xFF3FB36B)
private val Red = Color(0xFFE5534B)
private val Amber = Color(0xFFE0A53F)

private val DarkColors = darkColorScheme(
    primary = Blue,
    background = Color(0xFF0B1220),
    surface = Color(0xFF131C2B),
    surfaceVariant = Color(0xFF1C2738),
    onBackground = Color(0xFFE6ECF5),
    onSurface = Color(0xFFE6ECF5),
)

private val LightColors = lightColorScheme(
    primary = Blue,
    background = Color(0xFFF7F9FC),
    surface = Color(0xFFFFFFFF),
)

/** Semantic colors used by the verdict bar / health glyphs (not part of the M3 scheme). */
object Semantic {
    val good = Green
    val bad = Red
    val warn = Amber
    val active = Blue
}

@Composable
fun RoostTheme(content: @Composable () -> Unit) {
    val colors = if (isSystemInDarkTheme()) DarkColors else LightColors
    MaterialTheme(colorScheme = colors) {
        // MainActivity calls enableEdgeToEdge(): the window draws under the status and
        // navigation bars. Nothing else in the tree was consuming those insets, so on the
        // dashboard the TopAppBar collapsed behind the (transparent) status bar — its only
        // overflow menu (Publish/Notifications/Schedules) became unreachable — and on the
        // session screen the back-arrow crowded the status-bar clock. Apply the system-bar
        // insets ONCE at the app root: windowInsetsPadding both pads AND consumes them, so
        // every screen sits below the status bar / above the nav bar and the per-screen
        // TopAppBar default insets downstream resolve to zero (no double padding). One
        // source of truth fixes the dashboard blocker and the session crowding together.
        Box(
            Modifier
                .fillMaxSize()
                .background(colors.background)
                .windowInsetsPadding(WindowInsets.systemBars),
        ) {
            content()
        }
    }
}
