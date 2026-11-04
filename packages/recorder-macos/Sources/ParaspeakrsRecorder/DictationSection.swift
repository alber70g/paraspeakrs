import AppKit
import SwiftUI

/// The panel's dictation part: the shortcut, the Accessibility grant typing
/// needs, and the last transcript that could not be typed.
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
            if !dictation.accessibilityTrusted {
                Text("Accessibility is off, so dictation cannot type.").foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Open System Settings") {
                    NSWorkspace.shared.open(URL(string:
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
                }
            }
            if let failure = dictation.failure {
                Text(failure).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
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
        // The grant changes in System Settings; check again while the panel is open.
        .task {
            while !Task.isCancelled {
                dictation.refreshTrust()
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }
}
