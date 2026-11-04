import ApplicationServices
import CoreGraphics
import Foundation

/// Types text into the frontmost app with synthetic Unicode key events. The
/// clipboard is never touched. Values from VoiceInk's `DirectTextTyper`
/// (20 UTF-16 units per event, 10 ms apart, virtual key 0x31), posted at the
/// session tap: MacParakeet and FluidVoice report that HID-level posting left
/// Command stuck down.
enum TextTyper {
    enum Failure: Error {
        case noAccessibility, modifierHeld

        var message: String {
            switch self {
            case .noAccessibility: "Accessibility is off for Paraspeakrs Recorder, so it cannot type."
            case .modifierHeld: "A modifier key was still held after 2 s, so nothing was typed."
            }
        }
    }

    private static let unitsPerEvent = 20
    private static let held: CGEventFlags = [.maskShift, .maskControl, .maskAlternate, .maskCommand, .maskSecondaryFn]

    static var isTrusted: Bool { AXIsProcessTrusted() }

    /// Shows the system prompt that leads to the Accessibility list.
    static func requestTrust() {
        _ = AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": true] as CFDictionary)
    }

    /// Without Accessibility macOS drops the events and reports no error
    /// (Epicenter ADR-0040), so the grant is checked first. With Control still
    /// down from Ctrl+\, each character would arrive as a Control shortcut.
    static func type(_ text: String) async throws(Failure) {
        guard isTrusted else { throw .noAccessibility }
        guard await modifiersReleased(within: 2) else { throw .modifierHeld }
        let source = CGEventSource(stateID: .privateState)
        for chunk in chunks(of: text) {
            let units = Array(chunk.utf16)
            for keyDown in [true, false] {
                guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0x31, keyDown: keyDown) else { continue }
                event.flags = []
                event.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
                event.post(tap: .cgSessionEventTap)
            }
            try? await Task.sleep(for: .milliseconds(10))
        }
    }

    /// Pieces of at most 20 UTF-16 units that never split a character; a
    /// single character longer than that (a long emoji sequence) goes alone.
    static func chunks(of text: String) -> [String] {
        var result: [String] = []
        var current = ""
        for character in text {
            if !current.isEmpty, current.utf16.count + character.utf16.count > unitsPerEvent {
                result.append(current)
                current = ""
            }
            current.append(character)
        }
        if !current.isEmpty { result.append(current) }
        return result
    }

    private static func modifiersReleased(within seconds: Double) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while !CGEventSource.flagsState(.hidSystemState).intersection(held).isEmpty {
            if Date() >= deadline { return false }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return true
    }
}
