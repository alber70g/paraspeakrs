import Foundation

public enum PermissionStatus { case granted, denied, notAsked, unknown }

/// The System Audio Recording grant. macOS has no public API to read it, so
/// this calls the private `TCCAccessPreflight`, as AudioCap does
/// (github.com/insidegui/AudioCap). Without that symbol the state is unknown.
public enum SystemAudioPermission {
    private typealias Preflight = @convention(c) (CFString, CFDictionary?) -> Int

    private static let preflight: Preflight? = {
        guard let handle = dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW),
              let symbol = dlsym(handle, "TCCAccessPreflight") else { return nil }
        return unsafeBitCast(symbol, to: Preflight.self)
    }()

    /// 0, 1 and 2 are the values seen on macOS 26.5; anything else is unknown.
    public static var status: PermissionStatus {
        guard let preflight else { return .unknown }
        switch preflight("kTCCServiceAudioCapture" as CFString, nil) {
        case 0: return .granted
        case 1: return .denied
        case 2: return .notAsked
        default: return .unknown
        }
    }
}
