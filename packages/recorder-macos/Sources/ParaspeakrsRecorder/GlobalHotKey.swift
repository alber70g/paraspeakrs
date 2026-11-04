import Carbon.HIToolbox

/// A system-wide shortcut through Carbon's `RegisterEventHotKey`, which needs
/// no Accessibility permission (an `NSEvent` global monitor does). The key is
/// taken before the frontmost app sees it: with ⌘⇧M, Teams' own mute shortcut
/// stops working while the recorder runs. The user chose that knowingly.
///
/// Every instance installs its own handler on the application target, and
/// each handler sees every hot-key event, so it checks the id and passes on
/// the ones that belong to another shortcut.
final class GlobalHotKey {
    private static let signature = OSType(0x5052_5243)  // "PRRC"
    private static var nextID: UInt32 = 1

    private let id: UInt32
    private var hotKey: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let onPress: () -> Void
    private let onRelease: () -> Void
    private var isDown = false

    /// Returns nil when another app already registered the same combination.
    init?(keyCode: Int, modifiers: Int, onPress: @escaping () -> Void, onRelease: @escaping () -> Void = {}) {
        id = Self.nextID
        Self.nextID += 1
        self.onPress = onPress
        self.onRelease = onRelease
        var kinds = [kEventHotKeyPressed, kEventHotKeyReleased].map {
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32($0))
        }
        let installed = InstallEventHandler(GetApplicationEventTarget(), { _, event, context in
            guard let event, let context else { return OSStatus(eventNotHandledErr) }
            let hotKey = Unmanaged<GlobalHotKey>.fromOpaque(context).takeUnretainedValue()
            return hotKey.handle(event) ? noErr : OSStatus(eventNotHandledErr)
        }, kinds.count, &kinds, Unmanaged.passUnretained(self).toOpaque(), &handler)
        guard installed == noErr else { return nil }

        let hotKeyID = EventHotKeyID(signature: Self.signature, id: id)
        guard RegisterEventHotKey(UInt32(keyCode), UInt32(modifiers), hotKeyID, GetApplicationEventTarget(), 0,
                                  &hotKey) == noErr else {
            RemoveEventHandler(handler)
            return nil
        }
    }

    deinit {
        if let hotKey { UnregisterEventHotKey(hotKey) }
        if let handler { RemoveEventHandler(handler) }
    }

    private func handle(_ event: EventRef) -> Bool {
        var hotKeyID = EventHotKeyID()
        let status = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                       nil, MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID)
        guard status == noErr, hotKeyID.signature == Self.signature, hotKeyID.id == id else { return false }
        if GetEventKind(event) == UInt32(kEventHotKeyPressed) {
            // A second press without a release in between is a key repeat.
            guard !isDown else { return true }
            isDown = true
            onPress()
        } else {
            isDown = false
            onRelease()
        }
        return true
    }

    /// ⌘⇧M, start or stop a meeting recording.
    static func meetingRecording(_ action: @escaping () -> Void) -> GlobalHotKey? {
        GlobalHotKey(keyCode: kVK_ANSI_M, modifiers: cmdKey | shiftKey, onPress: action)
    }

    /// Ctrl+\, hold to dictate or tap to start and stop. `kVK_ANSI_Backslash`
    /// is a key position: on a non-US layout it may carry another character.
    static func dictation(onPress: @escaping () -> Void, onRelease: @escaping () -> Void) -> GlobalHotKey? {
        GlobalHotKey(keyCode: kVK_ANSI_Backslash, modifiers: controlKey, onPress: onPress, onRelease: onRelease)
    }

    /// Escape, registered only while dictating. Also ⌃Escape: while Ctrl+\ is
    /// held for push-to-talk, Control is still down when Escape is pressed.
    static func cancelDictation(_ action: @escaping () -> Void) -> [GlobalHotKey] {
        [0, controlKey].compactMap { GlobalHotKey(keyCode: kVK_Escape, modifiers: $0, onPress: action) }
    }
}
