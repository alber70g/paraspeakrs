import AppKit
import Carbon.HIToolbox

/// A key combination for `GlobalHotKey`: a key position, Carbon modifiers, and
/// the label the panel shows. Stored in UserDefaults as JSON.
struct Shortcut: Codable, Equatable {
    var keyCode: UInt32
    var modifiers: UInt32
    var label: String

    /// ⇧⌘M, start or stop a meeting recording.
    static let meetingDefault = Shortcut(keyCode: UInt32(kVK_ANSI_M), modifiers: UInt32(cmdKey | shiftKey), label: "⇧⌘M")
    /// ⌃\, dictation. `kVK_ANSI_Backslash` is a key position: on a non-US
    /// layout it may carry another character.
    static let dictationDefault = Shortcut(keyCode: UInt32(kVK_ANSI_Backslash), modifiers: UInt32(controlKey), label: "⌃\\")

    static func load(_ key: String, default fallback: Shortcut) -> Shortcut {
        UserDefaults.standard.data(forKey: key).flatMap { try? JSONDecoder().decode(Shortcut.self, from: $0) } ?? fallback
    }

    func save(_ key: String) {
        UserDefaults.standard.set(try? JSONEncoder().encode(self), forKey: key)
    }

    /// The combination of a key press, or nil without ⌘, ⌥ or ⌃: Shift alone
    /// would take a plain character from every app.
    init?(_ event: NSEvent) {
        guard !event.modifierFlags.isDisjoint(with: [.command, .option, .control]) else { return nil }
        let carbon = Self.carbonModifiers(event.modifierFlags)
        // Apple's order in menus: ⌃⌥⇧⌘.
        let symbols = [(controlKey, "⌃"), (optionKey, "⌥"), (shiftKey, "⇧"), (cmdKey, "⌘")]
            .filter { carbon & UInt32($0.0) != 0 }.map(\.1).joined()
        self.init(keyCode: UInt32(event.keyCode), modifiers: carbon, label: symbols + Self.keyName(event))
    }

    /// The same keys, whatever the label says.
    func sameKeys(as other: Shortcut) -> Bool { keyCode == other.keyCode && modifiers == other.modifiers }

    /// Whether `event` is this combination, ignoring Caps Lock and the like.
    func matches(_ event: NSEvent) -> Bool {
        UInt32(event.keyCode) == keyCode && Self.carbonModifiers(event.modifierFlags) == modifiers
    }

    static func carbonModifiers(_ flags: NSEvent.ModifierFlags) -> UInt32 {
        var carbon = 0
        if flags.contains(.control) { carbon |= controlKey }
        if flags.contains(.option) { carbon |= optionKey }
        if flags.contains(.shift) { carbon |= shiftKey }
        if flags.contains(.command) { carbon |= cmdKey }
        return UInt32(carbon)
    }

    private init(keyCode: UInt32, modifiers: UInt32, label: String) {
        self.keyCode = keyCode
        self.modifiers = modifiers
        self.label = label
    }

    private static let named: [Int: String] = [
        kVK_Space: "Space", kVK_Return: "↩", kVK_Tab: "⇥", kVK_Delete: "⌫", kVK_ForwardDelete: "⌦",
        kVK_Escape: "⎋", kVK_LeftArrow: "←", kVK_RightArrow: "→", kVK_UpArrow: "↑", kVK_DownArrow: "↓",
        kVK_F1: "F1", kVK_F2: "F2", kVK_F3: "F3", kVK_F4: "F4", kVK_F5: "F5", kVK_F6: "F6",
        kVK_F7: "F7", kVK_F8: "F8", kVK_F9: "F9", kVK_F10: "F10", kVK_F11: "F11", kVK_F12: "F12",
    ]

    /// The key's character without modifiers, so ⇧⌘M reads M, not m or µ.
    private static func keyName(_ event: NSEvent) -> String {
        if let name = named[Int(event.keyCode)] { return name }
        let character = event.characters(byApplyingModifiers: [])?.uppercased() ?? ""
        return character.isEmpty ? "Key \(event.keyCode)" : character
    }
}
