import AppKit

/// Dictation sounds: one synth tone each for start, stop and cancel. They are
/// in the app bundle's Resources; a build run with
/// `swift run` has none and stays silent. The panel's Sounds toggle mutes all.
enum DictationSounds {
    /// UserDefaults key of the panel's toggle; absent means on.
    static let enabledKey = "dictationSounds"

    static let start = bundled("dictation-start")
    static let stop = bundled("dictation-stop")
    static let cancel = bundled("dictation-cancel")
    static let failure = NSSound(named: "Basso")

    static func play(_ sound: NSSound?) {
        guard UserDefaults.standard.object(forKey: enabledKey) as? Bool ?? true else { return }
        sound?.play()
    }

    private static func bundled(_ name: String) -> NSSound? {
        Bundle.main.url(forResource: name, withExtension: "wav").flatMap { NSSound(contentsOf: $0, byReference: true) }
    }
}
