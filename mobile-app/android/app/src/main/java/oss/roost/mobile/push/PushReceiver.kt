package oss.roost.mobile.push

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import oss.roost.mobile.MainActivity
import oss.roost.mobile.R
import oss.roost.mobile.model.NotifyRoute
import oss.roost.mobile.model.NotifyRouter

/**
 * DEVICE-ONLY half of push notifications (R37 / DESIGN.md §6 v1.1). UNTESTED on
 * the JVM harness — there is no Android runtime, no NotificationManager, and no
 * UnifiedPush distributor here. The PURE routing/topic logic this leans on
 * ([NotifyRouter], [oss.roost.mobile.model.NtfyTopic]) IS JVM-tested; this file is
 * the thin, obvious binding around it.
 *
 * DESIGN.md §6 picks "ntfy.sh self-hosted or UnifiedPush-style webhooks" over
 * FCM to stay dependency-light. The honest Android v1.1 path is UnifiedPush: the
 * user installs a distributor (e.g. the ntfy app or ntfy-android acting as the
 * distributor), the app registers, and the distributor delivers the CP's POST as
 * a `PushMessage`. That registration/binding requires the `unifiedpush` connector
 * library + a `MessagingReceiver` subclass and a real device, so it is the capped
 * piece. What is wired here is the part that DOESN'T need extra infra: given a
 * delivered message body (the R37 JSON payload), build a tappable system
 * notification whose tap deep-links via the SAME pure router the tests pin.
 */
object PushNotifier {
    const val CHANNEL_ID = "roost_jobs"
    const val EXTRA_ROUTE_JOB_ID = "roost_route_job_id"

    /** Create the notification channel (idempotent). Call once at app start. */
    fun ensureChannel(context: Context) {
        val mgr = context.getSystemService(NotificationManager::class.java) ?: return
        val ch = NotificationChannel(
            CHANNEL_ID, "Job updates", NotificationManager.IMPORTANCE_DEFAULT
        ).apply { description = "Terminal-state notifications for your Roost jobs." }
        mgr.createNotificationChannel(ch)
    }

    /**
     * Render a delivered R37 payload as a system notification. The tap PendingIntent
     * carries the routed job id (decided by the pure [NotifyRouter]); MainActivity
     * reads [EXTRA_ROUTE_JOB_ID] and navigates to that Session (or the Dashboard
     * when the payload was malformed and the router fell back).
     */
    fun show(context: Context, rawPayloadJson: String) {
        val payload = NotifyRouter.decode(rawPayloadJson)
        val route = NotifyRouter.route(payload)
        val title = payload?.let { "Roost job ${it.jobId ?: "?"} ${it.state ?: ""}".trim() }
            ?: "Roost"
        val body = payload?.message ?: "A job finished."

        val tapIntent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            if (route is NotifyRoute.Session) putExtra(EXTRA_ROUTE_JOB_ID, route.jobId)
        }
        val pending = PendingIntent.getActivity(
            context, route.hashCode(), tapIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val notif = NotificationCompat.Builder(context, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(body)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()

        val mgr = context.getSystemService(NotificationManager::class.java) ?: return
        // One notification per job id so re-runs don't stack endlessly.
        mgr.notify((payload?.jobId ?: "roost").hashCode(), notif)
    }
}
