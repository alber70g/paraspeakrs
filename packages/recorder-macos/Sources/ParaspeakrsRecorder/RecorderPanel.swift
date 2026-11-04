import AppKit
import SwiftUI

struct RecorderPanel: View {
    @ObservedObject var model: RecorderModel
    @ObservedObject var dictation: DictationModel
    @AppStorage("paraspeakrsPath") private var executable = Transcriber.defaultExecutable

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            DictationSection(dictation: dictation, editShortcut: dictationEditing)

            Divider()
            MeetingSection(model: model, editShortcut: meetingEditing)

            Divider()
            PermissionsSection()

            Divider()
            Text("paraspeakrs executable").font(.caption).foregroundStyle(.secondary)
            TextField("path", text: $executable).textFieldStyle(.roundedBorder)
            HStack {
                Button("Open recordings") {
                    try? FileManager.default.createDirectory(at: RecorderModel.recordingsDirectory,
                                                             withIntermediateDirectories: true)
                    NSWorkspace.shared.open(RecorderModel.recordingsDirectory)
                }
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }.disabled(model.isRecording)
            }
        }
        .padding(14)
        .frame(width: 300)
    }

    /// Both shortcuts are off while either is recorded: a live one would take
    /// its keys before the field sees them. The two must also differ.
    private var dictationEditing: ShortcutEditing {
        ShortcutEditing(begin: suspendBoth) { [model, dictation] new in
            defer { model.resumeShortcut() }
            if let new, new.sameKeys(as: model.shortcut) {
                _ = dictation.endEditingShortcut(nil)
                return "\(new.label) is the meeting recording shortcut."
            }
            return dictation.endEditingShortcut(new)
        }
    }

    private var meetingEditing: ShortcutEditing {
        ShortcutEditing(begin: suspendBoth) { [model, dictation] new in
            defer {
                model.resumeShortcut()
                _ = dictation.endEditingShortcut(nil)
            }
            guard let new else { return nil }
            if new.sameKeys(as: dictation.shortcut) { return "\(new.label) is the dictation shortcut." }
            model.setShortcut(new)
            return nil
        }
    }

    private func suspendBoth() {
        model.suspendShortcut()
        dictation.suspendShortcut()
    }
}
