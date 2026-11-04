import AppKit
import Carbon.HIToolbox
import SwiftUI

/// What a `ShortcutField` calls: `begin` switches the global shortcuts off, so
/// the current combinations can be pressed again; `end` takes the new one, or
/// nil after Escape, and returns why it could not be used.
struct ShortcutEditing {
    let begin: () -> Void
    let end: (Shortcut?) -> String?
}

/// The editing state of one `ShortcutField`. An `ObservableObject` rather than
/// `@State`, which the Command Line Tools cannot build with the macOS 27.0 SDK.
@MainActor
final class ShortcutRecorder: ObservableObject {
    @Published var hovering = false
    @Published private(set) var recording = false
    @Published private(set) var problem: String?

    /// Only one field records at a time; clicking another cancels this one.
    private static weak var active: ShortcutRecorder?
    private var monitor: Any?
    private var end: ((Shortcut?) -> String?)?

    func start(_ editing: ShortcutEditing) {
        guard !recording else { return }
        Self.active?.cancel()
        Self.active = self
        editing.begin()
        end = editing.end
        problem = nil
        recording = true
        // Opening the panel does not always make the recorder the active app,
        // and a local monitor sees only the keys sent to it.
        NSApp.activate()
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            MainActor.assumeIsolated { self?.handle(event) }
            return nil
        }
    }

    func cancel() { finish(nil) }

    private func handle(_ event: NSEvent) {
        if event.keyCode == kVK_Escape, event.modifierFlags.isDisjoint(with: [.command, .option, .control, .shift]) {
            finish(nil)
        } else if let shortcut = Shortcut(event) {
            finish(shortcut)
        } else {
            problem = "Hold ⌘, ⌥ or ⌃ with the key."
        }
    }

    private func finish(_ shortcut: Shortcut?) {
        guard recording else { return }
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        recording = false
        if Self.active === self { Self.active = nil }
        problem = end?(shortcut)
        end = nil
    }
}

/// A shortcut label that is a button: outlined on hover, and a click records
/// the next key combination. Escape cancels.
struct ShortcutField: View {
    let shortcut: Shortcut
    let editing: ShortcutEditing
    @StateObject private var recorder = ShortcutRecorder()

    var body: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Button { recorder.start(editing) } label: {
                Text(recorder.recording ? "Press keys…" : shortcut.label)
                    .foregroundStyle(recorder.recording ? Color.accentColor : .secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .overlay {
                        RoundedRectangle(cornerRadius: 5)
                            .strokeBorder(recorder.recording ? Color.accentColor : .secondary,
                                          lineWidth: recorder.recording || recorder.hovering ? 1 : 0)
                    }
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .onHover { recorder.hovering = $0 }
            .help(recorder.recording ? "Press the new shortcut; Escape cancels" : "Click to change the shortcut")
            .accessibilityLabel(recorder.recording ? "Press keys" : "Shortcut \(shortcut.label)")
            if let problem = recorder.problem {
                Text(problem).font(.caption).foregroundStyle(.orange)
            }
        }
        // Closing the panel ends recording and restores the old shortcut.
        .onDisappear { recorder.cancel() }
    }
}
