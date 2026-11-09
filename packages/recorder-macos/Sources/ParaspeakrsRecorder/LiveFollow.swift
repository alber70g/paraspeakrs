import AppKit
import Foundation

/// Runs `paraspeakrs live follow` for a recording and opens its page. One follow
/// server at a time, on a fixed port, so the page has the same address for every
/// meeting. The server outlives the recording, so the page stays readable; it
/// ends when the next recording is followed or the app quits.
@MainActor
final class LiveFollow {
    static let port = 8765
    static let page = URL(string: "http://127.0.0.1:\(port)/")!
    private static let stateURL = URL(string: "http://127.0.0.1:\(port)/api/state")!
    /// The page answers once Python has imported its packages; the ASR model loads after.
    static let startTimeout = 30.0
    /// The server gives open pages 2 s on SIGTERM; a transcription in progress can hold it longer.
    static let stopGrace = 5.0

    enum Phase { case off, starting, running }

    /// `.running` only while our own process runs.
    private(set) var phase = Phase.off { didSet { onChange?(phase) } }
    var onChange: ((Phase) -> Void)?
    /// The message and, when there is one, the server's log.
    var onFailure: ((String, URL?) -> Void)?

    private var process: Process?

    /// Opens the page of the follow server for `base`, starting one when none answers for it.
    func open(base: URL, executable: String) {
        guard phase != .starting else { return }
        phase = .starting
        Task {
            do {
                try await serve(base, executable: executable)
                NSWorkspace.shared.open(Self.page)
            } catch let failure as FollowError {
                onFailure?(failure.message, failure.log)
            } catch {
                onFailure?("Live follow: \(error.localizedDescription)", nil)
            }
            phase = process?.isRunning == true ? .running : .off
        }
    }

    /// For app quit: the server shuts down by itself after SIGTERM.
    func stop() { process?.terminate() }

    private func serve(_ base: URL, executable: String) async throws {
        if let found = await Self.current() {
            if found.follows(base) { return }
            guard let process, process.isRunning else {
                throw FollowError(
                    message: "Port \(Self.port) is in use by a live follow of \(found.label). Stop it to follow this recording.",
                    log: nil)
            }
            await end(process)
        } else if let process, process.isRunning {
            await end(process)  // still starting, for an earlier recording
        }

        let log = base.appendingPathExtension("live.log")
        let process = try launch(base, executable: executable, log: log)
        self.process = process
        let deadline = Date().addingTimeInterval(Self.startTimeout)
        while Date() < deadline {
            try? await Task.sleep(for: .milliseconds(500))
            if let found = await Self.current(), found.follows(base) { return }
            if !process.isRunning {
                let reason = Transcriber.lastLine(of: log) ?? "exit status \(process.terminationStatus)"
                throw FollowError(message: "Live follow stopped: \(reason)", log: log)
            }
        }
        throw FollowError(message: "Live follow did not answer within \(Int(Self.startTimeout)) s.", log: log)
    }

    private func launch(_ base: URL, executable: String, log: URL) throws -> Process {
        let path = (executable as NSString).expandingTildeInPath
        guard FileManager.default.isExecutableFile(atPath: path) else {
            throw FollowError(message: "paraspeakrs not found at \(path)", log: nil)
        }
        FileManager.default.createFile(atPath: log.path, contents: nil)
        let output = try FileHandle(forWritingTo: log)
        defer { try? output.close() }  // the child keeps its own copy

        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = [
            "live", "follow", base.appendingPathExtension("mic.caf").path, "--no-browser", "--port", "\(Self.port)",
        ]
        process.standardOutput = output
        process.standardError = output
        process.environment = Transcriber.environment()
        process.terminationHandler = { [weak self] ended in
            DispatchQueue.main.async {
                guard let self, self.process === ended else { return }
                self.process = nil
                if self.phase == .running { self.phase = .off }
            }
        }
        try process.run()
        return process
    }

    /// SIGTERM, then SIGKILL after `stopGrace`.
    private func end(_ process: Process) async {
        process.terminate()
        let deadline = Date().addingTimeInterval(Self.stopGrace)
        while process.isRunning, Date() < deadline { try? await Task.sleep(for: .milliseconds(100)) }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
            while process.isRunning { try? await Task.sleep(for: .milliseconds(100)) }
        }
    }

    /// What the follow server on the port follows, or nil when none answers.
    private static func current() async -> Following? {
        var request = URLRequest(url: stateURL)
        request.timeoutInterval = 1
        guard let (data, _) = try? await URLSession.shared.data(for: request),
              let state = try? JSONDecoder().decode(ServerState.self, from: data)
        else { return nil }
        return state.source
    }

    private struct ServerState: Decodable { let source: Following }

    /// `/api/state`'s `source`: `watching` is the path `live follow` was given,
    /// `name` the recording it found there (empty until then).
    private struct Following: Decodable {
        let name: String?
        let watching: String?

        var label: String { (name?.isEmpty == false ? name : watching) ?? "another source" }

        func follows(_ base: URL) -> Bool {
            if name == base.lastPathComponent || watching == base.appendingPathExtension("mic.caf").path { return true }
            // Started on the recordings folder and still waiting: it takes the newest recording there.
            return name?.isEmpty != false && watching == base.deletingLastPathComponent().path
        }
    }
}

struct FollowError: Error {
    let message: String
    let log: URL?
}
