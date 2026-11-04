import Carbon.HIToolbox

/// A system-wide shortcut through Carbon's `RegisterEventHotKey`, which needs
/// no Accessibility permission (an `NSEvent` global monitor does). The key is
/// taken before the frontmost app sees it: with ⌘⇧M, Teams' own mute shortcut
/// stops working while the recorder runs. The user chose that knowingly.
final class GlobalHotKey {
    private var hotKey: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let action: () -> Void

    /// Returns nil when another app already registered the same combination.
    init?(keyCode: Int, modifiers: Int, action: @escaping () -> Void) {
        self.action = action
        var pressed = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let installed = InstallEventHandler(GetApplicationEventTarget(), { _, _, context in
            guard let context else { return OSStatus(eventNotHandledErr) }
            Unmanaged<GlobalHotKey>.fromOpaque(context).takeUnretainedValue().action()
            return noErr
        }, 1, &pressed, Unmanaged.passUnretained(self).toOpaque(), &handler)
        guard installed == noErr else { return nil }

        let id = EventHotKeyID(signature: OSType(0x5052_5243), id: 1)  // "PRRC"
        guard RegisterEventHotKey(UInt32(keyCode), UInt32(modifiers), id, GetApplicationEventTarget(), 0,
                                  &hotKey) == noErr else {
            RemoveEventHandler(handler)
            return nil
        }
    }

    deinit {
        if let hotKey { UnregisterEventHotKey(hotKey) }
        if let handler { RemoveEventHandler(handler) }
    }

    /// ⌘⇧M, start or stop a meeting recording.
    static func meetingRecording(_ action: @escaping () -> Void) -> GlobalHotKey? {
        GlobalHotKey(keyCode: kVK_ANSI_M, modifiers: cmdKey | shiftKey, action: action)
    }
}
