import Foundation

/// One `paraspeakrs dictate --serve` process per dictation. Started on
/// key-down, so the model (0.8 s to load) loads while the user speaks; after
/// release only the decode is left. Closing stdin ends the process, so it holds
/// no memory between dictations.
final class DictationTranscriber {
    static let log = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Paraspeakrs Recorder/dictation.log")

    private struct Reply: Decodable, Sendable {
        var ready: Bool?
        var text: String?
        var error: String?
    }

    private let executable: String
    private let process = Process()
    private let input = Pipe()
    private let replies: AsyncStream<Reply>

    /// Starts the server; when that fails, `transcribe` runs the one-shot command.
    init(executable: String) {
        // Writing to a server that already died must fail, not kill the app.
        signal(SIGPIPE, SIG_IGN)
        self.executable = (executable as NSString).expandingTildeInPath
        let output = Pipe()
        let (stream, continuation) = AsyncStream.makeStream(of: Reply.self)
        replies = stream
        process.executableURL = URL(fileURLWithPath: self.executable)
        process.arguments = ["dictate", "--serve"]
        process.environment = Transcriber.environment()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = Self.openLog()
        do {
            try process.run()
        } catch {
            Self.append("could not start \(self.executable): \(error.localizedDescription)")
            continuation.finish()
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let reader = output.fileHandleForReading
            var buffer = Data()
            while true {
                let chunk = reader.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                    let line = buffer[buffer.startIndex..<newline]
                    buffer.removeSubrange(buffer.startIndex...newline)
                    if let reply = try? JSONDecoder().decode(Reply.self, from: line) { continuation.yield(reply) }
                }
            }
            continuation.finish()
        }
    }

    /// The text of `wav`, from the server if it is still running, otherwise
    /// from `paraspeakrs dictate FILE`. Ends the server either way.
    func transcribe(_ wav: URL) async -> Result<String, DictationError> {
        defer { cancel() }
        let line = Data((wav.path + "\n").utf8)
        if process.isRunning, (try? input.fileHandleForWriting.write(contentsOf: line)) != nil {
            for await reply in replies {
                if let text = reply.text { return .success(text) }
                if let error = reply.error { return .failure(DictationError(message: error)) }
            }
        }
        Self.append("dictate --serve ended without a reply; running dictate \(wav.path)")
        return await Task.detached { [executable] in Self.oneShot(wav, executable: executable) }.value
    }

    /// Closes stdin; the server exits after any request it is still answering.
    func cancel() {
        try? input.fileHandleForWriting.close()
    }

    private static func oneShot(_ wav: URL, executable: String) -> Result<String, DictationError> {
        guard FileManager.default.isExecutableFile(atPath: executable) else {
            return .failure(DictationError(message: "paraspeakrs not found at \(executable)"))
        }
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = ["dictate", wav.path]
        process.environment = Transcriber.environment()
        process.standardOutput = output
        process.standardError = openLog()
        do { try process.run() } catch {
            return .failure(DictationError(message: "could not start paraspeakrs: \(error.localizedDescription)"))
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            return .failure(DictationError(message: "paraspeakrs dictate exited with status \(process.terminationStatus); see \(log.path)"))
        }
        var text = String(decoding: data, as: UTF8.self)
        if text.hasSuffix("\n") { text.removeLast() }
        return .success(text)
    }

    static func append(_ message: String) {
        guard let handle = openLog() else { return }
        handle.write(Data("\(Date()) recorder: \(message)\n".utf8))
        try? handle.close()
    }

    private static func openLog() -> FileHandle? {
        try? FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil) }
        let handle = try? FileHandle(forWritingTo: log)
        _ = try? handle?.seekToEnd()
        return handle
    }
}

struct DictationError: Error { let message: String }
