import AppKit
import Foundation
@preconcurrency import RecorderCore

/// Ctrl+\ dictation: hold to talk, or tap to start and tap again to stop.
/// Escape cancels. The transcript is typed into the frontmost app.
@MainActor
final class DictationModel: ObservableObject {
    enum Phase { case idle, recording, transcribing }

    /// Released sooner than this after the press, the key toggles instead of
    /// being held: recording goes on until the next press.
    static let holdThreshold = 0.35

    @Published private(set) var phase = Phase.idle
    /// False when another app already holds Ctrl+\.
    @Published private(set) var hotKeyActive = false
    @Published private(set) var accessibilityTrusted = TextTyper.isTrusted
    /// The last transcript that could not be typed, for the panel's Copy button.
    @Published private(set) var untyped: String?
    @Published private(set) var failure: String?

    private var hotKey: GlobalHotKey?
    private var escape: [GlobalHotKey] = []
    private var capture: DictationCapture?
    private var transcriber: DictationTranscriber?
    private var pressedAt = Date.distantPast
    private var latched = false
    private var askedForTrust = false

    init() {
        // Carbon delivers hot-key events on the main thread.
        hotKey = GlobalHotKey.dictation(
            onPress: { [weak self] in MainActor.assumeIsolated { self?.pressed() } },
            onRelease: { [weak self] in MainActor.assumeIsolated { self?.released() } })
        hotKeyActive = hotKey != nil
    }

    func refreshTrust() { accessibilityTrusted = TextTyper.isTrusted }

    private func pressed() {
        switch phase {
        case .idle: start()
        case .recording where latched: stop()
        case .recording, .transcribing: break
        }
    }

    private func released() {
        guard phase == .recording, !latched else { return }
        if Date().timeIntervalSince(pressedAt) >= Self.holdThreshold { stop() } else { latched = true }
    }

    private func start() {
        guard MicrophonePermission.isGranted else {
            if MicrophonePermission.isDenied {
                fail("Microphone access is off for Paraspeakrs Recorder.", transcript: nil)
            } else {
                Task { _ = await MicrophonePermission.request() }
                failure = "Allow microphone access, then press Ctrl+\\ again."
                DictationTranscriber.append("asked for microphone access; dictation not started")
            }
            return
        }
        if !TextTyper.isTrusted, !askedForTrust {
            askedForTrust = true
            TextTyper.requestTrust()
        }
        let wav = FileManager.default.temporaryDirectory.appendingPathComponent("dictation-\(UUID().uuidString).wav")
        let capture = DictationCapture(url: wav)
        do {
            try capture.start()
        } catch {
            fail("Could not start the microphone: \(error)", transcript: nil)
            return
        }
        // Loads the model while the user speaks.
        transcriber = DictationTranscriber(executable: RecorderModel.executable)
        self.capture = capture
        pressedAt = Date()
        latched = false
        failure = nil
        phase = .recording
        escape = GlobalHotKey.cancelDictation { [weak self] in MainActor.assumeIsolated { self?.cancel() } }
        DictationSounds.play(DictationSounds.start)
        DictationTranscriber.append("dictation started: \(wav.path)")
    }

    private func stop() {
        guard let capture, let transcriber else { return }
        releaseEscape()
        self.capture = nil
        self.transcriber = nil
        phase = .transcribing
        DictationSounds.play(DictationSounds.stop)
        do {
            try capture.stop()
        } catch {
            transcriber.cancel()
            fail("Could not save the dictation: \(error)", transcript: nil, keeping: capture.url)
            return
        }
        Task { @MainActor in
            switch await transcriber.transcribe(capture.url) {
            case .failure(let error):
                fail(error.message, transcript: nil, keeping: capture.url)
            case .success(let text) where text.isEmpty:
                fail("Nothing was recognised.", transcript: nil, keeping: capture.url)
            case .success(let text):
                do throws(TextTyper.Failure) {
                    try await TextTyper.type(text)
                    DictationTranscriber.append("typed \(text.count) characters")
                    untyped = nil
                    try? FileManager.default.removeItem(at: capture.url)
                    phase = .idle
                } catch {
                    fail(error.message, transcript: text, keeping: capture.url)
                }
            }
        }
    }

    private func cancel() {
        guard let capture else { return }
        releaseEscape()
        try? capture.stop()
        try? FileManager.default.removeItem(at: capture.url)
        transcriber?.cancel()
        self.capture = nil
        transcriber = nil
        phase = .idle
        DictationSounds.play(DictationSounds.cancel)
        DictationTranscriber.append("dictation cancelled with Escape")
    }

    /// Escape's own Carbon handler may be the caller, so the hot key is freed
    /// after it returns rather than from inside it.
    private func releaseEscape() {
        let escape = self.escape
        self.escape = []
        DispatchQueue.main.async { withExtendedLifetime(escape) {} }
    }

    private func fail(_ message: String, transcript: String?, keeping wav: URL? = nil) {
        failure = message
        if let transcript { untyped = transcript }
        refreshTrust()
        phase = .idle
        DictationSounds.play(DictationSounds.failure)
        Notifier.post(title: "Dictation not typed", body: message)
        DictationTranscriber.append("\(message)\(wav.map { " Recording kept at \($0.path)." } ?? "")")
    }
}
