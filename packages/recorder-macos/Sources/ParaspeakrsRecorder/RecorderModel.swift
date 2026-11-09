import AppKit
import Foundation
@preconcurrency import RecorderCore

/// Recording state for the menu-bar panel. Everything here runs on the main
/// thread; merging and transcription run on background queues.
@MainActor
final class RecorderModel: ObservableObject {
    static let recordingsDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Documents/paraspeakrs-recordings")
    /// The tap delivers nothing while no app plays sound, and only zeros
    /// without the system-audio permission; both look the same from here.
    static let silenceWarningSeconds = 30.0

    @Published private(set) var startedAt: Date?
    @Published private(set) var error: String? { didSet { errorLog = nil } }
    /// The log that goes with `error`, for the panel's Report button.
    private(set) var errorLog: URL?
    @Published private(set) var systemSilent = false
    @Published private(set) var finishing = 0
    @Published private(set) var transcribing = 0
    /// Set when a recording starts: the panel and the pop-up ask whether to follow it live.
    @Published private(set) var offerLiveFollow = false {
        didSet {
            guard offerLiveFollow != oldValue else { return }
            if offerLiveFollow {
                prompt.show(open: { [weak self] in self?.openLiveFollow() },
                            dismiss: { [weak self] in self?.dismissLiveFollow() })
            } else {
                prompt.close()
            }
        }
    }
    @Published private(set) var liveFollowPhase = LiveFollow.Phase.off
    /// While recording, one press of the shortcut is left to the frontmost app
    /// (⇧⌘M mutes in Teams); two presses within this many seconds stop.
    static let doubleTapInterval = 0.5

    @Published private(set) var shortcut = Shortcut.load(shortcutKey, default: .meetingDefault)
    private static let shortcutKey = "meetingShortcut"

    private(set) var capture: MeetingCapture?
    private var watchdog: Timer?
    private var rebuiltForSilence = false
    private var deviceObserver: DeviceChangeObserver?
    private let transcriber = Transcriber()
    private let background = DispatchQueue(label: "paraspeakrs.recorder.finish")
    private var keyMonitor: KeyMonitor?
    private var lastPress = Date.distantPast
    private let liveFollow = LiveFollow()
    private let prompt = LiveFollowPrompt()
    /// The recording live follow is for: the current one, or the last one.
    private var liveBase: URL?

    var isRecording: Bool { startedAt != nil }
    /// "Open live meeting" while recording, and after it while our follow server still runs.
    var canOpenLiveMeeting: Bool { liveBase != nil && (isRecording || liveFollowPhase == .running) }

