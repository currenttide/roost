package oss.roost.mobile.voice

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * Thin wrapper over android.speech.SpeechRecognizer for hold-to-talk dictation (DESIGN §4).
 *
 * On-device only: EXTRA_PREFER_OFFLINE so no audio leaves the phone where a local model
 * exists; EXTRA_PARTIAL_RESULTS so the field updates live while held. The recognizer must
 * be created and driven on the main thread.
 *
 * Caller flow: check isAvailable() to decide whether to show the mic; on press start(),
 * on release stop(). Partials and the final transcript arrive via the callbacks.
 */
class Dictation(private val context: Context) {

    private var recognizer: SpeechRecognizer? = null
    var listening: Boolean = false
        private set

    fun isAvailable(): Boolean = SpeechRecognizer.isRecognitionAvailable(context)

    /**
     * Begin listening. [onPartial] fires repeatedly with the best-so-far transcript;
     * [onFinal] fires once with the settled text (or empty); [onError] with a code.
     */
    fun start(
        onPartial: (String) -> Unit,
        onFinal: (String) -> Unit,
        onError: (Int) -> Unit,
    ) {
        if (listening) return
        val r = SpeechRecognizer.createSpeechRecognizer(context)
        recognizer = r
        r.setRecognitionListener(object : RecognitionListener {
            override fun onPartialResults(partialResults: Bundle?) {
                firstResult(partialResults)?.let(onPartial)
            }
            override fun onResults(results: Bundle?) {
                listening = false
                onFinal(firstResult(results) ?: "")
            }
            override fun onError(error: Int) {
                listening = false
                onError(error)
            }
            override fun onReadyForSpeech(params: Bundle?) {}
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {}
            override fun onEvent(eventType: Int, params: Bundle?) {}
        })
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
            )
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        }
        listening = true
        r.startListening(intent)
    }

    /** Stop on release; the final transcript still arrives via onResults. */
    fun stop() {
        recognizer?.stopListening()
    }

    fun destroy() {
        recognizer?.destroy()
        recognizer = null
        listening = false
    }

    private fun firstResult(bundle: Bundle?): String? =
        bundle?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
}
