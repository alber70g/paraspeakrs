import AppKit
import SwiftUI

/// The panel's dictation part: the shortcut and the last transcript that
/// could not be typed. The Accessibility grant is in `PermissionsSection`.
struct DictationSection: View {
    @ObservedObject var dictation: DictationModel
    @AppStorage(DictationSounds.enabledKey) private var sounds = true

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Dictation: hold, or tap to start and stop")
                Spacer()
                Text("⌃\\").foregroundStyle(.secondary)
            }
            Toggle("Sounds", isOn: $sounds)
            if !dictation.hotKeyActive {
                Text("⌃\\ is taken by another app.").foregroundStyle(.orange)
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
        }
    }
}
