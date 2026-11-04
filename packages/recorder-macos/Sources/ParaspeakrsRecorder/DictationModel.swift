import AppKit
import Foundation
@preconcurrency import RecorderCore

/// Dictation on a global shortcut, Ctrl+\ by default: hold to talk, or tap to start and tap again to stop.
/// Escape cancels. The transcript is typed into the frontmost app.
@MainActor
final class DictationModel: ObservableObject {
    enum Phase { case idle, recording, transcribing }

    /// Released sooner than this after the press, the key toggles instead of
    /// being held: recording goes on until the next press.
    static let holdThreshold = 0.35
    static let recentLimit = 5

    @Published private(set) var phase = Phase.idle
    /// False when another app already holds the shortcut.
    @Published private(set) var hotKeyActive = false
    @Published private(set) var shortcut = Shortcut.load(shortcutKey, default: .dictationDefault)
    private static let shortcutKey = "dictationShortcut"
    /// The last transcript that could not be typed, for the panel's Copy button.
    @Published private(set) var untyped: String?
    @Published private(set) var failure: String?
    /// The last transcripts, newest first, for the panel's list. In memory only,
    /// so nothing dictated is written to disk.
    @Published private(set) var recent: [String] = []

    private var hotKey: GlobalHotKey?
    private var escape: [GlobalHotKey] = []
    private var capture: DictationCapture?
    private var transcriber: DictationTranscriber?
    private var pressedAt = Date.distantPast
    private var latched = false
    private var askedForTrust = false
    /// The last app other than this one to be frontmost: opening the panel makes
    /// the recorder frontmost, and a past dictation is typed into the app before it.
    private var lastOtherApp: NSRunningApplication?
    private var activationObserver: NSObjectProtocol?

    init() {
        hotKey = register(shortcut)
        hotKeyActive = hotKey != nil
        let own = ProcessInfo.processInfo.processIdentifier
        if let front = NSWorkspace.shared.frontmostApplication, front.processIdentifier != own { lastOtherApp = front }
        activationObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] note in
            guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  app.processIdentifier != own else { return }
            MainActor.assumeIsolated { self?.lastOtherApp = app }
        }
    }

    private func register(_ shortcut: Shortcut) -> GlobalHotKey? {
        // Carbon delivers hot-key events on the main thread.
        GlobalHotKey.dictation(
            shortcut,
            onPress: { [weak self] in MainActor.assumeIsolated { self?.pressed() } },
            onRelease: { [weak self] in MainActor.assumeIsolated { self?.released() } })
    }

    /// Frees the shortcut while the panel records a new one.
    func suspendShortcut() { hotKey = nil }

    /// Registers `new`, or the current shortcut again when it is nil or taken.
    /// Returns why `new` was not used.
    func endEditingShortcut(_ new: Shortcut?) -> String? {
        if let new, let key = register(new) {
            hotKey = key
            shortcut = new
            new.save(Self.shortcutKey)
            hotKeyActive = true
            return nil
        }
        hotKey = register(shortcut)
        hotKeyActive = hotKey != nil
        return new.map { "\($0.label) is taken by another app." }
    }

    /// Types a past dictation into the app that was frontmost before the panel
    /// opened. The panel is closed first: open, it keeps the keyboard focus even
    /// when that app is frontmost again, and the key events would go to it.
    func retype(_ text: String) {
        guard phase == .idle else { return }
        guard let target = lastOtherApp, !target.isTerminated else {
            fail("No app to type into: switch to one, then open the panel again.", transcript: nil)
            return
        }
        Self.closePanel()
        target.activate()
        Task { @MainActor in
            // The key events go to the frontmost app, so wait until that is the target.
            let deadline = Date().addingTimeInterval(1)
            while NSWorkspace.shared.frontmostApplication != target, Date() < deadline {
                try? await Task.sleep(for: .milliseconds(20))
            }
            do throws(TextTyper.Failure) {
                try await TextTyper.type(text)
                failure = nil
                DictationTranscriber.append("typed \(text.count) characters again from the list")
            } catch {
                fail(error.message, transcript: nil)
            }
        }
    }

    /// Clicks the menu-bar icon, as orchetect/MenuBarExtraAccess does: closing
    /// the window directly leaves SwiftUI thinking the panel is open, and the
    /// next click on the icon is lost.
    private static func closePanel() {
        func button(in view: NSView?) -> NSStatusBarButton? {
            guard let view else { return nil }
            return view as? NSStatusBarButton ?? view.subviews.lazy.compactMap { button(in: $0) }.first
        }
        let icon = NSApp.windows.lazy.compactMap { button(in: $0.contentView) }.first
        if let icon { icon.performClick(nil) } else { NSApp.keyWindow?.close() }
    }

    private func remember(_ text: String) {
        recent.removeAll { $0 == text }
        recent.insert(text, at: 0)
        recent = Array(recent.prefix(Self.recentLimit))
    }

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
                failure = "Allow microphone access, then press \(shortcut.label) again."
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
        escape = GlobalHotKey.cancelDictation(held: shortcut.modifiers) { [weak self] in MainActor.assumeIsolated { self?.cancel() } }
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
                remember(text)
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
        phase = .idle
        DictationSounds.play(DictationSounds.failure)
        Notifier.post(title: "Dictation not typed", body: message)
        DictationTranscriber.append("\(message)\(wav.map { " Recording kept at \($0.path)." } ?? "")")
    }
}
