import AppKit
import SwiftUI

@main
struct RecorderApp: App {
    @StateObject private var model = RecorderModel()

    var body: some Scene {
        MenuBarExtra {
            RecorderPanel(model: model)
        } label: {
            Image(nsImage: Self.icon(recording: model.isRecording))
        }
        .menuBarExtraStyle(.window)
    }

    /// The menu bar renders template images in one colour, so the recording
    /// icon is a non-template image to stay red.
    private static func icon(recording: Bool) -> NSImage {
        let name = recording ? "record.circle.fill" : "waveform.circle"
        let base = NSImage(systemSymbolName: name, accessibilityDescription: recording ? "Recording" : "Recorder")!
        guard recording else {
            base.isTemplate = true
            return base
        }
        let red = base.withSymbolConfiguration(.init(paletteColors: [.systemRed])) ?? base
        red.isTemplate = false
        return red
    }
}
