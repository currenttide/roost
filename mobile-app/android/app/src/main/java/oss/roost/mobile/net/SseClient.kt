package oss.roost.mobile.net

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import oss.roost.mobile.model.StreamEvent
import oss.roost.mobile.sse.SseFrames
import java.io.InputStream
import java.net.HttpURLConnection
import kotlin.random.Random

/**
 * Hand-rolled SSE client over HttpURLConnection (API.md §5). Runs on Dispatchers.IO inside
 * a caller-owned coroutine; cancel the coroutine to stop the stream.
 *
 * It does ONLY transport: connect, read chunks, split frames, emit StreamEvents via
 * onEvent, and reconnect with jittered exponential backoff (1s→30s). The resume cursor
 * (`since`) and dedupe live in the ViewModel/LogBuffer — this client just streams from the
 * latest `since` the caller provides via the lambda, so a reconnect resumes correctly.
 *
 * WHY a sinceProvider lambda instead of a fixed value: between drops the caller advances
 * its max-seq cursor; on reconnect we must resume from the NEW cursor, not the old one.
 */
class SseClient(private val api: ApiClient) {

    /**
     * Stream events until the coroutine is cancelled or a `done` event arrives. Suspends.
     *
     * @param jobId        job to stream
     * @param sinceProvider returns the current max-seq cursor (called on each connect)
     * @param onEvent      invoked for every parsed event (State/Log/Done/Err)
     * @param onConnected  invoked once per successful connect (UI can clear "reconnecting")
     */
    suspend fun stream(
        jobId: String,
        sinceProvider: () -> Int,
        onEvent: suspend (StreamEvent) -> Unit,
        onConnected: suspend () -> Unit = {},
    ) {
        var backoffMs = 1_000L
        while (currentCoroutineContext().isActive) {
            try {
                val done = connectOnce(jobId, sinceProvider(), onEvent, onConnected)
                if (done) return            // server closed after `done`; we're finished.
                backoffMs = 1_000L          // clean EOF without done → reset and reconnect.
            } catch (ce: CancellationException) {
                throw ce
            } catch (_: Exception) {
                // network flap; fall through to backoff.
            }
            // Jittered exponential backoff, capped at 30s (API.md §5 rule 3).
            val jitter = Random.nextLong(0, backoffMs / 2 + 1)
            delay(backoffMs + jitter)
            backoffMs = (backoffMs * 2).coerceAtMost(30_000L)
        }
    }

    /** @return true if a `done` event was seen (stream is complete). */
    private suspend fun connectOnce(
        jobId: String,
        since: Int,
        onEvent: suspend (StreamEvent) -> Unit,
        onConnected: suspend () -> Unit,
    ): Boolean = withContext(Dispatchers.IO) {
        val conn: HttpURLConnection = api.openAuthed("/jobs/$jobId/stream?since=$since")
        conn.connectTimeout = 10_000
        // Long-lived stream. We cap the read timeout (not 0) so a fully-stalled socket
        // eventually throws SocketTimeoutException and we reconnect — HttpURLConnection's
        // blocking read() can't be interrupted by coroutine cancellation mid-read, so a
        // bounded timeout is how a dead connection is reclaimed. The CP sends data/heartbeat
        // frequently enough that 90s never trips on a healthy stream.
        conn.readTimeout = 90_000
        conn.setRequestProperty("Accept", "text/event-stream")
        try {
            val code = conn.responseCode
            if (code !in 200..299) {
                // 401/403/404 here are surfaced as an error event so the VM can react
                // (e.g. unpair on 401) without a separate channel.
                onEvent(StreamEvent.Err("stream http $code"))
                return@withContext code == 404 // 404 = job gone; stop trying.
            }
            onConnected()
            val stream: InputStream = conn.inputStream
            val buffer = SseFrames.FrameBuffer()
            val reader = stream.bufferedReader(Charsets.UTF_8)
            // Read decoded text in blocks; feed into the frame buffer. Using a char buffer
            // keeps multibyte UTF-8 intact across chunk boundaries.
            val cbuf = CharArray(4096)
            while (true) {
                currentCoroutineContext().ensureActive()
                val n = reader.read(cbuf)
                if (n < 0) break // EOF
                val frames = buffer.feed(String(cbuf, 0, n))
                for (block in frames) {
                    val ev = SseFrames.parseFrame(block) ?: continue
                    onEvent(ev)
                    if (ev is StreamEvent.Done) return@withContext true
                }
            }
            false
        } finally {
            conn.disconnect()
        }
    }
}
