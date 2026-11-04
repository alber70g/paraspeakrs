import AppKit
import SwiftUI

/// A new GitHub issue for an error the panel shows, with the message, the
/// paraspeakrs and macOS versions and the end of the matching log filled in.
/// The issue is public, so the home directory becomes `~`; the user sees the
/// text on GitHub before submitting it.
enum IssueReport {
    private static let newIssue = "https://github.com/alber70g/paraspeakrs/issues/new"
    private static let logLines = 30
    /// GitHub rejects much longer URLs; percent-encoding about triples the log.
    private static let maxLogCharacters = 2500

    static func url(message: String, log: URL?, paraspeakrsVersion: String) -> URL {
        let firstLine = message.split(whereSeparator: \.isNewline).first.map(String.init) ?? message
        let title = "Recorder: \(firstLine.prefix(80))"
        var body = """
            The Paraspeakrs Recorder showed this error:

            ```
            \(message)
            ```

            | | |
            |---|---|
            | paraspeakrs | \(paraspeakrsVersion) |
            | macOS | \(ProcessInfo.processInfo.operatingSystemVersionString) |

            """
        if let log, let tail = tail(of: log) {
            body += "\nLast lines of `\(log.path)`:\n\n```\n\(tail)\n```\n"
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let query = [("title", title), ("body", body)]
            .map { "\($0)=\(encode($1.replacingOccurrences(of: home, with: "~")))" }
            .joined(separator: "&")
        return URL(string: "\(newIssue)?\(query)")!
    }

    /// Opens the issue in the browser. `paraspeakrs --version` runs off the main thread.
    @MainActor
    static func open(message: String, log: URL?) {
        let executable = (RecorderModel.executable as NSString).expandingTildeInPath
        Task.detached {
            let version = paraspeakrsVersion(executable)
            await MainActor.run {
                _ = NSWorkspace.shared.open(url(message: message, log: log, paraspeakrsVersion: version))
            }
        }
    }

    private static func tail(of log: URL) -> String? {
        guard let text = try? String(contentsOf: log, encoding: .utf8) else { return nil }
        let lines = text.split(whereSeparator: \.isNewline).suffix(logLines).joined(separator: "\n")
        return lines.isEmpty ? nil : String(lines.suffix(maxLogCharacters))
    }

    private static func paraspeakrsVersion(_ executable: String) -> String {
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = ["--version"]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return "not found at \(executable)" }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        // `paraspeakrs --version` prints "paraspeakrs 0.9.1".
        let text = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "paraspeakrs ", with: "")
        return process.terminationStatus == 0 && !text.isEmpty ? text : "unknown"
    }

    /// `URLComponents` leaves `+` and `&` in query values as they are; GitHub
    /// would read them as a space and a new parameter.
    private static func encode(_ value: String) -> String {
        let unreserved = CharacterSet(charactersIn:
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return value.addingPercentEncoding(withAllowedCharacters: unreserved) ?? value
    }
}

/// The button shown under an error in the panel.
struct ReportButton: View {
    let message: String
    let log: URL?

    var body: some View {
        Button("Report on GitHub") { IssueReport.open(message: message, log: log) }
    }
}
