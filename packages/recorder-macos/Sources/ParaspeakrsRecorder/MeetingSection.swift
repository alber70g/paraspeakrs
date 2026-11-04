import AppKit
import RecorderCore
import SwiftUI

/// The panel's meeting part: the shortcut, the record button, the levels while
/// recording, and what went wrong.
struct MeetingSection: View {
    @ObservedObject var model: RecorderModel
    let editShortcut: ShortcutEditing

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Meeting recording").font(.headline)
                Spacer()
                ShortcutField(shortcut: model.shortcut, editing: editShortcut)
            }
            Button(model.isRecording ? "Stop recording" : "Start recording") { model.toggle() }
                .keyboardShortcut(.defaultAction)
                .controlSize(.large)
            if model.isRecording {
                Text("Press \(model.shortcut.label) twice to stop; one press goes to the app in front.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }

            if let startedAt = model.startedAt, let capture = model.capture {
                TimelineView(.periodic(from: .now, by: 0.1)) { context in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(Self.elapsed(from: startedAt, to: context.date)).monospacedDigit().font(.title3)
                        meter("Microphone (L)", capture.levels.mic)
                        meter("System audio (R)", capture.levels.system)
                    }
                }
            }

            if model.systemSilent {
                warning("No system audio for \(Int(RecorderModel.silenceWarningSeconds)) s. If a call is playing, allow "
                        + "Paraspeakrs Recorder under System Audio Recording.",
                        settings: "Privacy_ScreenCapture")
            }
            if let error = model.error {
                Text(error).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                ReportButton(message: error, log: model.errorLog)
            }
            if model.finishing > 0 { Text("Saving recording…").foregroundStyle(.secondary) }
            if model.transcribing > 0 {
                Text("Transcribing: \(model.transcribing) in queue").foregroundStyle(.secondary)
            }
        }
    }

    private func meter(_ label: String, _ peak: Float) -> some View {
        // -60 dBFS maps to empty, 0 dBFS to full.
        let level = peak > 0 ? max(0, min(1, (20 * log10(Double(peak)) + 60) / 60)) : 0
        return VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            ProgressView(value: level)
        }
    }

    private func warning(_ text: String, settings anchor: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(text).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            Button("Open System Settings") {
                NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)")!)
            }
        }
    }

    private static func elapsed(from start: Date, to now: Date) -> String {
        let seconds = Int(now.timeIntervalSince(start))
        return String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
    }
}
