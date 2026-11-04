import AppKit
import SwiftUI

/// The panel's dictation part: the shortcut, the last transcript that could
/// not be typed, and recent dictations to type again. The Accessibility grant
/// is in `PermissionsSection`.
struct DictationSection: View {
    @ObservedObject var dictation: DictationModel
    let editShortcut: ShortcutEditing
    @AppStorage(DictationSounds.enabledKey) private var sounds = true

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Dictation").font(.headline)
                Spacer()
                ShortcutField(shortcut: dictation.shortcut, editing: editShortcut)
            }
            Text("Hold, or tap to start and stop").foregroundStyle(.secondary)
            Toggle("Sounds", isOn: $sounds)
            if !dictation.hotKeyActive {
                Text("\(dictation.shortcut.label) is taken by another app.").foregroundStyle(.orange)
            }
            if let failure = dictation.failure {
                Text(failure).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                ReportButton(message: failure, log: DictationTranscriber.log)
            }
            if let untyped = dictation.untyped {
                Text(untyped).font(.callout).textSelection(.enabled).lineLimit(6)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(untyped, forType: .string)
                }
            }
            if !dictation.recent.isEmpty {
                Text("Recent: click to type into the app you were in").font(.caption).foregroundStyle(.secondary)
                ForEach(dictation.recent, id: \.self) { text in
                    Button { dictation.retype(text) } label: {
                        Text(text).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help(text)
                }
                .disabled(dictation.phase != .idle)
            }
        }
    }
}
