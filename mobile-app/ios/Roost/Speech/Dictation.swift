import Foundation
import Speech
import AVFoundation

/// Hold-to-talk on-device dictation (DESIGN §4). Wraps SFSpeechRecognizer +
/// AVAudioEngine: start on press, stream partial transcripts into the bound
/// field, stop on release. On-device where available (no audio leaves the
/// phone); mic is hidden by the view when `isAvailable` is false.
///
/// WHY an ObservableObject: the New-session sheet observes `transcript` and
/// `isRecording` to live-update the text field and button state.
@MainActor
final class Dictation: ObservableObject {
    @Published var transcript: String = ""
    @Published var isRecording: Bool = false
    @Published var authorized: Bool = false

    /// Recognizer may be nil if the device/locale has none → hide the mic.
    private let recognizer = SFSpeechRecognizer()
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?

    var isAvailable: Bool { recognizer?.isAvailable == true }

    /// Ask for speech + mic permission. Call before the first record.
    func requestAuthorization() async {
        let speechOK = await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { status in
                cont.resume(returning: status == .authorized)
            }
        }
        let micOK = await withCheckedContinuation { cont in
            // AVAudioApplication.requestRecordPermission is iOS 17+; the older
            // AVAudioSession.requestRecordPermission is deprecated there.
            AVAudioApplication.requestRecordPermission { granted in
                cont.resume(returning: granted)
            }
        }
        authorized = speechOK && micOK
    }

    /// Begin capturing. `seed` is the field's current text so dictation appends
    /// rather than replaces; partials are appended to it live.
    func start(seed: String) {
        guard isAvailable, !isRecording else { return }
        let recognizer = recognizer!
        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.record, mode: .measurement,
                                         options: .duckOthers)
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)

            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            // Prefer on-device where the model supports it (DESIGN §4).
            if recognizer.supportsOnDeviceRecognition {
                request.requiresOnDeviceRecognition = true
            }
            self.request = request

            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buf, _ in
                self?.request?.append(buf)
            }
            engine.prepare()
            try engine.start()

            let base = seed.isEmpty ? "" : seed + " "
            task = recognizer.recognitionTask(with: request) { [weak self] result, error in
                guard let self else { return }
                if let result {
                    self.transcript = base + result.bestTranscription.formattedString
                }
                if error != nil || (result?.isFinal ?? false) {
                    self.teardown()
                }
            }
            transcript = seed
            isRecording = true
            Haptics.tap()
        } catch {
            teardown()
        }
    }

    /// Stop capturing; the last partial stays in `transcript` for editing.
    func stop() {
        guard isRecording else { return }
        Haptics.tap()
        request?.endAudio()
        teardown()
    }

    private func teardown() {
        if engine.isRunning {
            engine.stop()
            engine.inputNode.removeTap(onBus: 0)
        }
        task?.cancel()
        task = nil
        request = nil
        try? AVAudioSession.sharedInstance().setActive(false,
                                                       options: .notifyOthersOnDeactivation)
        isRecording = false
    }
}
