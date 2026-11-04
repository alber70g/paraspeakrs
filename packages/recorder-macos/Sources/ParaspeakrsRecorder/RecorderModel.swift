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
    /// False when another app already holds ⌘⇧M.
    @Published private(set) var hotKeyActive = false

    private(set) var capture: MeetingCapture?
    private var watchdog: Timer?
    private var rebuiltForSilence = false
    private var deviceObserver: DeviceChangeObserver?
    private let transcriber = Transcriber()
    private let background = DispatchQueue(label: "paraspeakrs.recorder.finish")
    private var hotKey: GlobalHotKey?

    var isRecording: Bool { startedAt != nil }

    init() {
        transcriber.onChange = { [weak self] count in self?.transcribing = count }
        transcriber.onFailure = { [weak self] message, log in
            self?.error = message
            self?.errorLog = log
        }
        Notifier.requestPermission()
        // Carbon delivers hot-key events on the main thread.
        hotKey = GlobalHotKey.meetingRecording { [weak self] in MainActor.assumeIsolated { self?.toggle() } }
        hotKeyActive = hotKey != nil
    }

    func toggle() {
        if isRecording { stop(executable: Self.executable) } else { start() }
    }

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
                let capture = MeetingCapture(base: Self.newBase())
                try capture.start()
                self.capture = capture
                startedAt = Date()
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
