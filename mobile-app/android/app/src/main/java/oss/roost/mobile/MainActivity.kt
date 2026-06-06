package oss.roost.mobile

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import oss.roost.mobile.ui.RoostTheme
import oss.roost.mobile.ui.RoostNavHost

/**
 * Single-activity host. Handles the roost:// pairing deep link (launchMode=singleTask, so
 * a deep link while running arrives via onNewIntent). The actual decode happens in the
 * pairing screen's ViewModel; here we just surface the URI to Compose.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val container = AppContainer.get(this)

        setContent {
            // The deep-link URI is kept in Compose state so a fresh launch (onCreate intent)
            // and a warm launch (onNewIntent) both flow to the pairing screen.
            val deepLink = remember { mutableStateOf(intent?.dataString) }
            // Re-read on new intents via the activity field set in onNewIntent.
            val pairing by container.pairing.collectAsState()

            RoostTheme {
                RoostNavHost(
                    container = container,
                    isPaired = pairing != null,
                    pendingDeepLink = deepLink.value,
                    onDeepLinkConsumed = { deepLink.value = null },
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        // Restart content path is overkill; instead recreate to re-run setContent with the
        // new intent's data. Pairing is rare, so a recreate here is acceptable and simple.
        if (intent.dataString?.startsWith("roost://") == true) recreate()
    }
}