    init() {
        transcriber.onChange = { [weak self] count in self?.transcribing = count }
        transcriber.onFailure = { [weak self] message, log in
            self?.error = message
            self?.errorLog = log
        }
        liveFollow.onChange = { [weak self] phase in self?.liveFollowPhase = phase }
        liveFollow.onFailure = { [weak self] message, log in
            self?.error = message
            self?.errorLog = log
        }
        _ = NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in MainActor.assumeIsolated { self?.liveFollow.stop() } }
        Notifier.requestPermission()
        resumeShortcut()
    }

    /// Stops watching the shortcut while the panel records a new one.
    func suspendShortcut() { keyMonitor = nil }

    func resumeShortcut() {
        keyMonitor = KeyMonitor(shortcut) { [weak self] in self?.shortcutPressed() }
    }

    /// Takes effect on the next `resumeShortcut`.
    func setShortcut(_ new: Shortcut) {
        shortcut = new
        new.save(Self.shortcutKey)
    }

    /// One press starts; while recording, only a second press soon after the
    /// first stops, so a single press can mute or unmute in the meeting app.
    private func shortcutPressed() {
        guard isRecording else {
            lastPress = .distantPast
            start()
            return
        }
        let now = Date()
        if now.timeIntervalSince(lastPress) < Self.doubleTapInterval {
            lastPress = .distantPast
            stop(executable: Self.executable)
        } else {
            lastPress = now
        }
    }

    func toggle() {
        if isRecording { stop(executable: Self.executable) } else { start() }
    }

    func openLiveFollow() {
        offerLiveFollow = false
        guard let liveBase else { return }
        liveFollow.open(base: liveBase, executable: Self.executable)
    }

    func dismissLiveFollow() { offerLiveFollow = false }

    /// The path the panel's settings field stores; read at stop time so a
    /// recording started from the shortcut uses the current setting.
    static var executable: String {
        UserDefaults.standard.string(forKey: "paraspeakrsPath") ?? Transcriber.defaultExecutable
    }

    private func start() {
        error = nil
        Task { @MainActor in
            guard await MicrophonePermission.request() else {
                error = "Microphone access is off for Paraspeakrs Recorder."
                return
            }
            do {
                try FileManager.default.createDirectory(at: Self.recordingsDirectory, withIntermediateDirectories: true)
                let base = Self.newBase()
                let capture = MeetingCapture(base: base)
                try capture.start()
                self.capture = capture
                startedAt = Date()
                liveBase = base
                offerLiveFollow = true
                rebuiltForSilence = false
                systemSilent = false
                // Both callbacks are delivered on the main queue.
                deviceObserver = DeviceChangeObserver { [weak self] in MainActor.assumeIsolated { self?.devicesChanged() } }
                watchdog = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                    MainActor.assumeIsolated { self?.checkSilence() }
                }
            } catch {
                self.error = "Could not start recording: \(error)"
            }
        }
    }

    private func stop(executable: String) {
        guard let capture else { return }
        watchdog?.invalidate()
        watchdog = nil
        deviceObserver = nil
        self.capture = nil
        startedAt = nil
        systemSilent = false
        offerLiveFollow = false
        finishing += 1
        Task { @MainActor in
            // The capture is no longer touched here once it is handed over.
            let outcome = await withCheckedContinuation { done in
                background.async { done.resume(returning: Self.finish(capture)) }
            }
            finishing -= 1
            switch outcome {
            case .success(let recording):
                transcriber.enqueue(recording, executable: executable)
            case .failure(let failure):
                error = failure.message
                Notifier.post(title: "Recording could not be saved", body: failure.message)
                NSWorkspace.shared.activateFileViewerSelecting([capture.micURL, capture.systemURL])
            }
        }
    }

    /// Closes the tracks, merges them into `<base>.m4a` and deletes the CAFs.
    /// On failure the CAFs stay, so nothing recorded is lost.
    nonisolated private static func finish(_ capture: MeetingCapture) -> Result<URL, FinishError> {
        let output = capture.micURL.deletingPathExtension().deletingPathExtension().appendingPathExtension("m4a")
        do {
            try capture.stop()
            try StereoMerge.merge(left: capture.micURL, right: capture.systemURL, to: output)
        } catch {
            return .failure(FinishError(message: "Saving \(output.lastPathComponent) failed: \(error)"))
        }
        try? FileManager.default.removeItem(at: capture.micURL)
        try? FileManager.default.removeItem(at: capture.systemURL)
        return .success(output)
    }

    private func devicesChanged() {
        do { try capture?.rebuild() } catch { self.error = "Audio device change: \(error)" }
    }

    private func checkSilence() {
        guard let capture else { return }
        systemSilent = capture.levels.secondsSinceSystemSignal > Self.silenceWarningSeconds
        // One retry per recording: on macOS 26.5 a tap can go silent mid-session
        // and only a rebuild brings it back.
        if systemSilent, !rebuiltForSilence {
            rebuiltForSilence = true
            try? capture.rebuildSystem()
        }
    }

    /// `2026-11-04_1430`, with `-2`, `-3`... when that minute is taken.
    private static func newBase() -> URL {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd_HHmm"
        let stem = formatter.string(from: Date())
        var candidate = recordingsDirectory.appendingPathComponent(stem)
        var n = 2
        while FileManager.default.fileExists(atPath: candidate.appendingPathExtension("m4a").path)
            || FileManager.default.fileExists(atPath: candidate.appendingPathExtension("mic.caf").path) {
            candidate = recordingsDirectory.appendingPathComponent("\(stem)-\(n)")
            n += 1
        }
        return candidate
    }
}

struct FinishError: Error { let message: String }
