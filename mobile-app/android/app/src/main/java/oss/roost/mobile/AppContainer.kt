package oss.roost.mobile

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import oss.roost.mobile.net.ApiClient
import oss.roost.mobile.net.SseClient
import oss.roost.mobile.store.OfflineCache
import oss.roost.mobile.store.Pairing
import oss.roost.mobile.store.SecureStore

/**
 * Tiny manual DI container (no Hilt — weight budget). Holds the single ApiClient/SseClient
 * and the SecureStore, and exposes the paired/unpaired state as a flow so the nav host can
 * route to the pairing screen on a 401 (API.md §1: 401 → drop to pairing).
 */
class AppContainer(context: Context) {

    val store = SecureStore(context.applicationContext)
    val cache = OfflineCache(context.applicationContext)

    private val initial = store.loadPairing()
    val api = ApiClient(baseUrl = initial?.url ?: "", token = initial?.token)
    val sse = SseClient(api)

    private val _pairing = MutableStateFlow(initial)
    val pairing: StateFlow<Pairing?> = _pairing

    val isPaired: Boolean get() = _pairing.value != null

    fun setPaired(p: Pairing) {
        store.savePairing(p)
        api.baseUrl = p.url
        api.token = p.token
        _pairing.value = p
    }

    /** Called on a 401 anywhere: forget the token and bounce to pairing.
     *  The offline cache goes too — fleet goals/logs shouldn't outlive the pairing. */
    fun unpair() {
        store.clearPairing()
        cache.clear()
        api.token = null
        _pairing.value = null
    }

    companion object {
        @Volatile private var instance: AppContainer? = null
        fun get(context: Context): AppContainer =
            instance ?: synchronized(this) {
                instance ?: AppContainer(context).also { instance = it }
            }
    }
}
