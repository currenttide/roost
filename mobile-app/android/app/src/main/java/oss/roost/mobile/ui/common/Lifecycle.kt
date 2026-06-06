package oss.roost.mobile.ui.common

import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver

/**
 * Run [onResume] when the screen enters RESUMED and [onPause] when it leaves, cleaning up
 * the observer on dispose. This is how the dashboard pauses polling in the background and
 * the session re-pages logs on foreground (API.md §2/§5, DESIGN §7 "SSE only while
 * foregrounded").
 */
@Composable
fun LifecycleResume(onResume: () -> Unit, onPause: () -> Unit) {
    val owner = LocalLifecycleOwner.current
    val resume = rememberUpdatedState(onResume)
    val pause = rememberUpdatedState(onPause)
    DisposableEffect(owner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_RESUME -> resume.value()
                Lifecycle.Event.ON_PAUSE -> pause.value()
                else -> {}
            }
        }
        owner.lifecycle.addObserver(observer)
        onDispose {
            owner.lifecycle.removeObserver(observer)
            pause.value()
        }
    }
}
