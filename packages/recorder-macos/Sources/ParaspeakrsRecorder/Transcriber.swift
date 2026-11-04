import AppKit
import Foundation
import UserNotifications

/// Runs `paraspeakrs run` on finished recordings, one at a time: each
/// transcription peaks at 3-4 GB RAM, so two in parallel would double that.
/// The job lands in the store `paraspeakrs tui` reads.
final class Transcriber {
    static let defaultExecutable = "~/.local/bin/paraspeakrs"

    private let queue = DispatchQueue(label: "paraspeakrs.recorder.transcriber")
    private var pending = 0
    /// Called on the main queue with the number of queued or running jobs.
    var onChange: ((Int) -> Void)?

    func enqueue(_ recording: URL, executable: String) {
        changePending(by: 1)
        queue.async { [weak self] in
            guard let self else { return }
            let log = recording.deletingPathExtension().appendingPathExtension("log")
            let result = Self.run(recording, executable: (executable as NSString).expandingTildeInPath, log: log)
            switch result {
            case .success:
                Notifier.post(title: "Transcript ready", body: recording.lastPathComponent)
            case .failure(let message):
                Notifier.post(title: "Transcription failed", body: message)
                DispatchQueue.main.async { NSWorkspace.shared.activateFileViewerSelecting([recording, log]) }
            }
            changePending(by: -1)
        }
    }

    private func changePending(by delta: Int) {
        DispatchQueue.main.async {
            self.pending += delta
            self.onChange?(self.pending)
        }
    }

    enum Outcome { case success, failure(String) }

    private static func run(_ recording: URL, executable: String, log: URL) -> Outcome {
        guard FileManager.default.isExecutableFile(atPath: executable) else {
            return .failure("paraspeakrs not found at \(executable)")
        }
        FileManager.default.createFile(atPath: log.path, contents: nil)
        guard let output = try? FileHandle(forWritingTo: log) else { return .failure("cannot write \(log.path)") }
        defer { try? output.close() }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = ["run", "--single-speaker-channel", "L", "--txt", recording.path]
        process.standardOutput = output
        process.standardError = output
        process.environment = environment()
        do {
            try process.run()
        } catch {
            return .failure("could not start paraspeakrs: \(error.localizedDescription)")
        }
        process.waitUntilExit()
        if process.terminationStatus == 0 { return .success }
        return .failure(lastLine(of: log) ?? "paraspeakrs exited with status \(process.terminationStatus)")
    }

    /// A GUI app gets launchd's minimal PATH; paraspeakrs shells out to ffmpeg,
    /// which Homebrew installs outside it.
    private static func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let extra = ["~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"].map { ($0 as NSString).expandingTildeInPath }
        env["PATH"] = (extra + [env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"]).joined(separator: ":")
        env["PARAKEET_NONINTERACTIVE"] = "1"
        return env
    }

    private static func lastLine(of file: URL) -> String? {
        guard let text = try? String(contentsOf: file, encoding: .utf8) else { return nil }
        return text.split(whereSeparator: \.isNewline).last.map(String.init)
    }
}

enum Notifier {
    static func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func post(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
