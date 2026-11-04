import AppKit
import SwiftUI

@main
struct RecorderApp: App {
    @StateObject private var model = RecorderModel()
    @StateObject private var dictation = DictationModel()

    var body: some Scene {
        MenuBarExtra {
            RecorderPanel(model: model, dictation: dictation)
        } label: {
            Image(nsImage: Self.icon(recording: model.isRecording, dictation: dictation.phase))
        }
        .menuBarExtraStyle(.window)
    }

    /// The menu bar renders template images in one colour, so the recording
    /// icon is a non-template image to stay red. A meeting recording wins over
    /// a dictation.
    private static func icon(recording: Bool, dictation: DictationModel.Phase) -> NSImage {
        let (name, label): (String, String) = if recording {
            ("record.circle.fill", "Recording")
        } else {
            switch dictation {
            case .idle: ("waveform.circle", "Recorder")
            case .recording: ("mic.fill", "Dictating")
            case .transcribing: ("ellipsis.circle", "Transcribing dictation")
            }
        }
        let base = NSImage(systemSymbolName: name, accessibilityDescription: label)!
        guard recording else {
            base.isTemplate = true
            return base
        }
        let red = base.withSymbolConfiguration(.init(paletteColors: [.systemRed])) ?? base
        red.isTemplate = false
        return red
    }
}
